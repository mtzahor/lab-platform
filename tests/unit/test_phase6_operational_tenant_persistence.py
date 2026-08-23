from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from lab_platform.control_plane_core.reservations import ReservationGrantRequest
from lab_platform.models import (
    LEGACY_ORGANISATION_ID,
    ArtifactOwnerType,
    ArtifactRecord,
    CiProvider,
    CiSession,
    DistributedOperation,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandType,
    Reservation,
    ReservationStatus,
    WorkflowDefinition,
)
from lab_platform.models.agents import AgentStatus
from lab_platform.persistence import (
    SQLiteAgentEnrollmentRepository,
    SQLiteCentralReservationLeaseRepository,
    SQLiteCiSessionRepository,
    SQLiteDatabase,
    SQLiteGenericArtifactRepository,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.distributed import (
    SQLiteDistributedOperationRepository,
    SQLiteGlobalBenchRepository,
    SQLiteRemoteArtifactRepository,
    SQLiteRemoteCommandRepository,
)

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
SECOND_ORGANISATION_ID = UUID("10000000-0000-0000-0000-000000000002")


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO organisations (id, slug, name, status, created_at, updated_at) "
            "VALUES (?, 'second', 'Second Lab', 'ACTIVE', ?, ?)",
            (
                str(SECOND_ORGANISATION_ID),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    return database


def _seed_agent(database: SQLiteDatabase) -> UUID:
    agent_id = uuid4()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, organisation_id, slug, name, status, version, protocol_version, "
            "labels_json, registered_at, enrollment_status) "
            "VALUES (?, ?, 'second-agent', 'Second Agent', 'ONLINE', '0.7.0-alpha', "
            "'1.0', '{}', ?, 'ENROLLED')",
            (str(agent_id), str(SECOND_ORGANISATION_ID), NOW.isoformat()),
        )
    return agent_id


def test_distributed_resources_are_hydrated_and_filtered_by_organisation(
    tmp_path: Path,
) -> None:
    asyncio.run(_distributed_resources_are_hydrated_and_filtered(tmp_path))


async def _distributed_resources_are_hydrated_and_filtered(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "tenant-distributed.db")
    agent_id = _seed_agent(database)
    agents = SQLiteAgentEnrollmentRepository(database)
    assert await agents.get_agent(agent_id, organisation_id=LEGACY_ORGANISATION_ID) is None
    agent = await agents.get_agent(agent_id, organisation_id=SECOND_ORGANISATION_ID)
    assert agent is not None and agent.organisation_id == SECOND_ORGANISATION_ID

    bench = GlobalBenchRecord(
        id="second-agent/bench-1",
        organisation_id=SECOND_ORGANISATION_ID,
        agent_id=agent_id,
        agent_slug="second-agent",
        local_bench_id="bench-1",
        name="Second Bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        created_at=NOW,
        updated_at=NOW,
    )
    benches = SQLiteGlobalBenchRepository(database)
    assert await benches.upsert(bench) == bench
    assert await benches.get(bench.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await benches.list(organisation_id=SECOND_ORGANISATION_ID) == [bench]

    reservations = SQLiteCentralReservationLeaseRepository(database)
    result = await reservations.grant_if_eligible(
        ReservationGrantRequest(
            reservation=Reservation(
                id=uuid4(),
                bench_id=bench.id,
                owner="second-owner",
                status=ReservationStatus.SCHEDULED,
                created_at=NOW,
                starts_at=NOW,
                ends_at=NOW + timedelta(hours=1),
            ),
            agent_id=agent_id,
            lease_valid_until=NOW + timedelta(minutes=10),
        ),
        mutation_key="tenant-reservation",
        request_fingerprint="d" * 64,
        expected_agent_status=AgentStatus.ONLINE,
        expected_bench_status=GlobalBenchStatus.ONLINE,
    )
    assert result is not None
    reservation_id = result.record.reservation.id
    assert result.record.reservation.organisation_id == SECOND_ORGANISATION_ID
    assert await reservations.get(reservation_id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert len(await reservations.list(organisation_id=SECOND_ORGANISATION_ID)) == 1

    command = RemoteCommand(
        organisation_id=SECOND_ORGANISATION_ID,
        agent_id=agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RESET,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="tenant-command",
        created_at=NOW,
    )
    command_repository = SQLiteRemoteCommandRepository(database)
    await command_repository.create(command)
    operation = DistributedOperation(
        organisation_id=SECOND_ORGANISATION_ID,
        remote_command_id=command.id,
        agent_id=agent_id,
        bench_id=bench.id,
        operation_type="reset",
        created_at=NOW,
    )
    operation_repository = SQLiteDistributedOperationRepository(database)
    await operation_repository.create(operation)
    assert (
        await operation_repository.get(
            operation.id,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    assert await operation_repository.list(organisation_id=SECOND_ORGANISATION_ID) == [operation]

    reported = RemoteArtifactMetadata(
        agent_id=agent_id,
        local_artifact_id=uuid4(),
        command_id=command.id,
        name="serial.log",
        artifact_type="serial_log",
        content_type="text/plain",
        size_bytes=3,
        sha256="a" * 64,
        created_at=NOW,
    )
    remote_artifacts = SQLiteRemoteArtifactRepository(database)
    stored = await remote_artifacts.create(reported)
    assert stored.organisation_id == SECOND_ORGANISATION_ID
    assert await remote_artifacts.list(organisation_id=SECOND_ORGANISATION_ID) == [stored]
    assert (
        await remote_artifacts.get(
            stored.id,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    database.close()


def test_workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path: Path) -> None:
    asyncio.run(_workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path))


async def _workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path: Path) -> None:
    database = _database(tmp_path / "tenant-platform.db")
    workflow = WorkflowDefinition.model_validate(
        {
            "organisation_id": SECOND_ORGANISATION_ID,
            "name": "tenant-probe",
            "version": 1,
            "requirements": {"capabilities": ["probe"], "labels": {}},
            "steps": [{"action": "probe"}],
        }
    )
    workflows = SQLiteWorkflowRepository(database, initialize_schema=False)
    await workflows.save_definition(workflow)
    assert (
        await workflows.get_definition(
            workflow.name,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    assert await workflows.list_definitions(organisation_id=SECOND_ORGANISATION_ID) == [workflow]

    artifact = ArtifactRecord(
        organisation_id=SECOND_ORGANISATION_ID,
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=uuid4(),
        name="result.json",
        artifact_type="test_result",
        content_type="application/json",
        path="objects/result.json",
        size_bytes=2,
        sha256="b" * 64,
        created_at=NOW,
    )
    artifacts = SQLiteGenericArtifactRepository(database)
    await artifacts.save(artifact, idempotency_key="result")
    assert await artifacts.get(artifact.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await artifacts.list_for_owner(
        artifact.owner_type,
        artifact.owner_id,
        organisation_id=SECOND_ORGANISATION_ID,
    ) == [artifact]
    assert await artifacts.list_all(organisation_id=SECOND_ORGANISATION_ID) == [artifact]
    assert await artifacts.list_all(organisation_id=LEGACY_ORGANISATION_ID) == []

    session = CiSession(
        provider=CiProvider.LOCAL,
        external_run_id="second-run",
        requested_by="second-service",
        organisation_id=SECOND_ORGANISATION_ID,
        requested_by_principal_id=uuid4(),
        requested_by_principal_type=PrincipalType.SERVICE_ACCOUNT,
        created_at=NOW,
    )
    sessions = SQLiteCiSessionRepository(database)
    await sessions.create(session, idempotency_key="second-session")
    assert await sessions.get(session.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await sessions.list(organisation_id=SECOND_ORGANISATION_ID) == [session]
    database.close()
