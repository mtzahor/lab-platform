from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import (
    CommandAcceptedPayload,
    CommandCancelPayload,
    CommandRejectedPayload,
    CommandRequestPayload,
    MessageType,
    OperationEventPayload,
)
from lab_platform.control_plane_core.commands import (
    CommandDeliveryReceipt,
    InMemoryRemoteCommandRepository,
    RemoteCommandService,
)
from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    RemoteCommandDeliveryFailedError,
    RemoteCommandDuplicateError,
    RemoteCommandExpiredError,
    RemoteCommandNotFoundError,
)
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import AuthenticationRequiredError, PermissionDeniedError
from lab_platform.models import (
    ActorContext,
    AgentRecord,
    AgentStatus,
    AuditEvent,
    AuthenticationContext,
    AuthorisationSnapshot,
    DistributedCiWorkflowBinding,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationMembership,
    Principal,
    PrincipalType,
    RemoteCommand,
    RemoteCommandAttempt,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    WorkflowDefinition,
)

NOW = datetime(2026, 7, 29, 9, tzinfo=UTC)
AGENT_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_AGENT_ID = UUID("10000000-0000-0000-0000-000000000002")
CONNECTION_ID = UUID("20000000-0000-0000-0000-000000000001")
RESERVATION_ID = UUID("30000000-0000-0000-0000-000000000001")
BENCH_ID = "home-lab/esp32-01"
PHASE6_PRINCIPAL_ID = UUID(int=60_001)


class ScopedAuthorisationRepository:
    def __init__(self, assignments: Sequence[RoleAssignment]) -> None:
        self.assignments = tuple(assignments)

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None:
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


class InMemoryAuthorisationSnapshotRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[UUID, UUID], AuthorisationSnapshot] = {}

    async def create_authorisation_snapshot(
        self,
        organisation_id: UUID,
        snapshot: AuthorisationSnapshot,
    ) -> AuthorisationSnapshot:
        self.records[(organisation_id, snapshot.id)] = snapshot
        return snapshot

    async def get_authorisation_snapshot(
        self,
        organisation_id: UUID,
        snapshot_id: UUID,
    ) -> AuthorisationSnapshot | None:
        return self.records.get((organisation_id, snapshot_id))


class InMemoryWorkflowDefinitionCatalog:
    def __init__(self, *definitions: WorkflowDefinition) -> None:
        self.definitions = {
            (definition.organisation_id, definition.name, definition.version): definition
            for definition in definitions
        }

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None:
        assert version is not None
        assert organisation_id is not None
        return self.definitions.get((organisation_id, name, version))


class InMemoryCiCancellationBindings:
    def __init__(self, binding: DistributedCiWorkflowBinding) -> None:
        self.binding = binding

    async def get_distributed_workflow(
        self,
        session_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedCiWorkflowBinding | None:
        if session_id != self.binding.ci_session_id or organisation_id != _bench().organisation_id:
            return None
        return self.binding


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: float) -> datetime:
        self.value += timedelta(**values)
        return self.value


class FakeDirectory:
    def __init__(
        self,
        agent: AgentRecord | None = None,
        bench: GlobalBenchRecord | None = None,
    ) -> None:
        self.agent = agent
        self.bench = bench

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        if self.agent is None or self.agent.id != agent_id:
            return None
        return self.agent

    async def get_bench(self, bench_id: str) -> GlobalBenchRecord | None:
        if self.bench is None or self.bench.id != bench_id:
            return None
        return self.bench


