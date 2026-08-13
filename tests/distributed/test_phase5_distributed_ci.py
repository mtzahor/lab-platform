from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Coroutine, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiCreateRequest,
    DistributedCiSessionService,
    DistributedCiStartRequest,
)
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowDispatch,
    DistributedWorkflowRequest,
)
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import CiSessionConflictError, PermissionDeniedError
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    ActorContext,
    AgentRecord,
    AgentStatus,
    AuthenticationContext,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Principal,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationOwner,
    ReservationSource,
    ReservationStatus,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    WorkflowDefinition,
)
from lab_platform.persistence import SQLiteCiSessionRepository, SQLiteDatabase
from lab_platform.persistence.distributed import SQLiteRemoteArtifactRepository
from lab_platform.persistence.distributed_adapters import SQLiteRemoteCommandServiceRepository

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _run_async_test(
    function: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., None]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeCatalog:
    def __init__(self, *definitions: WorkflowDefinition) -> None:
        self.definitions = {(item.name, item.version): item for item in definitions}

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None:
        matches = [
            item
            for (stored_name, _), item in self.definitions.items()
            if stored_name == name
            and (version is None or item.version == version)
            and (organisation_id is None or item.organisation_id == organisation_id)
        ]
        return max(matches, key=lambda item: item.version) if matches else None


class ScopedAuthorisationRepository:
    def __init__(self, assignments: Sequence[RoleAssignment]) -> None:
        self.assignments = tuple(assignments)

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        del organisation_id, user_id
        return None

    async def list_team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]:
        del organisation_id, user_id
        return ()

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]],
    ) -> Sequence[RoleAssignment]:
        return tuple(
            assignment
            for assignment in self.assignments
            if assignment.organisation_id == organisation_id
            and (assignment.subject_type, assignment.subject_id) in subjects
        )


class FakeCoordinator:
    def __init__(self, dispatch: DistributedWorkflowDispatch) -> None:
        self.dispatch = dispatch
        self.calls: list[DistributedWorkflowRequest] = []

    async def run(self, request: DistributedWorkflowRequest) -> DistributedWorkflowDispatch:
        self.calls.append(request)
        return replace(
            self.dispatch,
            definition=request.definition,
            inputs=dict(request.inputs),
        )


class MutableRemoteWork:
    def __init__(self, command: RemoteCommand, operation: DistributedOperation) -> None:
        self.commands = {command.id: command}
        self.operations = {command.id: operation}

    async def get_command(self, command_id: UUID) -> RemoteCommand | None:
        return self.commands.get(command_id)

    async def get_operation_for_command(self, command_id: UUID) -> DistributedOperation | None:
        return self.operations.get(command_id)


class FakeReservations:
    def __init__(self, record: CoordinatedReservationLease, clock: MutableClock) -> None:
        self.record = record
        self.clock = clock
        self.renew_calls = 0
        self.release_calls = 0
        self.fail_release = False

    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease:
        assert reservation_id == self.record.reservation.id
        return self.record

    async def renew(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
        lease_ttl_seconds: int | None = None,
    ) -> CoordinatedReservationLease:
        del idempotency_key, lease_ttl_seconds
        assert reservation_id == self.record.reservation.id
        assert owner == self.record.reservation.owner
        assert expected_lease_version == self.record.lease.lease_version
        self.renew_calls += 1
        self.record = CoordinatedReservationLease(
            reservation=self.record.reservation,
            lease=self.record.lease.model_copy(
                update={
                    "valid_from": self.clock.now,
                    "valid_until": self.clock.now + timedelta(minutes=10),
                    "lease_version": self.record.lease.lease_version + 1,
                }
            ),
            state=ReservationLeaseState.ACTIVE,
            revision=self.record.revision + 1,
        )
        return self.record

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
    ) -> CoordinatedReservationLease:
        del idempotency_key
        assert reservation_id == self.record.reservation.id
        assert owner == self.record.reservation.owner
        assert expected_lease_version == self.record.lease.lease_version
        self.release_calls += 1
        if self.fail_release:
            raise RuntimeError("injected release failure")
        self.record = CoordinatedReservationLease(
            reservation=self.record.reservation.model_copy(
                update={
                    "status": ReservationStatus.RELEASED,
                    "released_at": self.clock.now,
                }
            ),
            lease=self.record.lease.model_copy(update={"released_at": self.clock.now}),
            state=ReservationLeaseState.RELEASED,
            revision=self.record.revision + 1,
        )
        return self.record


