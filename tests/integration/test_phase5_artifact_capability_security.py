from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import CommandRequestEnvelope
from lab_platform.agent_runtime.command_journal import command_fingerprint
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.models import (
    AgentConnectionRecord,
    AgentStatus,
    ArtifactOwnerType,
    ArtifactTransferDirection,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)
from lab_platform.persistence.distributed import SQLiteAgentConnectionRepository
from starlette.websockets import WebSocketState


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "artifacts": {
                    "directory": tmp_path / "artifacts",
                    "transfer_token_ttl_seconds": 300,
                },
                "development": {"allow_insecure_agent_transport": True},
            }
        )
    )


async def _attach(runtime: ControlPlaneRuntime, agent_id: UUID) -> Any:
    connection_id = uuid4()
    boot_id = uuid4()
    connected_at = datetime.now(UTC)
    await SQLiteAgentConnectionRepository(runtime.database).open(
        AgentConnectionRecord(
            id=connection_id,
            agent_id=agent_id,
            boot_id=boot_id,
            protocol_version="1.0",
            connected_at=connected_at,
            last_heartbeat_at=connected_at,
        )
    )
    websocket = SimpleNamespace(
        application_state=WebSocketState.CONNECTED,
        close=AsyncMock(),
    )
    return await runtime.hub.attach(
        agent_id=agent_id,
        connection_id=connection_id,
        boot_id=boot_id,
        websocket=cast(Any, websocket),
    )


def _database_dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


