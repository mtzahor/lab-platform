from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from lab_platform.agent_protocol import CommandRequestPayload
from lab_platform.models import (
    ActorContext,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    PrincipalType,
    RemoteCommand,
    RemoteCommandType,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.distributed import (
    SQLiteGlobalBenchRepository,
    SQLiteRemoteCommandRepository,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


def test_remote_command_persists_and_serializes_initiating_actor(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "actor-context.db")
        database.initialize()
        agent_id = uuid4()
        organisation_id = uuid4()
        snapshot_id = uuid4()
        principal_id = uuid4()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO agents "
                "(id, slug, name, status, version, protocol_version, labels_json, "
                "registered_at, enrollment_status, organisation_id) "
                "VALUES (?, 'home-lab', 'Home Lab', 'ONLINE', '0.7.0-alpha', '1.0', "
                "'{}', ?, 'ENROLLED', ?)",
                (str(agent_id), NOW.isoformat(), str(organisation_id)),
            )
        bench = GlobalBenchRecord(
            id="home-lab/esp32-01",
            agent_id=agent_id,
            agent_slug="home-lab",
            local_bench_id="esp32-01",
            name="ESP32",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            created_at=NOW,
            updated_at=NOW,
        )
        await SQLiteGlobalBenchRepository(database).upsert(bench)
        actor = ActorContext(
            principal_id=principal_id,
            principal_type=PrincipalType.USER,
            display_name="Alice Example",
            organisation_id=organisation_id,
            authorisation_snapshot_id=snapshot_id,
        )
        command = RemoteCommand(
            agent_id=agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.PROBE,
            expires_at=NOW + timedelta(minutes=10),
            idempotency_key="actor-traceability",
            actor_context=actor,
            authorisation_snapshot_id=snapshot_id,
            created_at=NOW,
        )

        repository = SQLiteRemoteCommandRepository(database)
        await repository.create(command)
        restored = await repository.get(command.id)

        assert restored == command
        payload = CommandRequestPayload(command=restored).model_dump(mode="json")
        assert payload["command"]["actor_context"] == {
            "principal_id": str(principal_id),
            "principal_type": "USER",
            "display_name": "Alice Example",
            "organisation_id": str(organisation_id),
            "authorisation_snapshot_id": str(snapshot_id),
        }
        database.close()

    asyncio.run(scenario())


def test_phase5_commands_without_actor_context_remain_valid() -> None:
    command = RemoteCommand(
        agent_id=uuid4(),
        bench_id="home-lab/legacy",
        command_type=RemoteCommandType.PROBE,
        expires_at=NOW + timedelta(minutes=10),
        idempotency_key="legacy-command",
        created_at=NOW,
    )

    assert command.actor_context is None
    assert CommandRequestPayload(command=command).command == command