class FakeCommands:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.calls: list[dict[str, object]] = []
        self.artifact_upload_calls: list[tuple[UUID, UUID]] = []

    async def request_cancel(
        self,
        command_id: UUID,
        *,
        reason: str | None = None,
    ) -> RemoteCommand:
        self.calls.append({"command_id": command_id, "reason": reason})
        return RemoteCommand(
            id=command_id,
            agent_id=UUID(int=1),
            bench_id="agent-one/esp32-01",
            command_type=RemoteCommandType.RUN_WORKFLOW,
            created_at=self.clock.now,
            expires_at=self.clock.now + timedelta(hours=1),
            idempotency_key="seed-command",
        )

    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> object:
        self.artifact_upload_calls.append((agent_id, artifact_id))
        return None


def _workflow(name: str = "distributed-ci") -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": name,
            "version": 2,
            "requirements": {
                "capabilities": ["reset"],
                "labels": {"board": "esp32"},
            },
            "steps": [{"action": "reset"}],
        }
    )


async def _seed_distributed_route(
    database: SQLiteDatabase,
    definition: WorkflowDefinition,
) -> tuple[
    DistributedWorkflowDispatch,
    MutableRemoteWork,
    CoordinatedReservationLease,
]:
    agent = AgentRecord(
        id=UUID(int=1),
        slug="agent-one",
        name="Agent One",
        status=AgentStatus.ONLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location="jerusalem",
        labels={"pool": "ci"},
        registered_at=NOW - timedelta(days=1),
        last_connected_at=NOW - timedelta(minutes=1),
        last_seen_at=NOW,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )
    bench = GlobalBenchRecord(
        id="agent-one/esp32-01",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="esp32-01",
        name="ESP32 01",
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"reset", "diagnostic"}),
        labels={"board": "esp32", "rack": "preferred"},
        last_seen_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )
    reservation = Reservation(
        id=UUID(int=2),
        bench_id=bench.id,
        owner="ci/build",
        created_at=NOW,
        requested_at=NOW,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        activated_at=NOW,
        status=ReservationStatus.ACTIVE,
        source=ReservationSource.CI,
        idempotency_key="seed-reservation",
    )
    lease = ReservationLease(
        reservation_id=reservation.id,
        agent_id=agent.id,
        bench_id=bench.id,
        owner=reservation.owner,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        lease_version=1,
    )
    coordinated = CoordinatedReservationLease(
        reservation=reservation,
        lease=lease,
        state=ReservationLeaseState.ACTIVE,
        revision=2,
    )
    command = RemoteCommand(
        id=UUID(int=3),
        agent_id=agent.id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={"definition": definition.model_dump(mode="json"), "inputs": {}},
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="seed-command",
        reservation_id=reservation.id,
        lease_version=lease.lease_version,
    )
    operation = DistributedOperation(
        id=UUID(int=4),
        remote_command_id=command.id,
        agent_id=agent.id,
        bench_id=bench.id,
        reservation_id=reservation.id,
        operation_type="RUN_WORKFLOW",
        created_at=NOW,
    )
    command = command.model_copy(update={"operation_id": operation.id})
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, location, labels_json, "
            "registered_at, last_connected_at, last_seen_at, disconnected_at, "
            "certificate_fingerprint, enrollment_status, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)",
            (
                str(agent.id),
                agent.slug,
                agent.name,
                agent.status.value,
                agent.version,
                agent.protocol_version,
                agent.location,
                '{"pool":"ci"}',
                agent.registered_at.isoformat(),
                agent.last_connected_at.isoformat() if agent.last_connected_at else None,
                agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                agent.enrollment_status.value,
            ),
        )
        connection.execute(
            "INSERT INTO global_benches "
            "(id, agent_id, agent_slug, local_bench_id, name, backend_id, kind, target_type, "
            "status, health, capabilities_json, labels_json, firmware_version, last_seen_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, "
            "?, ?)",
            (
                bench.id,
                str(agent.id),
                agent.slug,
                bench.local_bench_id,
                bench.name,
                bench.backend_id,
                bench.kind.value,
                bench.status.value,
                bench.health.value,
                '["diagnostic","reset"]',
                '{"board":"esp32","rack":"preferred"}',
                NOW.isoformat(),
                bench.created_at.isoformat(),
                bench.updated_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO reservations "
            "(id, bench_id, owner, created_at, released_at, status, requested_at, starts_at, "
            "ends_at, activated_at, expired_at, source, metadata, idempotency_key, "
            "release_pending) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, '{}', ?, 0)",
            (
                str(reservation.id),
                reservation.bench_id,
                reservation.owner,
                reservation.created_at.isoformat(),
                reservation.status.value,
                reservation.requested_at.isoformat() if reservation.requested_at else None,
                reservation.starts_at.isoformat() if reservation.starts_at else None,
                reservation.ends_at.isoformat() if reservation.ends_at else None,
                reservation.activated_at.isoformat() if reservation.activated_at else None,
                reservation.source.value,
                reservation.idempotency_key,
            ),
        )
    persistent_remote = SQLiteRemoteCommandServiceRepository(database)
    await persistent_remote.create_bundle(command, operation)
    remote = MutableRemoteWork(command, operation)
    return (
        DistributedWorkflowDispatch(
            agent=agent,
            bench=bench,
            definition=definition,
            inputs={},
            reservation=coordinated,
            artifact_transfers=(),
            command=command,
            operation=operation,
        ),
        remote,
        coordinated,
    )


async def _service_fixture(
    path: Path,
    *,
    clock: MutableClock | None = None,
) -> tuple[
    SQLiteDatabase,
    SQLiteCiSessionRepository,
    DistributedCiSessionService,
    FakeCoordinator,
    MutableRemoteWork,
    FakeReservations,
    FakeCommands,
]:
    database = SQLiteDatabase(path)
    database.initialize()
    definition = _workflow()
    dispatch, remote, record = await _seed_distributed_route(database, definition)
    selected_clock = clock or MutableClock()
    coordinator = FakeCoordinator(dispatch)
    reservations = FakeReservations(record, selected_clock)
    commands = FakeCommands(selected_clock)
    repository = SQLiteCiSessionRepository(database)
    service = DistributedCiSessionService(
        repository,
        FakeCatalog(definition, _workflow("another-workflow")),
        coordinator,
        remote,
        reservations,
        commands,
        SQLiteRemoteArtifactRepository(database),
        commands,
        clock=selected_clock,
        heartbeat_timeout_seconds=300,
    )
    return database, repository, service, coordinator, remote, reservations, commands


@_run_async_test
async def test_create_is_durable_idempotent_and_rejects_content_reuse(tmp_path: Path) -> None:
    database, _, service, _, _, _, _ = await _service_fixture(tmp_path / "ci.db")
    request = DistributedCiCreateRequest(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="run-42",
        requested_by="ci/build",
        bench_request=BenchRequest(required_labels={"board": "esp32"}),
        idempotency_key="run-42",
    )
    first = await service.create(request)
    assert first.status is CiSessionStatus.WAITING_FOR_BENCH
    assert await service.create(request) == first
    with pytest.raises(CiSessionConflictError):
        await service.create(
            replace(
                request,
                bench_request=BenchRequest(required_labels={"board": "stm32"}),
            )
        )
    database.close()

    reopened = SQLiteDatabase(tmp_path / "ci.db")
    reopened.initialize()
    sessions = await SQLiteCiSessionRepository(reopened).list(limit=10)
    assert sessions == [first]
    reopened.close()


@_run_async_test
async def test_identity_ci_service_reauthorises_scoped_principal_and_narrowing(
    tmp_path: Path,
) -> None:
    database, _, service, coordinator, _, _, _ = await _service_fixture(tmp_path / "ci.db")
    definition = _workflow()
    principal = Principal(
        id=UUID(int=901),
        type=PrincipalType.SERVICE_ACCOUNT,
        organisation_id=definition.organisation_id,
        display_name="scoped-ci",
    )
    assignment = RoleAssignment(
        organisation_id=definition.organisation_id,
        subject_type=RoleSubjectType.SERVICE_ACCOUNT,
        subject_id=principal.id,
        role=RoleName.WORKFLOW_RUNNER,
        resource_type=ResourceType.WORKFLOW,
        resource_id=definition.name,
        created_by=principal.id,
        created_at=NOW - timedelta(minutes=1),
    )
    service.set_authorisation_service(
        AuthorisationService(
            ScopedAuthorisationRepository((assignment,)),
            clock=lambda: NOW,
        )
    )
    context = AuthenticationContext(principal=principal)
    owner = ReservationOwner(
        principal_id=principal.id,
        principal_type=principal.type,
        display_name=principal.display_name,
    )
    actor = ActorContext(
        principal_id=principal.id,
        principal_type=principal.type,
        display_name=principal.display_name,
        organisation_id=principal.organisation_id,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="scoped-run",
            requested_by=principal.display_name,
            owner_principal=owner,
            actor_context=actor,
            authentication_context=context,
            idempotency_key="scoped-run",
        )
    )

    other_context = AuthenticationContext(
        principal=principal.model_copy(update={"id": UUID(int=902), "display_name": "other-ci"})
    )
    with pytest.raises(PermissionDeniedError):
        await service.start(
            session.id,
            DistributedCiStartRequest(workflow_name=definition.name),
            organisation_id=principal.organisation_id,
            authentication_context=other_context,
        )

    started = await service.start(
        session.id,
        DistributedCiStartRequest(workflow_name=definition.name),
        organisation_id=principal.organisation_id,
        authentication_context=context,
    )
    assert started.requested_by_principal_id == principal.id
    assert coordinator.calls[-1].authentication_context == context
    assert coordinator.calls[-1].organisation_id == principal.organisation_id
    assert not coordinator.calls[-1].allow_legacy_authorisation

    with pytest.raises(PermissionDeniedError):
        await service.create(
            DistributedCiCreateRequest(
                provider=CiProvider.GITHUB_ACTIONS,
                external_run_id="narrowed-run",
                requested_by=principal.display_name,
                owner_principal=owner,
                actor_context=actor,
                authentication_context=context.model_copy(
                    update={"permission_restrictions": {"workflows:run"}}
                ),
                idempotency_key="narrowed-run",
            )
        )
    database.close()