class FakeTransport:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.connected = True
        self.error: Exception | None = None
        self.sent: list[CommandRequestPayload] = []
        self.cancelled: list[CommandCancelPayload] = []

    async def is_connected(self, agent_id: UUID) -> bool:
        assert agent_id == AGENT_ID
        return self.connected

    async def send_command(
        self,
        agent_id: UUID,
        payload: CommandRequestPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        assert agent_id == AGENT_ID
        assert correlation_id == payload.command.id
        if self.error is not None:
            raise self.error
        self.sent.append(payload)
        return CommandDeliveryReceipt(
            connection_id=CONNECTION_ID,
            sequence_number=len(self.sent),
            dispatched_at=self.clock(),
        )

    async def send_cancel(
        self,
        agent_id: UUID,
        payload: CommandCancelPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        assert agent_id == AGENT_ID
        assert correlation_id == payload.command_id
        self.cancelled.append(payload)
        return CommandDeliveryReceipt(
            connection_id=CONNECTION_ID,
            sequence_number=len(self.sent) + len(self.cancelled),
            dispatched_at=self.clock(),
        )


def _agent(status: AgentStatus = AgentStatus.ONLINE) -> AgentRecord:
    revoked = status is AgentStatus.REVOKED
    return AgentRecord(
        id=AGENT_ID,
        slug="home-lab",
        name="Home Lab",
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=NOW - timedelta(days=1),
        last_connected_at=NOW - timedelta(minutes=1),
        last_seen_at=NOW,
        enrollment_status=(EnrollmentStatus.REVOKED if revoked else EnrollmentStatus.ENROLLED),
        revoked_at=NOW if revoked else None,
    )


def _bench(
    *,
    agent_id: UUID = AGENT_ID,
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=BENCH_ID,
        agent_id=agent_id,
        agent_slug="home-lab",
        local_bench_id="esp32-01",
        name="ESP32",
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        status=status,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe", "reset"}),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        last_seen_at=NOW,
    )


def _lease(
    *,
    agent_id: UUID = AGENT_ID,
    bench_id: str = BENCH_ID,
    reservation_id: UUID = RESERVATION_ID,
    lease_version: int = 1,
    valid_from: datetime = NOW - timedelta(minutes=1),
    valid_until: datetime = NOW + timedelta(hours=1),
    released_at: datetime | None = None,
) -> ReservationLease:
    return ReservationLease(
        reservation_id=reservation_id,
        agent_id=agent_id,
        bench_id=bench_id,
        owner="ci/test",
        valid_from=valid_from,
        valid_until=valid_until,
        lease_version=lease_version,
        released_at=released_at,
    )


def _phase6_command_identity(
    role: RoleName,
    *,
    resource_id: str = BENCH_ID,
    permission_restrictions: set[str] | None = None,
    principal_id: UUID = PHASE6_PRINCIPAL_ID,
) -> tuple[AuthorisationService, AuthenticationContext, ActorContext]:
    organisation_id = _bench().organisation_id
    principal = Principal(
        id=principal_id,
        type=PrincipalType.USER,
        organisation_id=organisation_id,
        display_name=role.value,
    )
    assignment = RoleAssignment(
        organisation_id=organisation_id,
        subject_type=RoleSubjectType.USER,
        subject_id=principal.id,
        role=role,
        resource_type=ResourceType.BENCH,
        resource_id=resource_id,
        created_by=principal.id,
        created_at=NOW - timedelta(minutes=1),
    )
    context = AuthenticationContext(
        principal=principal,
        permission_restrictions=permission_restrictions,
    )
    actor = ActorContext(
        principal_id=principal.id,
        principal_type=principal.type,
        display_name=principal.display_name,
        organisation_id=principal.organisation_id,
    )
    return (
        AuthorisationService(
            ScopedAuthorisationRepository((assignment,)),
            clock=lambda: NOW,
        ),
        context,
        actor,
    )


def _stack(
    *,
    status: AgentStatus = AgentStatus.ONLINE,
    bench_status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    queue_offline: bool = False,
    repository: InMemoryRemoteCommandRepository | None = None,
) -> tuple[
    MutableClock,
    FakeDirectory,
    FakeTransport,
    InMemoryRemoteCommandRepository,
    RemoteCommandService,
]:
    clock = MutableClock()
    directory = FakeDirectory(_agent(status), _bench(status=bench_status))
    transport = FakeTransport(clock)
    records = repository or InMemoryRemoteCommandRepository()
    service = RemoteCommandService(
        records,
        directory,
        transport,
        queue_commands_for_offline_agents=queue_offline,
        reconciliation_timeout_seconds=90,
        clock=clock,
    )
    service.set_authorisation_snapshot_repository(InMemoryAuthorisationSnapshotRepository())
    return clock, directory, transport, records, service


async def _create_probe(
    service: RemoteCommandService,
    *,
    key: str = "probe-1",
    dispatch: bool = True,
    payload: Mapping[str, object] | None = None,
    operation_type: str | None = None,
) -> tuple[RemoteCommand, DistributedOperation | None]:
    return await service.create(
        agent_id=AGENT_ID,
        bench_id=BENCH_ID,
        command_type=RemoteCommandType.PROBE,
        payload=payload or {"detail": "full"},
        expires_at=NOW + timedelta(hours=2),
        idempotency_key=key,
        operation_type=operation_type,
        dispatch=dispatch,
    )


def test_remote_command_happy_path_accepts_progresses_and_finishes_once() -> None:
    async def scenario() -> None:
        clock, _directory, transport, records, service = _stack()
        command, operation = await _create_probe(service)

        assert command.status is RemoteCommandStatus.DISPATCHED
        assert command.attempt_count == 1
        assert command.dispatched_at == NOW
        assert operation is not None
        assert operation.status is DistributedOperationStatus.DISPATCHED
        assert transport.sent[0].command.id == command.id
        assert transport.sent[0].command.attempt_count == 0

        accepted_at = clock.advance(seconds=1)
        command, operation = await service.accepted(
            AGENT_ID,
            CommandAcceptedPayload(command_id=command.id, accepted_at=accepted_at),
        )
        assert command.status is RemoteCommandStatus.ACCEPTED
        assert command.acknowledged_at == accepted_at
        assert operation is not None
        assert operation.status is DistributedOperationStatus.ACCEPTED

        # Duplicate acknowledgement is a replay, not another state transition.
        assert (
            await service.accepted(
                AGENT_ID,
                CommandAcceptedPayload(
                    command_id=command.id,
                    accepted_at=clock.advance(seconds=1),
                ),
            )
        )[0] == command

        started_at = clock.advance(seconds=1)
        command, operation = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_STARTED,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=started_at,
                progress=0,
                message="started",
            ),
        )
        assert command.status is RemoteCommandStatus.RUNNING
        assert command.started_at == started_at
        assert operation is not None
        assert operation.status is DistributedOperationStatus.RUNNING

        progressed_at = clock.advance(seconds=1)
        command, operation = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_PROGRESS,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=progressed_at,
                progress=45,
                message="working",
                result={"completed_steps": 2},
            ),
        )
        assert operation is not None
        assert operation.progress == 45
        assert operation.message == "working"
        assert operation.result == {"completed_steps": 2}

        # Old and duplicate updates cannot regress the user-facing view.
        stale = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_PROGRESS,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=started_at,
                progress=1,
                message="stale",
                result={"completed_steps": 0},
            ),
        )
        assert stale == (command, operation)

        completed_at = clock.advance(seconds=1)
        command, operation = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_SUCCEEDED,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=completed_at,
                progress=100,
                message="done",
                result={"firmware_version": "0.6.0-alpha"},
            ),
        )
        assert command.status is RemoteCommandStatus.SUCCEEDED
        assert command.completed_at == completed_at
        assert operation is not None
        assert operation.status is DistributedOperationStatus.SUCCEEDED
        assert operation.progress == 100
        assert operation.result == {"firmware_version": "0.6.0-alpha"}

        late_failure = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_FAILED,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=clock.advance(seconds=1),
                result={"firmware_version": "corrupted"},
                error_code="LATE",
                error_message="must not overwrite success",
            ),
        )
        assert late_failure == (command, operation)
        assert (await records.get_command(command.id)) == command

    asyncio.run(scenario())