def test_download_capabilities_are_transient_and_reissued_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        runtime = _runtime(tmp_path)
        await runtime.start()
        enrollment = await runtime.enrollment.issue_token(
            name="artifact-command-agent",
            expires_at=now + timedelta(minutes=30),
            allow_internal_authorisation=True,
        )
        enrolled = await runtime.enrollment.enroll(
            plaintext_token=enrollment.plaintext.get_secret_value(),
            request_id=uuid4(),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        agent = enrolled.agent.model_copy(
            update={
                "status": AgentStatus.ONLINE,
                "last_connected_at": now,
                "last_seen_at": now,
            }
        )
        bench = GlobalBenchRecord(
            id=f"{agent.slug}/bench-01",
            agent_id=agent.id,
            agent_slug=agent.slug,
            local_bench_id="bench-01",
            name="Capability security bench",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            capabilities=frozenset({"firmware"}),
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        await runtime.inventory_repository.reconcile_agent_snapshot(
            enrolled.agent,
            (bench,),
            observed_at=now,
        )
        monkeypatch.setattr(runtime.directory, "get_agent", AsyncMock(return_value=agent))
        monkeypatch.setattr(runtime.directory, "get_bench", AsyncMock(return_value=bench))

        content = b"signed-firmware-image"
        artifact = await runtime.platform_artifacts.store_bytes(
            content,
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="firmware.bin",
            artifact_type="firmware",
        )
        durable_descriptor = await runtime.workflow_artifacts.issue_download(
            agent_id=agent.id,
            input_name="firmware",
            artifact_id=artifact.id,
            target_path=f"artifacts/{artifact.id}",
            idempotency_key="stage-firmware",
            allow_internal_authorisation=True,
        )
        assert await runtime.artifact_transfers.list(agent_id=agent.id, limit=10) == []
        reservation = ReservationLease(
            reservation_id=uuid4(),
            agent_id=agent.id,
            bench_id=bench.id,
            owner="ci/security-test",
            valid_from=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=20),
            lease_version=1,
        )
        with pytest.raises(ValueError, match="cannot be persisted"):
            await runtime.commands.create(
                agent_id=agent.id,
                bench_id=bench.id,
                command_type=RemoteCommandType.FLASH,
                payload={
                    "artifact": {
                        **durable_descriptor.as_payload(),
                        "transfer_token": "lpt_plaintext_must_never_reach_sqlite_1234567890",
                    }
                },
                expires_at=now + timedelta(minutes=20),
                idempotency_key="reject-persisted-capability",
                reservation_lease=reservation,
                dispatch=False,
            )
        durable_payload = {
            "definition": {
                "name": "artifact-security",
                "version": 1,
                "steps": [{"action": "wait", "seconds": 1}],
            },
            "inputs": {"firmware": {"artifact_id": str(artifact.id)}},
            "owner": "ci/security-test",
            "artifact_transfers": [durable_descriptor.as_payload()],
            "reservation_lease": reservation.model_dump(mode="json"),
        }
        command = RemoteCommand(
            agent_id=agent.id,
            bench_id=bench.id,
            command_type=RemoteCommandType.RUN_WORKFLOW,
            payload=durable_payload,
            created_at=now,
            expires_at=now + timedelta(minutes=20),
            idempotency_key="artifact-capability-security",
            reservation_id=reservation.reservation_id,
            lease_version=reservation.lease_version,
        )
        await runtime.command_repository.create_bundle(command, None)
        first_session = await _attach(runtime, agent.id)

        first_command, _ = await runtime.commands.dispatch(
            command.id,
            reservation_lease=reservation,
        )
        assert first_command.status is RemoteCommandStatus.DISPATCHED
        first_envelope = cast(CommandRequestEnvelope, first_session.outgoing.get_nowait())
        first_transfer = first_envelope.payload.command.payload["artifact_transfers"][0]
        first_token = cast(str, first_transfer["transfer_token"])
        assert first_token.startswith("lpt_")
        assert first_token in first_envelope.model_dump_json()
        first_authorized = await runtime.artifacts.authorize(
            UUID(cast(str, first_transfer["transfer_id"])),
            first_token,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            agent_id=agent.id,
        )
        assert first_authorized.artifact_id == artifact.id
        assert len(await runtime.artifact_transfers.list(agent_id=agent.id, limit=10)) == 1

        with runtime.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM remote_commands WHERE id = ?",
                (str(command.id),),
            ).fetchone()
            assert row is not None
            persisted_payload = cast(str, row["payload_json"])
        assert first_token not in persisted_payload
        assert "transfer_token" not in persisted_payload
        assert "download_url" not in persisted_payload
        assert first_token not in _database_dump(tmp_path / "control-plane.db")

        assert await runtime.commands.mark_unknown(agent.id) == 1
        first_fingerprint = command_fingerprint(first_envelope.payload)
        await runtime.stop()

        restarted = _runtime(tmp_path)
        await restarted.start()
        monkeypatch.setattr(restarted.directory, "get_agent", AsyncMock(return_value=agent))
        monkeypatch.setattr(restarted.directory, "get_bench", AsyncMock(return_value=bench))
        second_session = await _attach(restarted, agent.id)
        replayed, _ = await restarted.commands.dispatch(
            command.id,
            reservation_lease=reservation,
        )
        assert replayed.status is RemoteCommandStatus.UNKNOWN
        second_envelope = cast(CommandRequestEnvelope, second_session.outgoing.get_nowait())
        assert second_envelope.payload.command.status is RemoteCommandStatus.DISPATCHED
        second_transfer = second_envelope.payload.command.payload["artifact_transfers"][0]
        second_token = cast(str, second_transfer["transfer_token"])
        assert second_token.startswith("lpt_")
        assert second_token != first_token
        assert command_fingerprint(second_envelope.payload) == first_fingerprint
        second_authorized = await restarted.artifacts.authorize(
            UUID(cast(str, second_transfer["transfer_id"])),
            second_token,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            agent_id=agent.id,
        )
        assert second_authorized.artifact_id == artifact.id
        await restarted.stop()

        dump = _database_dump(tmp_path / "control-plane.db")
        assert first_token not in dump
        assert second_token not in dump
        for database_file in tmp_path.glob("control-plane.db*"):
            data = database_file.read_bytes()
            assert first_token.encode() not in data
            assert second_token.encode() not in data

    asyncio.run(scenario())