@_run_async_test
async def test_start_routes_bench_request_and_atomically_persists_remote_binding(
    tmp_path: Path,
) -> None:
    database, repository, service, coordinator, _, _, _ = await _service_fixture(tmp_path / "ci.db")
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="route-1",
            requested_by="ci/build",
            bench_request=BenchRequest(
                required_capabilities={"diagnostic"},
                required_labels={"board": "esp32"},
                preferred_labels={"rack": "preferred"},
                required_agent_labels={"pool": "ci"},
                preferred_location="jerusalem",
                allow_simulated=False,
                allow_physical=True,
                reservation_duration_seconds=900,
            ),
        )
    )
    start = DistributedCiStartRequest(
        workflow_name="distributed-ci",
        location="jerusalem",
        agent_labels={"pool": "ci"},
    )
    running = await service.start(session.id, start)
    assert running.status is CiSessionStatus.RUNNING
    assert running.workflow_run_id is None
    assert running.bench_id == "agent-one/esp32-01"
    assert len(coordinator.calls) == 1
    routed = coordinator.calls[0]
    assert routed.bench_id is None
    assert routed.kind is GlobalBenchKind.PHYSICAL
    assert routed.bench_labels == {"board": "esp32"}
    assert routed.preferred_bench_labels == {"rack": "preferred"}
    assert routed.agent_labels == {"pool": "ci"}
    assert routed.preferred_location == "jerusalem"
    assert routed.manage_reservation_lifecycle is False
    assert set(routed.definition.requirements.capabilities) == {"reset"}
    assert set(routed.required_capabilities) == {"diagnostic"}
    binding = await repository.get_distributed_workflow(session.id)
    assert binding is not None
    assert binding.remote_command_id == UUID(int=3)
    assert binding.operation_id == UUID(int=4)
    assert len(binding.request_fingerprint) == 64

    assert await service.start(session.id, start) == running
    assert len(coordinator.calls) == 1
    with pytest.raises(CiSessionConflictError):
        await service.start(
            session.id,
            DistributedCiStartRequest(workflow_name="another-workflow"),
        )
    database.close()