def test_cancellation_uses_protocol_path_and_waits_for_agent_terminal_event() -> None:
    async def scenario() -> None:
        clock, _directory, transport, records, service = _stack()
        command, operation = await _create_probe(service, operation_type="PROBE")
        assert operation is not None

        requested = await service.request_cancel(command.id, reason="CI session cancelled")
        assert requested.status is RemoteCommandStatus.DISPATCHED
        assert transport.cancelled == [
            CommandCancelPayload(command_id=command.id, reason="CI session cancelled")
        ]
        assert len(await records.list_commands(limit=10)) == 1

        # Durable callers may retry until the Agent confirms the original command terminal.
        await service.request_cancel(command.id, reason="CI session cancelled")
        assert [payload.command_id for payload in transport.cancelled] == [
            command.id,
            command.id,
        ]
        occurred_at = clock.advance(seconds=1)
        cancelled, cancelled_operation = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_CANCELLED,
            OperationEventPayload(
                command_id=command.id,
                occurred_at=occurred_at,
                message="CI session cancelled",
            ),
        )
        assert cancelled.status is RemoteCommandStatus.CANCELLED
        assert cancelled_operation is not None
        assert cancelled_operation.status is DistributedOperationStatus.CANCELLED
        await service.request_cancel(command.id, reason="replayed")
        assert len(transport.cancelled) == 2

    asyncio.run(scenario())


def test_undispatched_command_cancellation_is_atomic_and_idempotent() -> None:
    async def scenario() -> None:
        _clock, _directory, transport, records, service = _stack()
        command, operation = await _create_probe(
            service,
            dispatch=False,
            operation_type="PROBE",
        )
        assert operation is not None

        cancelled = await service.request_cancel(command.id, reason="no longer needed")
        assert cancelled.status is RemoteCommandStatus.CANCELLED
        assert cancelled.error_message == "no longer needed"
        stored_operation = await records.get_operation_for_command(command.id)
        assert stored_operation is not None
        assert stored_operation.status is DistributedOperationStatus.CANCELLED
        assert await service.request_cancel(command.id, reason="replayed") == cancelled
        assert transport.cancelled == []

    asyncio.run(scenario())


def test_rejection_and_invalid_events_preserve_state_machine_invariants() -> None:
    async def scenario() -> None:
        clock, _directory, _transport, _records, service = _stack()
        command, operation = await _create_probe(service, key="reject")
        rejected_at = clock.advance(seconds=1)
        command, operation = await service.rejected(
            AGENT_ID,
            CommandRejectedPayload(
                command_id=command.id,
                rejected_at=rejected_at,
                error_code="LOCAL_SAFETY",
                error_message="bench interlock is active",
            ),
        )
        assert command.status is RemoteCommandStatus.FAILED
        assert command.error_code == "LOCAL_SAFETY"
        assert operation is not None
        assert operation.status is DistributedOperationStatus.FAILED
        assert operation.last_agent_update_at == rejected_at

        replay = await service.rejected(
            AGENT_ID,
            CommandRejectedPayload(
                command_id=command.id,
                rejected_at=clock.advance(seconds=1),
                error_code="DIFFERENT",
                error_message="ignored terminal replay",
            ),
        )
        assert replay == (command, operation)

        unsent, unsent_operation = await _create_probe(
            service,
            key="unsent",
            dispatch=False,
        )
        assert unsent_operation is not None
        unchanged = await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_STARTED,
            OperationEventPayload(
                command_id=unsent.id,
                occurred_at=clock.advance(seconds=1),
            ),
        )
        assert unchanged == (unsent, unsent_operation)

        invalid_rejection = await service.rejected(
            AGENT_ID,
            CommandRejectedPayload(
                command_id=unsent.id,
                rejected_at=clock.advance(seconds=1),
                error_code="UNSENT",
                error_message="cannot reject work that was never dispatched",
            ),
        )
        assert invalid_rejection == (unsent, unsent_operation)

        with pytest.raises(ValueError, match="not an operation event"):
            await service.operation_event(
                AGENT_ID,
                MessageType.AGENT_STATUS,
                OperationEventPayload(
                    command_id=unsent.id,
                    occurred_at=clock.advance(seconds=1),
                ),
            )

        with pytest.raises(RemoteCommandNotFoundError, match="authenticated Agent"):
            await service.accepted(
                OTHER_AGENT_ID,
                CommandAcceptedPayload(
                    command_id=unsent.id,
                    accepted_at=clock.advance(seconds=1),
                ),
            )
        with pytest.raises(RemoteCommandNotFoundError, match="does not exist"):
            await service.rejected(
                AGENT_ID,
                CommandRejectedPayload(
                    command_id=UUID(int=999),
                    rejected_at=clock.advance(seconds=1),
                    error_code="NOPE",
                    error_message="missing",
                ),
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (AgentStatus.REVOKED, AgentRevokedError),
        (AgentStatus.DRAINING, AgentDrainingError),
        (AgentStatus.DRAINED, AgentDrainingError),
        (AgentStatus.DEGRADED, AgentDegradedError),
        (AgentStatus.INCOMPATIBLE, AgentIncompatibleError),
        (AgentStatus.OFFLINE, AgentOfflineError),
        (AgentStatus.PENDING, AgentOfflineError),
    ],
)
def test_ineligible_agents_never_receive_new_work(
    status: AgentStatus,
    error: type[Exception],
) -> None:
    async def scenario() -> None:
        _clock, _directory, transport, _records, service = _stack(status=status)
        with pytest.raises(error):
            await _create_probe(service)
        assert transport.sent == []

    asyncio.run(scenario())