@_run_async_test
async def test_authenticated_ci_identity_survives_restart_and_reaches_workflow(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database, _, service, coordinator, remote, reservations, commands = await _service_fixture(
        tmp_path / "ci-identity.db",
        clock=clock,
    )
    principal_id = UUID(int=800)
    organisation_id = UUID(int=801)
    owner = ReservationOwner(
        principal_id=principal_id,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
        display_name="github-ci",
    )
    actor = ActorContext(
        principal_id=principal_id,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
        display_name="github-ci",
        organisation_id=organisation_id,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="authenticated-run",
            requested_by="github-ci",
            owner_principal=owner,
            actor_context=actor,
            idempotency_key="authenticated-run",
        )
    )
    assert session.organisation_id == organisation_id
    assert session.requested_by_principal_id == principal_id
    assert session.requested_by_principal_type is PrincipalType.SERVICE_ACCOUNT
    database.close()

    reopened = SQLiteDatabase(tmp_path / "ci-identity.db")
    reopened.initialize()
    restarted = DistributedCiSessionService(
        SQLiteCiSessionRepository(reopened),
        FakeCatalog(_workflow(), _workflow("another-workflow")),
        coordinator,
        remote,
        reservations,
        commands,
        SQLiteRemoteArtifactRepository(reopened),
        commands,
        clock=clock,
        heartbeat_timeout_seconds=300,
    )
    await restarted.start(
        session.id,
        DistributedCiStartRequest(workflow_name="distributed-ci"),
    )
    assert len(coordinator.calls) == 1
    routed = coordinator.calls[0]
    assert routed.owner == "github-ci"
    assert routed.owner_principal == owner
    assert routed.actor_context == actor
    reopened.close()


@_run_async_test
async def test_ci_rejects_partial_or_mismatched_authenticated_identity(tmp_path: Path) -> None:
    database, _, service, _, _, _, _ = await _service_fixture(tmp_path / "ci-identity.db")
    owner = ReservationOwner(
        principal_id=UUID(int=810),
        principal_type=PrincipalType.USER,
        display_name="Alice",
    )
    with pytest.raises(CiSessionConflictError, match="supplied together"):
        await service.create(
            DistributedCiCreateRequest(
                provider=CiProvider.LOCAL,
                external_run_id="partial-identity",
                requested_by="Alice",
                owner_principal=owner,
            )
        )
    with pytest.raises(CiSessionConflictError, match="do not match"):
        await service.create(
            DistributedCiCreateRequest(
                provider=CiProvider.LOCAL,
                external_run_id="mismatched-identity",
                requested_by="Alice",
                owner_principal=owner,
                actor_context=ActorContext(
                    principal_id=UUID(int=811),
                    principal_type=PrincipalType.USER,
                    display_name="Alice",
                    organisation_id=UUID(int=812),
                ),
            )
        )
    database.close()


@_run_async_test
async def test_terminal_remote_result_releases_lease_and_survives_restart(tmp_path: Path) -> None:
    database, _, service, _, remote, reservations, _ = await _service_fixture(tmp_path / "ci.db")
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.GITLAB_CI,
            external_run_id="terminal-1",
            requested_by="ci/build",
        )
    )
    running = await service.start(
        session.id,
        DistributedCiStartRequest(workflow_name="distributed-ci"),
    )
    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={
            "status": DistributedOperationStatus.SUCCEEDED,
            "completed_at": NOW + timedelta(seconds=1),
        }
    )
    assert (await service.get(running.id)).status is CiSessionStatus.SUCCEEDED
    completed = await service.cleanup(running.id)
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.SUCCEEDED
    assert reservations.release_calls == 1
    assert (await service.cleanup(running.id)) == completed
    database.close()

    reopened = SQLiteDatabase(tmp_path / "ci.db")
    reopened.initialize()
    stored = await SQLiteCiSessionRepository(reopened).get(running.id)
    assert stored == completed
    reopened.close()