def test_offline_queue_policy_and_route_validation_are_fail_safe() -> None:
    async def scenario() -> None:
        _clock, directory, transport, _records, service = _stack(
            status=AgentStatus.OFFLINE,
            bench_status=GlobalBenchStatus.OFFLINE,
            queue_offline=True,
        )
        queued, operation = await _create_probe(service)
        assert queued.status is RemoteCommandStatus.QUEUED
        assert operation is not None and operation.status is DistributedOperationStatus.CREATED
        assert transport.sent == []

        with pytest.raises(AgentOfflineError):
            await service.dispatch(queued.id)

        directory.agent = _agent()
        directory.bench = _bench()
        transport.connected = False
        still_queued, _ = await service.dispatch(queued.id)
        assert still_queued.status is RemoteCommandStatus.QUEUED

        _clock, _directory, disconnected, _records, strict = _stack()
        disconnected.connected = False
        with pytest.raises(AgentOfflineError, match="no active command channel"):
            await _create_probe(strict)

        _clock, missing_directory, _transport, _records, missing = _stack()
        missing_directory.agent = None
        with pytest.raises(AgentNotFoundError):
            await _create_probe(missing)
        missing_directory.agent = _agent()
        missing_directory.bench = None
        with pytest.raises(BenchAgentMismatchError):
            await _create_probe(missing, key="missing-bench")
        missing_directory.bench = _bench(agent_id=OTHER_AGENT_ID)
        with pytest.raises(BenchAgentMismatchError):
            await _create_probe(missing, key="wrong-owner")

        _clock, _directory, _transport, _records, offline_bench = _stack(
            bench_status=GlobalBenchStatus.OFFLINE,
        )
        with pytest.raises(AgentOfflineError, match="bench is offline"):
            await _create_probe(offline_bench)

    asyncio.run(scenario())


def test_reservation_lease_and_idempotency_fences_are_enforced() -> None:
    async def scenario() -> None:
        clock, _directory, transport, _records, service = _stack()
        with pytest.raises(ValueError, match="requires a reservation lease"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RESET,
                payload={},
                expires_at=NOW + timedelta(hours=1),
                idempotency_key="reset-no-lease",
            )

        for lease in (
            _lease(agent_id=OTHER_AGENT_ID),
            _lease(bench_id="home-lab/other"),
        ):
            with pytest.raises(BenchAgentMismatchError):
                await service.create(
                    agent_id=AGENT_ID,
                    bench_id=BENCH_ID,
                    command_type=RemoteCommandType.RESET,
                    payload={},
                    expires_at=NOW + timedelta(hours=1),
                    idempotency_key=f"bad-route-{lease.agent_id}-{lease.bench_id}",
                    reservation_lease=lease,
                )
        with pytest.raises(RemoteCommandExpiredError, match="lease is not currently valid"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RESET,
                payload={},
                expires_at=NOW + timedelta(hours=1),
                idempotency_key="expired-lease",
                reservation_lease=_lease(valid_until=NOW - timedelta(seconds=1)),
            )

        lease = _lease()
        command, operation = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RESET,
            payload={"mode": "hard"},
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="reset-once",
            reservation_lease=lease,
            dispatch=False,
        )
        assert command.status is RemoteCommandStatus.CREATED
        assert operation is not None
        replay = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RESET,
            payload={"mode": "hard"},
            expires_at=NOW + timedelta(hours=3),
            idempotency_key="reset-once",
            reservation_lease=lease,
        )
        assert replay == (command, operation)
        assert transport.sent == []

        for changed_payload, changed_lease in (
            ({"mode": "soft"}, lease),
            ({"mode": "hard"}, _lease(lease_version=2)),
            ({"mode": "hard"}, _lease(reservation_id=UUID(int=301))),
        ):
            with pytest.raises(RemoteCommandDuplicateError):
                await service.create(
                    agent_id=AGENT_ID,
                    bench_id=BENCH_ID,
                    command_type=RemoteCommandType.RESET,
                    payload=changed_payload,
                    expires_at=NOW + timedelta(hours=1),
                    idempotency_key="reset-once",
                    reservation_lease=changed_lease,
                )

        with pytest.raises(ValueError, match="does not match persisted command"):
            await service.dispatch(command.id)
        with pytest.raises(ValueError, match="does not match persisted command"):
            await service.dispatch(command.id, reservation_lease=_lease(lease_version=2))
        clock.advance(seconds=1)
        with pytest.raises(RemoteCommandExpiredError, match="expired before dispatch"):
            await service.dispatch(
                command.id,
                reservation_lease=_lease(valid_until=NOW),
            )

        clock.value = NOW
        dispatched, _ = await service.dispatch(command.id, reservation_lease=lease)
        assert dispatched.status is RemoteCommandStatus.DISPATCHED

        probe, _ = await _create_probe(service, key="unreserved", dispatch=False)
        with pytest.raises(ValueError, match="Unreserved command"):
            await service.dispatch(probe.id, reservation_lease=lease)

    asyncio.run(scenario())