@_run_async_test
async def test_successful_cleanup_waits_for_remote_artifact_upload_and_recovers(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database, repository, service, _, remote, reservations, commands = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="artifact-race",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={"status": DistributedOperationStatus.SUCCEEDED, "completed_at": clock.now}
    )
    assert (await service.get(session.id)).status is CiSessionStatus.SUCCEEDED

    artifacts = SQLiteRemoteArtifactRepository(database)
    artifact = await artifacts.create(
        RemoteArtifactMetadata(
            id=UUID(int=50),
            agent_id=UUID(int=1),
            local_artifact_id=UUID(int=51),
            command_id=UUID(int=3),
            operation_id=UUID(int=4),
            name="junit.xml",
            artifact_type="junit_xml",
            content_type="application/xml",
            size_bytes=12,
            sha256="a" * 64,
            created_at=clock.now,
        )
    )

    pending = await service.cleanup(session.id)
    assert pending.status is CiSessionStatus.CLEANUP_PENDING
    assert pending.cleanup_status.value == "pending"
    assert reservations.release_calls == 1
    cleanup = await repository.get_cleanup(session.id)
    assert cleanup is not None and cleanup.artifacts_finalized is False
    assert commands.artifact_upload_calls == [(artifact.agent_id, artifact.id)]

    uploaded_at = clock.now + timedelta(seconds=1)
    assert (
        await artifacts.update_uploaded(
            artifact.id,
            uploaded_at,
            expected_uploaded_at=None,
        )
    ) is not None
    restarted = DistributedCiSessionService(
        repository,
        service._workflow_catalog,  # noqa: SLF001 - verifies restart recovery
        service._workflows,  # noqa: SLF001 - verifies restart recovery
        remote,
        reservations,
        commands,
        artifacts,
        commands,
        clock=clock,
        artifact_finalization_timeout_seconds=30,
    )
    result = await restarted.recover_incomplete()
    completed = await restarted.get(session.id, synchronize=False)
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.SUCCEEDED
    assert completed.cleanup_status.value == "succeeded"
    cleanup = await repository.get_cleanup(session.id)
    assert cleanup is not None and cleanup.artifacts_finalized is True
    database.close()


@_run_async_test
async def test_remote_artifact_finalization_timeout_is_bounded_and_fails_cleanup(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database, repository, service, _, remote, _, commands = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="artifact-timeout",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={"status": DistributedOperationStatus.SUCCEEDED, "completed_at": clock.now}
    )
    await service.get(session.id)
    await SQLiteRemoteArtifactRepository(database).create(
        RemoteArtifactMetadata(
            id=UUID(int=60),
            agent_id=UUID(int=1),
            local_artifact_id=UUID(int=61),
            command_id=UUID(int=3),
            operation_id=UUID(int=4),
            name="serial.log",
            artifact_type="serial_log",
            size_bytes=3,
            sha256="b" * 64,
            created_at=clock.now,
        )
    )
    assert (await service.cleanup(session.id)).status is CiSessionStatus.CLEANUP_PENDING

    clock.now += timedelta(seconds=301)
    result = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.INFRASTRUCTURE_ERROR
    assert completed.cleanup_status.value == "failed"
    cleanup = await repository.get_cleanup(session.id)
    assert cleanup is not None and cleanup.artifacts_finalized is False
    assert cleanup.errors == [
        "artifact finalization timed out: 1 remote artifact upload(s) incomplete"
    ]
    database.close()


@_run_async_test
async def test_cancel_waits_for_agent_confirmation_then_maintenance_finalizes(
    tmp_path: Path,
) -> None:
    database, _, service, _, remote, reservations, commands = await _service_fixture(
        tmp_path / "ci.db"
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.JENKINS,
            external_run_id="cancel-1",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    requested = await service.cancel(session.id)
    assert requested.status is CiSessionStatus.CANCEL_REQUESTED
    assert requested.outcome is CiOutcome.CANCELLED
    assert commands.calls == [
        {
            "command_id": UUID(int=3),
            "reason": "CI session cancelled",
        }
    ]
    assert reservations.release_calls == 0

    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={
            "status": DistributedOperationStatus.CANCELLED,
            "completed_at": NOW + timedelta(seconds=1),
        }
    )
    result = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.CANCELLED
    assert reservations.release_calls == 1
    database.close()


@_run_async_test
async def test_heartbeat_renews_short_lease_and_cleanup_retries(tmp_path: Path) -> None:
    clock = MutableClock()
    database, _, service, _, remote, reservations, _ = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    reservations.record = CoordinatedReservationLease(
        reservation=reservations.record.reservation,
        lease=reservations.record.lease.model_copy(
            update={"valid_until": NOW + timedelta(seconds=30)}
        ),
        state=ReservationLeaseState.ACTIVE,
        revision=reservations.record.revision,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="heartbeat-1",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    clock.now += timedelta(seconds=5)
    heartbeat = await service.heartbeat(session.id)
    assert heartbeat.heartbeat_at == clock.now
    assert reservations.renew_calls == 1

    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={
            "status": DistributedOperationStatus.FAILED,
            "completed_at": clock.now,
        }
    )
    failed = await service.get(session.id)
    reservations.fail_release = True
    pending = await service.cleanup(failed.id)
    assert pending.status is CiSessionStatus.CLEANUP_PENDING
    assert pending.cleanup_status.value == "failed"
    reservations.fail_release = False
    completed = await service.cleanup(failed.id)
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.FAILED
    database.close()


@_run_async_test
async def test_500_queued_sessions_survive_restart_and_are_maintained_in_one_batch(
    tmp_path: Path,
) -> None:
    database, _, service, coordinator, _, _, _ = await _service_fixture(tmp_path / "ci.db")
    sessions = await asyncio.gather(
        *(
            service.create(
                DistributedCiCreateRequest(
                    provider=CiProvider.GITHUB_ACTIONS,
                    external_run_id=f"queued-{index}",
                    requested_by="ci/build",
                    idempotency_key=f"queued-{index}",
                    timeout_seconds=3_600,
                )
            )
            for index in range(500)
        )
    )
    assert len({session.id for session in sessions}) == 500
    database.close()

    reopened = SQLiteDatabase(tmp_path / "ci.db")
    reopened.initialize()
    repository = SQLiteCiSessionRepository(reopened)
    assert len(await repository.list(status=CiSessionStatus.WAITING_FOR_BENCH, limit=600)) == 500
    maintenance_service = DistributedCiSessionService(
        repository,
        service._workflow_catalog,  # noqa: SLF001 - verifies restart composition
        coordinator,
        service._remote_work,  # noqa: SLF001 - test fixture port
        service._reservations,  # noqa: SLF001 - test fixture port
        service._commands,  # noqa: SLF001 - test fixture port
        service._artifacts,  # noqa: SLF001 - verifies restart composition
        service._artifact_uploads,  # noqa: SLF001 - verifies restart composition
        clock=MutableClock(),
        maintenance_batch_size=600,
    )
    result = await maintenance_service.recover_incomplete()
    assert result.examined == 500
    assert result.timed_out == 0
    assert coordinator.calls == []
    reopened.close()


@_run_async_test
async def test_waiting_session_expiry_completes_without_allocating_a_bench(tmp_path: Path) -> None:
    clock = MutableClock()
    database, _, service, coordinator, _, reservations, _ = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="wait-timeout",
            requested_by="ci/build",
            bench_request=BenchRequest(maximum_wait_seconds=10),
            timeout_seconds=100,
        )
    )
    clock.now += timedelta(seconds=11)
    result = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert result.timed_out == 1
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.TIMED_OUT
    assert reservations.release_calls == 0
    assert coordinator.calls == []
    database.close()


@_run_async_test
async def test_unknown_remote_operation_waits_for_reconciliation_deadline(tmp_path: Path) -> None:
    clock = MutableClock()
    database, _, service, _, remote, reservations, _ = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="unknown-1",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={
            "status": DistributedOperationStatus.UNKNOWN,
            "reconciliation_deadline": NOW + timedelta(seconds=10),
        }
    )
    assert (await service.get(session.id)).status is CiSessionStatus.RUNNING
    assert reservations.release_calls == 0

    clock.now += timedelta(seconds=11)
    result = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert result.synchronized == 1
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.INFRASTRUCTURE_ERROR
    errors = (await service.details(session.id))["errors"]
    assert isinstance(errors, list)
    assert "REMOTE_OPERATION_RECONCILIATION_TIMEOUT" in errors
    assert reservations.release_calls == 1
    database.close()