def test_delivery_failure_marks_active_work_unknown_for_reconciliation() -> None:
    async def scenario() -> None:
        clock, _directory, transport, records, service = _stack()
        first, first_operation = await _create_probe(service, key="first")
        assert first_operation is not None
        await service.accepted(
            AGENT_ID,
            CommandAcceptedPayload(
                command_id=first.id,
                accepted_at=clock.advance(seconds=1),
            ),
        )
        await service.operation_event(
            AGENT_ID,
            MessageType.OPERATION_STARTED,
            OperationEventPayload(
                command_id=first.id,
                occurred_at=clock.advance(seconds=1),
            ),
        )

        transport.error = ConnectionError("partition")
        with pytest.raises(RemoteCommandDeliveryFailedError) as raised:
            await _create_probe(service, key="delivery-fails")
        failed = await records.get_by_idempotency_key(AGENT_ID, "delivery-fails")
        assert failed is not None
        assert failed.status is RemoteCommandStatus.UNKNOWN
        assert failed.attempt_count == 1
        assert raised.value.details["command_id"] == str(failed.id)

        interrupted = await records.get_command(first.id)
        interrupted_operation = await records.get_operation_for_command(first.id)
        assert interrupted is not None and interrupted.status is RemoteCommandStatus.UNKNOWN
        assert interrupted_operation is not None
        assert interrupted_operation.status is DistributedOperationStatus.UNKNOWN
        assert interrupted_operation.reconciliation_deadline == clock() + timedelta(seconds=90)

        assert await service.mark_unknown(UUID(int=404), observed_at=clock()) == 0

    asyncio.run(scenario())


def test_expiry_covers_created_queued_dispatched_and_unknown_commands() -> None:
    async def scenario() -> None:
        clock, directory, transport, records, service = _stack(queue_offline=True)
        created, _ = await _create_probe(service, key="created", dispatch=False)
        dispatched, _ = await _create_probe(service, key="dispatched")

        directory.agent = _agent(AgentStatus.OFFLINE)
        directory.bench = _bench(status=GlobalBenchStatus.OFFLINE)
        queued, _ = await _create_probe(service, key="queued")
        directory.agent = _agent()
        directory.bench = _bench()
        await service.mark_unknown(AGENT_ID, observed_at=clock())
        unknown = await records.get_command(dispatched.id)
        assert unknown is not None and unknown.status is RemoteCommandStatus.UNKNOWN

        clock.value = NOW + timedelta(hours=3)
        assert await service.expire_due(limit=100) == 3
        for command_id in (created.id, queued.id, dispatched.id):
            expired = await records.get_command(command_id)
            operation = await records.get_operation_for_command(command_id)
            assert expired is not None and expired.status is RemoteCommandStatus.EXPIRED
            assert operation is not None and operation.status is DistributedOperationStatus.FAILED
            assert operation.error_code == RemoteCommandExpiredError.code

        # Dispatch performs the same durable expiry check before consulting connectivity.
        late, _ = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.PROBE,
            payload={},
            expires_at=clock() + timedelta(seconds=1),
            idempotency_key="late",
            dispatch=False,
        )
        clock.advance(seconds=2)
        transport.connected = False
        late, late_operation = await service.dispatch(late.id)
        assert late.status is RemoteCommandStatus.EXPIRED
        assert late_operation is not None
        assert late_operation.status is DistributedOperationStatus.FAILED

    asyncio.run(scenario())


def test_repository_attempts_filters_and_bundle_invariants() -> None:
    async def scenario() -> None:
        records = InMemoryRemoteCommandRepository()
        command = RemoteCommand(
            id=UUID(int=501),
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.REFRESH_INVENTORY,
            payload={},
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            idempotency_key="repository",
        )
        operation = DistributedOperation(
            id=UUID(int=502),
            remote_command_id=UUID(int=999),
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            operation_type="refresh",
            created_at=NOW,
        )
        with pytest.raises(ValueError, match="not bound"):
            await records.create_bundle(command, operation)

        created, no_operation = await records.create_bundle(command, None)
        assert created == command and no_operation is None
        assert await records.create_bundle(command, None) == (command, None)
        assert await records.get_by_idempotency_key(AGENT_ID, "repository") == command

        missing_attempt = RemoteCommandAttempt(
            command_id=UUID(int=777),
            attempt_number=1,
            dispatched_at=NOW,
        )
        with pytest.raises(RemoteCommandNotFoundError):
            await records.record_attempt(missing_attempt)
        with pytest.raises(RemoteCommandDuplicateError, match="sequence"):
            await records.record_attempt(
                RemoteCommandAttempt(
                    command_id=command.id,
                    attempt_number=2,
                    dispatched_at=NOW,
                )
            )

        attempt = RemoteCommandAttempt(
            id=UUID(int=503),
            command_id=command.id,
            attempt_number=1,
            connection_id=CONNECTION_ID,
            sequence_number=1,
            dispatched_at=NOW,
        )
        assert await records.record_attempt(attempt) == attempt
        assert await records.record_attempt(attempt) == attempt
        with pytest.raises(RemoteCommandDuplicateError, match="different content"):
            await records.record_attempt(attempt.model_copy(update={"id": UUID(int=504)}))
        await records.acknowledge_latest_attempt(command.id, NOW + timedelta(seconds=1))
        await records.acknowledge_latest_attempt(command.id, NOW + timedelta(seconds=2))
        await records.acknowledge_latest_attempt(UUID(int=778), NOW)

        assert await records.list_commands(agent_id=AGENT_ID) == [
            command.model_copy(update={"attempt_count": 1})
        ]
        assert await records.list_commands(statuses={RemoteCommandStatus.FAILED}) == []
        with pytest.raises(ValueError, match="limit must be positive"):
            await records.list_commands(limit=0)

        assert (
            await records.update_command(
                command,
                expected_statuses={RemoteCommandStatus.FAILED},
            )
            is None
        )
        assert (
            await records.update_operation(
                operation,
                expected_statuses={DistributedOperationStatus.CREATED},
            )
            is None
        )

    asyncio.run(scenario())