@_run_async_test
async def test_queued_cancel_is_immediately_safe_and_idempotent(tmp_path: Path) -> None:
    database, _, service, coordinator, _, reservations, commands = await _service_fixture(
        tmp_path / "ci.db"
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.UNKNOWN,
            external_run_id="queued-cancel",
            requested_by="ci/build",
        )
    )
    completed = await service.cancel(session.id)
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.CANCELLED
    assert await service.cancel(session.id) == completed
    assert await service.heartbeat(session.id) == completed
    details = await service.details(session.id)
    assert details["distributed"] is True
    assert details["cleanup"] == {
        "reservation_released": True,
        "workflow_stopped": True,
        "locks_released": True,
        "serial_closed": True,
        "artifacts_finalized": True,
        "errors": [],
    }
    assert coordinator.calls == []
    assert reservations.release_calls == 0
    assert commands.calls == []
    database.close()


@_run_async_test
async def test_validation_rejects_unknown_workflow_conflicting_labels_and_bad_bounds(
    tmp_path: Path,
) -> None:
    database, _, service, _, _, _, _ = await _service_fixture(tmp_path / "ci.db")
    with pytest.raises(ValueError):
        await service.create(
            DistributedCiCreateRequest(
                provider=CiProvider.LOCAL,
                external_run_id="invalid-timeout",
                requested_by="ci/build",
                timeout_seconds=0,
            )
        )
    with pytest.raises(CiSessionConflictError):
        await service.create(
            DistributedCiCreateRequest(
                provider=CiProvider.LOCAL,
                external_run_id="invalid-timeout",
                requested_by="ci/build",
                timeout_seconds=100_000,
            )
        )
    with pytest.raises(ValueError):
        await service.list(limit=0)

    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="validation",
            requested_by="ci/build",
            bench_request=BenchRequest(required_agent_labels={"pool": "ci"}),
        )
    )
    with pytest.raises(WorkflowInvalidError):
        await service.start(
            session.id,
            DistributedCiStartRequest(workflow_name="missing"),
        )
    with pytest.raises(CiSessionConflictError):
        await service.start(
            session.id,
            DistributedCiStartRequest(
                workflow_name="distributed-ci",
                agent_labels={"pool": "development"},
            ),
        )
    database.close()


@_run_async_test
async def test_running_timeout_requests_remote_cancel_and_preserves_timeout_outcome(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database, _, service, _, remote, reservations, commands = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="running-timeout",
            requested_by="ci/build",
            timeout_seconds=10,
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    clock.now += timedelta(seconds=11)
    first = await service.process_maintenance()
    pending = await service.get(session.id, synchronize=False)
    assert first.timed_out == 1
    assert pending.status is CiSessionStatus.CANCEL_REQUESTED
    assert pending.outcome is CiOutcome.TIMED_OUT
    assert pending.cancel_actor_context is None
    assert pending.cancel_authorisation_snapshot_id is None
    assert commands.calls[-1] == {
        "command_id": UUID(int=3),
        "reason": "CI session timed out",
    }
    assert reservations.release_calls == 0

    operation = remote.operations[UUID(int=3)]
    remote.operations[UUID(int=3)] = operation.model_copy(
        update={
            "status": DistributedOperationStatus.CANCELLED,
            "completed_at": clock.now,
        }
    )
    second = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert second.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.TIMED_OUT
    database.close()


@_run_async_test
async def test_terminal_lease_ends_unconfirmed_cancel_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database, _, service, _, _, reservations, _ = await _service_fixture(
        tmp_path / "ci.db",
        clock=clock,
    )
    session = await service.create(
        DistributedCiCreateRequest(
            provider=CiProvider.LOCAL,
            external_run_id="ownership-ended",
            requested_by="ci/build",
        )
    )
    await service.start(session.id, DistributedCiStartRequest(workflow_name="distributed-ci"))
    assert (await service.cancel(session.id)).status is CiSessionStatus.CANCEL_REQUESTED
    await reservations.release(
        reservations.record.reservation.id,
        owner="ci/build",
        expected_lease_version=reservations.record.lease.lease_version,
        idempotency_key="external-release",
    )
    result = await service.process_maintenance()
    completed = await service.get(session.id, synchronize=False)
    assert result.finalized == 1
    assert completed.status is CiSessionStatus.COMPLETED
    assert completed.outcome is CiOutcome.INFRASTRUCTURE_ERROR
    errors = (await service.details(session.id))["errors"]
    assert isinstance(errors, list)
    assert "REMOTE_OPERATION_STATE_UNKNOWN" in errors
    database.close()