def test_constructor_timestamp_and_expiry_validation() -> None:
    with pytest.raises(ValueError, match="reconciliation_timeout_seconds"):
        RemoteCommandService(
            InMemoryRemoteCommandRepository(),
            FakeDirectory(_agent(), _bench()),
            FakeTransport(MutableClock()),
            reconciliation_timeout_seconds=0,
        )

    async def scenario() -> None:
        _clock, _directory, _transport, _records, service = _stack()
        with pytest.raises(RemoteCommandExpiredError, match="must be in the future"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW,
                idempotency_key="already-expired",
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
                idempotency_key="naive-expiry",
            )

        naive_clock = MutableClock(NOW.replace(tzinfo=None))
        invalid_clock_service = RemoteCommandService(
            InMemoryRemoteCommandRepository(),
            FakeDirectory(_agent(), _bench()),
            FakeTransport(naive_clock),
            clock=naive_clock,
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            await invalid_clock_service.expire_due()

    asyncio.run(scenario())


def test_phase6_command_create_enforces_exact_action_scope_before_persistence() -> None:
    async def scenario() -> None:
        _, _, transport, records, service = _stack()
        viewer, viewer_context, viewer_actor = _phase6_command_identity(RoleName.VIEWER)
        service.set_authorisation_service(viewer)

        with pytest.raises(AuthenticationRequiredError):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="implicit-legacy-probe",
                dispatch=False,
            )
        with pytest.raises(PermissionDeniedError):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="viewer-probe",
                dispatch=False,
                actor_context=viewer_actor,
                authentication_context=viewer_context,
            )
        assert await records.list_commands() == []
        assert transport.sent == []

        operator, operator_context, operator_actor = _phase6_command_identity(RoleName.OPERATOR)
        service.set_authorisation_service(operator)
        command, _ = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RESET,
            payload={},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="operator-reset",
            reservation_lease=_lease(),
            dispatch=False,
            actor_context=operator_actor,
            authentication_context=operator_context,
        )
        assert (await records.get_command(command.id)) == command

        narrowed, narrowed_context, narrowed_actor = _phase6_command_identity(
            RoleName.OPERATOR,
            permission_restrictions={"benches:read"},
            principal_id=UUID(int=60_002),
        )
        service.set_authorisation_service(narrowed)
        with pytest.raises(PermissionDeniedError):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RESET,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="narrowed-reset",
                reservation_lease=_lease(),
                dispatch=False,
                actor_context=narrowed_actor,
                authentication_context=narrowed_context,
            )

        wrong_scope, wrong_context, wrong_actor = _phase6_command_identity(
            RoleName.OPERATOR,
            resource_id="home-lab/other-bench",
            principal_id=UUID(int=60_003),
        )
        service.set_authorisation_service(wrong_scope)
        with pytest.raises(PermissionDeniedError):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="cross-resource-probe",
                dispatch=False,
                actor_context=wrong_actor,
                authentication_context=wrong_context,
            )
        assert [item.id for item in await records.list_commands()] == [command.id]
        assert transport.sent == []

        _, _, legacy_transport, legacy_records, legacy_service = _stack()
        legacy_service.set_authorisation_service(viewer)
        legacy_command, _ = await legacy_service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.PROBE,
            payload={},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="explicit-legacy-probe",
            dispatch=False,
            allow_legacy_authorisation=True,
        )
        assert await legacy_records.get_command(legacy_command.id) == legacy_command
        assert legacy_transport.sent == []

    asyncio.run(scenario())


def test_phase6_command_snapshot_boundary_preserves_exact_replay_evidence() -> None:
    async def scenario() -> None:
        _, _, _, records, service = _stack()
        snapshots = InMemoryAuthorisationSnapshotRepository()
        service.set_authorisation_snapshot_repository(snapshots)
        operator, context, actor = _phase6_command_identity(RoleName.OPERATOR)
        service.set_authorisation_service(operator)

        command, operation = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RESET,
            payload={"owner": "phase6/operator"},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="snapshot-replay",
            reservation_lease=_lease(),
            dispatch=False,
            actor_context=actor,
            authentication_context=context,
        )
        assert command.authorisation_snapshot_id is not None
        assert command.actor_context is not None
        assert command.actor_context.authorisation_snapshot_id == command.authorisation_snapshot_id
        snapshot = await snapshots.get_authorisation_snapshot(
            command.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.principal_id == context.principal.id
        assert snapshot.permission == "benches:reset"
        assert snapshot.resource_type is ResourceType.BENCH
        assert snapshot.resource_id == BENCH_ID

        # An exact retry keeps the evidence accepted with the original command. It does
        # not become new work merely because the HTTP permission dependency minted a
        # fresh decision snapshot, and it remains attributable after the role is removed.
        audit = InMemoryAuditRepository()
        service.set_authorisation_service(
            AuthorisationService(
                ScopedAuthorisationRepository(()),
                audit_repository=audit,
                clock=lambda: NOW,
            )
        )
        fresh_snapshot_id = uuid4()
        replayed, replayed_operation = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RESET,
            payload={"owner": "phase6/operator"},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="snapshot-replay",
            reservation_lease=_lease(),
            dispatch=False,
            actor_context=actor.model_copy(update={"authorisation_snapshot_id": fresh_snapshot_id}),
            authorisation_snapshot_id=fresh_snapshot_id,
            authentication_context=context.model_copy(
                update={"authorisation_snapshot_id": fresh_snapshot_id}
            ),
        )
        assert replayed == command
        assert replayed_operation == operation
        assert replayed.authorisation_snapshot_id == snapshot.id

        with pytest.raises(PermissionDeniedError):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RESET,
                payload={"owner": "phase6/operator"},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="new-work-after-role-removal",
                reservation_lease=_lease(),
                dispatch=False,
                actor_context=actor,
                authentication_context=context,
            )

        narrowed = context.model_copy(update={"permission_restrictions": {"benches:read"}})
        with pytest.raises(PermissionDeniedError, match="cannot replay"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RESET,
                payload={"owner": "phase6/operator"},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="snapshot-replay",
                reservation_lease=_lease(),
                dispatch=False,
                actor_context=actor,
                authentication_context=narrowed,
            )
        assert audit.events[-1].action == "PERMISSION_DENIED"
        assert audit.events[-1].metadata == {"required_permission": "benches:reset"}
        assert await records.get_command(command.id) == command

    asyncio.run(scenario())


def test_phase6_command_snapshot_boundary_rejects_missing_or_forged_evidence_store() -> None:
    async def scenario() -> None:
        clock, directory, transport, records, _ = _stack()
        operator, context, actor = _phase6_command_identity(RoleName.OPERATOR)
        missing_store = RemoteCommandService(
            records,
            directory,
            transport,
            clock=clock,
        )
        missing_store.set_authorisation_service(operator)
        with pytest.raises(AuthenticationRequiredError, match="snapshot store"):
            await missing_store.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="missing-snapshot-store",
                dispatch=False,
                actor_context=actor,
                authentication_context=context,
            )

        snapshots = InMemoryAuthorisationSnapshotRepository()
        missing_store.set_authorisation_snapshot_repository(snapshots)
        forged_id = uuid4()
        forged_context = context.model_copy(update={"authorisation_snapshot_id": forged_id})
        forged_actor = actor.model_copy(update={"authorisation_snapshot_id": forged_id})
        with pytest.raises(ValueError, match="unresolved or mismatched"):
            await missing_store.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.PROBE,
                payload={},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="forged-snapshot",
                dispatch=False,
                actor_context=forged_actor,
                authorisation_snapshot_id=forged_id,
                authentication_context=forged_context,
            )
        assert await records.list_commands() == []

    asyncio.run(scenario())


def test_phase6_workflow_command_mints_trusted_workflow_evidence_for_ci() -> None:
    async def scenario() -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "name": "trusted-ci",
                "version": 2,
                "requirements": {"capabilities": ["reset"], "labels": {}},
                "steps": [{"action": "reset"}],
            }
        )
        principal = Principal(
            id=PHASE6_PRINCIPAL_ID,
            type=PrincipalType.SERVICE_ACCOUNT,
            organisation_id=definition.organisation_id,
            display_name="trusted-ci",
        )
        assignments = tuple(
            RoleAssignment(
                organisation_id=definition.organisation_id,
                subject_type=RoleSubjectType.SERVICE_ACCOUNT,
                subject_id=principal.id,
                role=RoleName.WORKFLOW_RUNNER,
                resource_type=resource_type,
                resource_id=resource_id,
                created_by=principal.id,
                created_at=NOW - timedelta(minutes=1),
            )
            for resource_type, resource_id in (
                (ResourceType.WORKFLOW, definition.name),
                (ResourceType.BENCH, BENCH_ID),
            )
        )
        context = AuthenticationContext(
            principal=principal,
            permission_restrictions={"workflows:run", "benches:operate"},
        )
        actor = ActorContext(
            principal_id=principal.id,
            principal_type=principal.type,
            display_name=principal.display_name,
            organisation_id=principal.organisation_id,
        )
        _, _, _, records, service = _stack()
        snapshots = InMemoryAuthorisationSnapshotRepository()
        service.set_authorisation_snapshot_repository(snapshots)
        service.set_workflow_definition_catalog(InMemoryWorkflowDefinitionCatalog(definition))
        service.set_authorisation_service(
            AuthorisationService(
                ScopedAuthorisationRepository(assignments),
                clock=lambda: NOW,
            )
        )
        payload = {
            "definition": definition.model_dump(mode="json"),
            "inputs": {},
            "artifact_transfers": [],
        }
        command, _ = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RUN_WORKFLOW,
            payload=payload,
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="ci-mints-workflow-snapshot",
            reservation_lease=_lease(),
            dispatch=False,
            actor_context=actor,
            authentication_context=context,
        )
        assert command.authorisation_snapshot_id is not None
        snapshot = await snapshots.get_authorisation_snapshot(
            command.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.permission == "workflows:run"
        assert snapshot.resource_type is ResourceType.WORKFLOW
        assert snapshot.resource_id == definition.name

        forged = WorkflowDefinition.model_validate(
            {
                **definition.model_dump(mode="python"),
                "requirements": {"capabilities": [], "labels": {}},
                "steps": [{"action": "wait", "seconds": 1}],
            }
        )
        with pytest.raises(ValueError, match="trusted catalog"):
            await service.create(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                command_type=RemoteCommandType.RUN_WORKFLOW,
                payload={**payload, "definition": forged.model_dump(mode="json")},
                expires_at=NOW + timedelta(minutes=5),
                idempotency_key="forged-workflow-definition",
                reservation_lease=_lease(),
                dispatch=False,
                actor_context=actor,
                authentication_context=context,
            )
        assert len(await records.list_commands()) == 1

    asyncio.run(scenario())


def test_phase6_command_cancel_requires_exact_operation_permission_without_mutation() -> None:
    async def scenario() -> None:
        _, _, transport, records, service = _stack()
        command, _ = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.PROBE,
            payload={},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="cancel-target",
            dispatch=False,
        )

        viewer, viewer_context, _ = _phase6_command_identity(RoleName.VIEWER)
        service.set_authorisation_service(viewer)
        with pytest.raises(PermissionDeniedError):
            await service.request_cancel(
                command.id,
                authentication_context=viewer_context,
            )
        unchanged = await records.get_command(command.id)
        assert unchanged is not None
        assert unchanged.status is RemoteCommandStatus.CREATED
        assert transport.cancelled == []

        administrator, admin_context, _ = _phase6_command_identity(
            RoleName.LAB_ADMIN,
            principal_id=UUID(int=60_004),
        )
        service.set_authorisation_service(administrator)
        cancelled = await service.request_cancel(
            command.id,
            authentication_context=admin_context,
        )
        assert cancelled.status is RemoteCommandStatus.CANCELLED

    asyncio.run(scenario())


def test_phase6_dispatched_cancel_requires_snapshot_evidence_before_protocol_send() -> None:
    async def scenario() -> None:
        _, _, transport, _records, service = _stack()
        command, _ = await _create_probe(service, operation_type="PROBE")
        administrator, context, _ = _phase6_command_identity(RoleName.LAB_ADMIN)
        service.set_authorisation_service(administrator)

        with pytest.raises(AuthenticationRequiredError, match="durable authorisation evidence"):
            await service.request_cancel(
                command.id,
                authentication_context=context,
            )
        assert transport.cancelled == []

    asyncio.run(scenario())


def test_phase6_ci_cancel_accepts_only_exact_snapshot_and_trusted_work_binding() -> None:
    async def scenario() -> None:
        _, _, transport, _records, service = _stack()
        snapshots = InMemoryAuthorisationSnapshotRepository()
        service.set_authorisation_snapshot_repository(snapshots)
        command, operation = await service.create(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            command_type=RemoteCommandType.RUN_WORKFLOW,
            payload={},
            expires_at=NOW + timedelta(minutes=5),
            idempotency_key="ci-cancel-boundary",
            reservation_lease=_lease(),
            operation_type=RemoteCommandType.RUN_WORKFLOW.value,
        )
        assert operation is not None
        session_id = uuid4()
        binding = DistributedCiWorkflowBinding(
            ci_session_id=session_id,
            remote_command_id=command.id,
            operation_id=operation.id,
            agent_id=command.agent_id,
            bench_id=command.bench_id,
            reservation_id=RESERVATION_ID,
            workflow_name="trusted-ci",
            workflow_version=1,
            launch_idempotency_key="ci-cancel-boundary",
            request_fingerprint="a" * 64,
            created_at=NOW,
        )
        bindings = InMemoryCiCancellationBindings(binding)
        service.set_ci_cancellation_binding_repository(bindings)
        principal = Principal(
            id=PHASE6_PRINCIPAL_ID,
            type=PrincipalType.USER,
            organisation_id=command.organisation_id,
            display_name="Workflow Runner",
        )
        snapshot = await snapshots.create_authorisation_snapshot(
            command.organisation_id,
            AuthorisationSnapshot(
                principal_id=principal.id,
                permission="ci:sessions:cancel",
                resource_type="CI_SESSION",
                resource_id=str(session_id),
                evaluated_at=NOW,
            ),
        )
        context = AuthenticationContext(
            principal=principal,
            authorisation_snapshot_id=snapshot.id,
        )

        returned = await service.request_ci_cancel(
            command.id,
            ci_session_id=session_id,
            reason="CI requested",
            authentication_context=context,
        )
        assert returned == command
        assert len(transport.cancelled) == 1
        assert transport.cancelled[0].actor_context is not None
        assert transport.cancelled[0].actor_context.principal_id == principal.id
        assert transport.cancelled[0].authorisation_snapshot_id == snapshot.id

        bindings.binding = binding.model_copy(update={"operation_id": uuid4()})
        with pytest.raises(ValueError, match="trusted remote work"):
            await service.request_ci_cancel(
                command.id,
                ci_session_id=session_id,
                reason=None,
                authentication_context=context,
            )
        assert len(transport.cancelled) == 1

        wrong_snapshot = await snapshots.create_authorisation_snapshot(
            command.organisation_id,
            AuthorisationSnapshot(
                principal_id=principal.id,
                permission="operations:cancel",
                resource_type=ResourceType.BENCH,
                resource_id=BENCH_ID,
                evaluated_at=NOW,
            ),
        )
        with pytest.raises(ValueError, match="unresolved or mismatched"):
            await service.request_ci_cancel(
                command.id,
                ci_session_id=session_id,
                reason=None,
                authentication_context=context.model_copy(
                    update={"authorisation_snapshot_id": wrong_snapshot.id}
                ),
            )
        assert len(transport.cancelled) == 1

    asyncio.run(scenario())
