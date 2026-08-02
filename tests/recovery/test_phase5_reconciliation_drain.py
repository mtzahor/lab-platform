from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.agent_protocol.errors import ProtocolMessageInvalidError
from lab_platform.control_plane_core.drain import (
    AgentDrainService,
    AgentDrainSnapshot,
    AgentWorkload,
)
from lab_platform.control_plane_core.errors import (
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRestartedDuringOperationError,
    AgentRevokedError,
    RemoteOperationReconciliationTimeoutError,
)
from lab_platform.control_plane_core.reconciliation import (
    AgentBootContext,
    ReconciliationResult,
    ReconciliationService,
    ReportClaim,
    ReportClaimStatus,
    reconciliation_report_digest,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchStatus,
    HealthStatus,
    ReconciliationBenchSnapshot,
    ReconciliationCommandState,
    ReconciliationReport,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
AGENT_ID = UUID(int=1)
OLD_BOOT_ID = UUID(int=101)
NEW_BOOT_ID = UUID(int=102)


def _agent(status: AgentStatus = AgentStatus.ONLINE, *, number: int = 1) -> AgentRecord:
    registered_at = NOW - timedelta(days=1)
    revoked = status is AgentStatus.REVOKED
    return AgentRecord(
        id=UUID(int=number),
        slug=f"agent-{number}",
        name=f"Agent {number}",
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=registered_at,
        enrollment_status=(EnrollmentStatus.REVOKED if revoked else EnrollmentStatus.ENROLLED),
        revoked_at=NOW if revoked else None,
    )


def _command(number: int) -> RemoteCommand:
    created_at = NOW - timedelta(hours=1)
    return RemoteCommand(
        id=UUID(int=number),
        agent_id=AGENT_ID,
        bench_id="agent-1/bench-a",
        command_type=RemoteCommandType.RUN_WORKFLOW,
        status=RemoteCommandStatus.UNKNOWN,
        created_at=created_at,
        dispatched_at=created_at + timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=f"command-{number}",
    )


def _operation(
    number: int,
    command: RemoteCommand,
    *,
    status: DistributedOperationStatus = DistributedOperationStatus.UNKNOWN,
    deadline: datetime | None = None,
) -> DistributedOperation:
    return DistributedOperation(
        id=UUID(int=10_000 + number),
        remote_command_id=command.id,
        agent_id=command.agent_id,
        bench_id=command.bench_id,
        operation_type="RUN_WORKFLOW",
        status=status,
        created_at=command.created_at,
        dispatched_at=command.dispatched_at,
        reconciliation_deadline=deadline or NOW + timedelta(minutes=10),
    )


def _state(
    command: RemoteCommand,
    status: RemoteCommandStatus,
    *,
    updated_at: datetime = NOW,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
) -> ReconciliationCommandState:
    return ReconciliationCommandState(
        command_id=command.id,
        status=status,
        updated_at=updated_at,
        result=result,
        error_code=error_code,
    )


def _lease(
    number: int,
    *,
    version: int = 1,
    agent_id: UUID = AGENT_ID,
    valid_until: datetime | None = None,
) -> ReservationLease:
    return ReservationLease(
        reservation_id=UUID(int=20_000 + number),
        agent_id=agent_id,
        bench_id="agent-1/bench-a",
        owner="ci:test",
        valid_from=NOW - timedelta(hours=1),
        valid_until=valid_until or NOW + timedelta(hours=1),
        lease_version=version,
    )


def _bench_snapshot(local_id: str = "bench-a") -> ReconciliationBenchSnapshot:
    return ReconciliationBenchSnapshot(
        local_bench_id=local_id,
        name="Bench A",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe", "workflow"}),
    )


def _report(
    *,
    boot_id: UUID = OLD_BOOT_ID,
    active: tuple[ReconciliationCommandState, ...] = (),
    recent: tuple[ReconciliationCommandState, ...] = (),
    leases: tuple[ReservationLease, ...] = (),
    benches: tuple[ReconciliationBenchSnapshot, ...] = (),
    generated_at: datetime = NOW - timedelta(seconds=1),
) -> ReconciliationReport:
    return ReconciliationReport(
        agent_id=AGENT_ID,
        boot_id=boot_id,
        generated_at=generated_at,
        active_commands=active,
        recent_commands=recent,
        local_reservation_leases=leases,
        bench_snapshots=benches,
        buffered_event_count=0,
    )


@dataclass(slots=True)
class _StoredReport:
    agent_id: UUID
    digest: str
    result: ReconciliationResult | None = None


class FakeReportStore:
    def __init__(self) -> None:
        self.by_id: dict[UUID, _StoredReport] = {}
        self.by_content: dict[tuple[UUID, str], UUID] = {}

    async def claim_report(
        self,
        report_id: UUID,
        *,
        agent_id: UUID,
        content_digest: str,
        received_at: datetime,
    ) -> ReportClaim:
        del received_at
        existing = self.by_id.get(report_id)
        if existing is not None:
            if existing.agent_id != agent_id or existing.digest != content_digest:
                return ReportClaim(ReportClaimStatus.CONFLICT)
            if existing.result is None:
                return ReportClaim(ReportClaimStatus.IN_PROGRESS)
            return ReportClaim(ReportClaimStatus.DUPLICATE, existing.result)
        content_owner = self.by_content.get((agent_id, content_digest))
        if content_owner is not None:
            prior = self.by_id[content_owner]
            if prior.result is None:
                return ReportClaim(ReportClaimStatus.IN_PROGRESS)
            self.by_id[report_id] = _StoredReport(
                agent_id=agent_id,
                digest=content_digest,
                result=prior.result,
            )
            return ReportClaim(ReportClaimStatus.DUPLICATE, prior.result)
        self.by_id[report_id] = _StoredReport(agent_id=agent_id, digest=content_digest)
        self.by_content[(agent_id, content_digest)] = report_id
        return ReportClaim(ReportClaimStatus.NEW)

    async def complete_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
        result: ReconciliationResult,
    ) -> None:
        stored = self.by_id[report_id]
        if stored.digest != content_digest or stored.result is not None:
            raise AssertionError("invalid report completion")
        stored.result = result

    async def abandon_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
    ) -> None:
        stored = self.by_id.get(report_id)
        if stored is None or stored.digest != content_digest or stored.result is not None:
            return
        self.by_id.pop(report_id)
        self.by_content.pop((stored.agent_id, content_digest))


class FakeBootRepository:
    def __init__(self, context: AgentBootContext | None) -> None:
        self.context = context

    async def get_boot_context(self, agent_id: UUID) -> AgentBootContext | None:
        assert agent_id == AGENT_ID
        return self.context


class FakeWorkRepository:
    def __init__(
        self,
        commands: tuple[RemoteCommand, ...] = (),
        operations: tuple[DistributedOperation, ...] = (),
    ) -> None:
        self.commands = {command.id: command for command in commands}
        self.operations = {operation.id: operation for operation in operations}
        self.command_races: dict[UUID, RemoteCommand] = {}
        self.operation_races: dict[UUID, DistributedOperation] = {}

    async def get_command(self, command_id: UUID) -> RemoteCommand | None:
        return self.commands.get(command_id)

    async def list_commands(
        self,
        *,
        agent_id: UUID,
        statuses: Iterable[RemoteCommandStatus],
        limit: int,
    ) -> list[RemoteCommand]:
        selected = frozenset(statuses)
        return [
            command
            for command in sorted(self.commands.values(), key=lambda item: str(item.id))
            if command.agent_id == agent_id and command.status in selected
        ][:limit]

    async def update_command(
        self,
        command: RemoteCommand,
        *,
        expected_statuses: Iterable[RemoteCommandStatus],
    ) -> RemoteCommand | None:
        raced = self.command_races.pop(command.id, None)
        if raced is not None:
            self.commands[command.id] = raced
            return None
        expected = frozenset(expected_statuses)
        current = self.commands.get(command.id)
        if current is None or current.status not in expected:
            return None
        self.commands[command.id] = command
        return command

    async def list_operations(
        self,
        *,
        agent_id: UUID | None,
        statuses: Iterable[DistributedOperationStatus],
        limit: int,
    ) -> list[DistributedOperation]:
        selected = frozenset(statuses)
        return [
            operation
            for operation in sorted(self.operations.values(), key=lambda item: str(item.id))
            if (agent_id is None or operation.agent_id == agent_id) and operation.status in selected
        ][:limit]

    async def update_operation(
        self,
        operation: DistributedOperation,
        *,
        expected_statuses: Iterable[DistributedOperationStatus],
    ) -> DistributedOperation | None:
        raced = self.operation_races.pop(operation.id, None)
        if raced is not None:
            self.operations[operation.id] = raced
            return None
        expected = frozenset(expected_statuses)
        current = self.operations.get(operation.id)
        if current is None or current.status not in expected:
            return None
        self.operations[operation.id] = operation
        return operation


class FakeLeaseRepository:
    def __init__(self, leases: tuple[ReservationLease, ...] = ()) -> None:
        self.leases = {lease.reservation_id: lease for lease in leases}

    async def list_leases(self, agent_id: UUID) -> list[ReservationLease]:
        return [lease for lease in self.leases.values() if lease.agent_id == agent_id]

    async def release_if_current(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        expected_lease_version: int,
        released_at: datetime,
    ) -> ReservationLease | None:
        lease = self.leases.get(reservation_id)
        if (
            lease is None
            or lease.agent_id != agent_id
            or lease.lease_version != expected_lease_version
            or lease.released_at is not None
        ):
            return None
        released = lease.model_copy(update={"released_at": released_at})
        self.leases[reservation_id] = released
        return released


class FakeInventory:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                UUID,
                UUID,
                datetime,
                tuple[ReconciliationBenchSnapshot, ...],
                datetime,
            ]
        ] = []

    async def reconcile_report_inventory(
        self,
        agent_id: UUID,
        *,
        boot_id: UUID,
        generated_at: datetime,
        snapshots: tuple[ReconciliationBenchSnapshot, ...],
        observed_at: datetime,
    ) -> object:
        self.calls.append((agent_id, boot_id, generated_at, snapshots, observed_at))
        return snapshots


def _reconciliation_stack(
    *,
    context: AgentBootContext | None,
    commands: tuple[RemoteCommand, ...] = (),
    operations: tuple[DistributedOperation, ...] = (),
    leases: tuple[ReservationLease, ...] = (),
) -> tuple[
    ReconciliationService,
    FakeReportStore,
    FakeBootRepository,
    FakeWorkRepository,
    FakeLeaseRepository,
    FakeInventory,
]:
    reports = FakeReportStore()
    boots = FakeBootRepository(context)
    work = FakeWorkRepository(commands, operations)
    lease_repository = FakeLeaseRepository(leases)
    inventory = FakeInventory()
    service = ReconciliationService(
        reports,
        boots,
        work,
        lease_repository,
        inventory,
        clock=lambda: NOW,
    )
    return service, reports, boots, work, lease_repository, inventory


def test_reconciliation_applies_journal_truth_leases_and_inventory() -> None:
    async def scenario() -> None:
        succeeded = _command(1)
        running = _command(2)
        accepted = _command(3)
        missing = _command(4)
        operations = (
            _operation(1, succeeded),
            _operation(2, running, status=DistributedOperationStatus.RECONCILING),
            _operation(3, accepted),
            _operation(4, missing),
        )
        expired = _lease(1, valid_until=NOW - timedelta(seconds=1))
        current = _lease(2, version=2)
        stale_old_version = _lease(2, version=1)
        report = _report(
            active=(
                _state(succeeded, RemoteCommandStatus.RUNNING),
                _state(
                    running,
                    RemoteCommandStatus.RUNNING,
                    result={"progress_marker": 60},
                ),
                _state(
                    accepted,
                    RemoteCommandStatus.ACCEPTED,
                    result={"queue": "hardware"},
                ),
            ),
            recent=(
                _state(
                    succeeded,
                    RemoteCommandStatus.SUCCEEDED,
                    result={"tests": "passed"},
                ),
            ),
            leases=(expired, stale_old_version),
            benches=(_bench_snapshot(),),
        )
        service, _, _, work, leases, inventory = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID),
            commands=(succeeded, running, accepted, missing),
            operations=operations,
            leases=(expired, current),
        )

        result = await service.reconcile(UUID(int=30_001), report)

        assert work.commands[succeeded.id].status is RemoteCommandStatus.SUCCEEDED
        assert work.commands[running.id].status is RemoteCommandStatus.RUNNING
        assert work.commands[accepted.id].status is RemoteCommandStatus.ACCEPTED
        assert work.commands[missing.id].status is RemoteCommandStatus.UNKNOWN
        assert work.operations[operations[0].id].status is DistributedOperationStatus.SUCCEEDED
        assert work.operations[operations[0].id].progress == 100
        assert work.operations[operations[0].id].result == {"tests": "passed"}
        assert work.operations[operations[1].id].status is DistributedOperationStatus.RUNNING
        assert work.operations[operations[1].id].result == {"progress_marker": 60}
        assert work.operations[operations[2].id].status is DistributedOperationStatus.ACCEPTED
        assert work.operations[operations[2].id].result == {"queue": "hardware"}
        assert work.operations[operations[3].id].status is DistributedOperationStatus.RECONCILING
        assert result.reconciled_command_ids == {succeeded.id, running.id, accepted.id}
        assert result.reconciled_operation_ids == {
            operations[0].id,
            operations[1].id,
            operations[2].id,
        }
        assert result.expired_reservation_ids == {expired.reservation_id}
        assert result.stale_local_leases == {
            (expired.reservation_id, 1),
            (current.reservation_id, 1),
        }
        assert leases.leases[expired.reservation_id].released_at == NOW
        assert inventory.calls == [
            (AGENT_ID, OLD_BOOT_ID, report.generated_at, report.bench_snapshots, NOW)
        ]
        RemoteCommand.model_validate(work.commands[succeeded.id].model_dump())
        DistributedOperation.model_validate(work.operations[operations[0].id].model_dump())

    asyncio.run(scenario())


def test_reconciliation_preserves_a_newer_operation_result() -> None:
    async def scenario() -> None:
        command = _command(1)
        operation = _operation(1, command).model_copy(
            update={
                "last_agent_update_at": NOW,
                "result": {"latest": True},
            }
        )
        report = _report(
            active=(
                _state(
                    command,
                    RemoteCommandStatus.RUNNING,
                    updated_at=NOW - timedelta(seconds=1),
                    result={"latest": False},
                ),
            )
        )
        service, _, _, work, _, _ = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID),
            commands=(command,),
            operations=(operation,),
        )

        await service.reconcile(UUID(int=30_008), report)

        reconciled = work.operations[operation.id]
        assert reconciled.status is DistributedOperationStatus.RUNNING
        assert reconciled.result == {"latest": True}
        assert reconciled.last_agent_update_at == NOW

    asyncio.run(scenario())


def test_transient_reconnect_keeps_missing_work_pending_reconciliation() -> None:
    async def scenario() -> None:
        command = _command(1)
        operation = _operation(1, command)
        service, _, _, work, _, _ = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID),
            commands=(command,),
            operations=(operation,),
        )

        result = await service.reconcile(UUID(int=30_002), _report())

        assert work.commands[command.id].status is RemoteCommandStatus.UNKNOWN
        assert work.operations[operation.id].status is DistributedOperationStatus.RECONCILING
        assert not result.reconciled_command_ids
        assert not result.reconciled_operation_ids
        assert not result.restarted

    asyncio.run(scenario())


def test_new_boot_fails_all_nonterminal_work_and_replays_terminal_history() -> None:
    async def scenario() -> None:
        missing = _command(1)
        accepted = _command(2)
        running = _command(3)
        succeeded = _command(4)
        missing_operation = _operation(1, missing)
        accepted_operation = _operation(2, accepted)
        running_operation = _operation(3, running)
        succeeded_operation = _operation(4, succeeded)
        report = _report(
            boot_id=NEW_BOOT_ID,
            active=(
                _state(accepted, RemoteCommandStatus.ACCEPTED),
                _state(running, RemoteCommandStatus.RUNNING),
            ),
            recent=(_state(succeeded, RemoteCommandStatus.SUCCEEDED),),
        )
        service, _, _, work, _, _ = _reconciliation_stack(
            context=AgentBootContext(NEW_BOOT_ID, OLD_BOOT_ID),
            commands=(missing, accepted, running, succeeded),
            operations=(
                missing_operation,
                accepted_operation,
                running_operation,
                succeeded_operation,
            ),
        )

        result = await service.reconcile(UUID(int=30_003), report)

        for command in (missing, accepted, running):
            interrupted_command = work.commands[command.id]
            assert interrupted_command.status is RemoteCommandStatus.FAILED
            assert interrupted_command.error_code == AgentRestartedDuringOperationError.code
        for operation in (missing_operation, accepted_operation, running_operation):
            interrupted_operation = work.operations[operation.id]
            assert interrupted_operation.status is DistributedOperationStatus.FAILED
            assert interrupted_operation.error_code == AgentRestartedDuringOperationError.code
        assert work.commands[succeeded.id].status is RemoteCommandStatus.SUCCEEDED
        assert (
            work.operations[succeeded_operation.id].status is DistributedOperationStatus.SUCCEEDED
        )
        assert result.restarted
        assert result.interrupted_operation_ids == {
            missing_operation.id,
            accepted_operation.id,
            running_operation.id,
        }

    asyncio.run(scenario())


def test_reconciliation_deduplicates_report_id_and_content() -> None:
    async def scenario() -> None:
        command_a = _command(1)
        command_b = _command(2)
        first_order = _report(
            active=(
                _state(command_a, RemoteCommandStatus.RUNNING),
                _state(command_b, RemoteCommandStatus.ACCEPTED),
            ),
            benches=(_bench_snapshot("bench-b"), _bench_snapshot("bench-a")),
        )
        reverse_order = _report(
            active=tuple(reversed(first_order.active_commands)),
            benches=tuple(reversed(first_order.bench_snapshots)),
        )
        service, _, _, _, _, inventory = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID)
        )
        report_id = UUID(int=30_004)

        original = await service.reconcile(report_id, first_order)
        same_id = await service.reconcile(report_id, first_order)
        same_content = await service.reconcile(UUID(int=30_005), reverse_order)

        assert reconciliation_report_digest(first_order) == reconciliation_report_digest(
            reverse_order
        )
        assert not original.deduplicated
        assert same_id.deduplicated
        assert same_content.deduplicated
        assert same_content.report_id == UUID(int=30_005)
        assert len(inventory.calls) == 1

        changed = first_order.model_copy(update={"buffered_event_count": 1})
        with pytest.raises(ProtocolMessageInvalidError):
            await service.reconcile(report_id, changed)
        with pytest.raises(ProtocolMessageInvalidError):
            await service.reconcile(UUID(int=30_005), changed)

    asyncio.run(scenario())


def test_invalid_boot_abandons_claim_and_can_be_retried() -> None:
    async def scenario() -> None:
        service, reports, boots, _, _, inventory = _reconciliation_stack(
            context=AgentBootContext(NEW_BOOT_ID, OLD_BOOT_ID)
        )
        report_id = UUID(int=30_006)
        report = _report(boot_id=OLD_BOOT_ID)

        with pytest.raises(ProtocolMessageInvalidError):
            await service.reconcile(report_id, report)
        assert report_id not in reports.by_id
        assert not inventory.calls

        boots.context = AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID)
        result = await service.reconcile(report_id, report)
        assert result.report_id == report_id
        assert len(inventory.calls) == 1

    asyncio.run(scenario())


def test_reconciliation_cas_does_not_overwrite_concurrent_terminal_truth() -> None:
    async def scenario() -> None:
        command = _command(1)
        operation = _operation(1, command)
        service, _, _, work, _, _ = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID, OLD_BOOT_ID),
            commands=(command,),
            operations=(operation,),
        )
        work.command_races[command.id] = command.model_copy(
            update={
                "status": RemoteCommandStatus.SUCCEEDED,
                "completed_at": NOW,
            }
        )
        work.operation_races[operation.id] = operation.model_copy(
            update={
                "status": DistributedOperationStatus.SUCCEEDED,
                "completed_at": NOW,
            }
        )
        report = _report(recent=(_state(command, RemoteCommandStatus.FAILED),))

        result = await service.reconcile(UUID(int=30_007), report)

        assert work.commands[command.id].status is RemoteCommandStatus.SUCCEEDED
        assert work.operations[operation.id].status is DistributedOperationStatus.SUCCEEDED
        assert not result.reconciled_command_ids
        assert not result.reconciled_operation_ids

    asyncio.run(scenario())


def test_reconciliation_timeout_fails_only_due_operations_with_stable_code() -> None:
    async def scenario() -> None:
        due_command = _command(1)
        future_command = _command(2)
        no_deadline_command = _command(3)
        due = _operation(1, due_command, deadline=NOW - timedelta(seconds=1))
        future = _operation(2, future_command, deadline=NOW + timedelta(seconds=1))
        no_deadline = _operation(3, no_deadline_command).model_copy(
            update={"reconciliation_deadline": None}
        )
        service, _, _, work, _, _ = _reconciliation_stack(
            context=AgentBootContext(OLD_BOOT_ID),
            commands=(due_command, future_command, no_deadline_command),
            operations=(due, future, no_deadline),
        )

        assert await service.timeout_unreconciled_operations() == 1
        assert work.operations[due.id].status is DistributedOperationStatus.FAILED
        assert work.operations[due.id].error_code == RemoteOperationReconciliationTimeoutError.code
        assert work.commands[due_command.id].status is RemoteCommandStatus.FAILED
        assert (
            work.commands[due_command.id].error_code
            == RemoteOperationReconciliationTimeoutError.code
        )
        assert work.operations[future.id].status is DistributedOperationStatus.UNKNOWN
        assert work.operations[no_deadline.id].status is DistributedOperationStatus.UNKNOWN

    asyncio.run(scenario())


class FakeDrainRepository:
    def __init__(self, *snapshots: AgentDrainSnapshot) -> None:
        self.snapshots = {snapshot.agent.id: snapshot for snapshot in snapshots}
        self.cas_calls: list[tuple[UUID, AgentStatus, AgentStatus, bool, bool]] = []
        self.cancel_statuses: list[AgentStatus] = []
        self.force_status_on_next_cas: AgentStatus | None = None
        self.disconnect_on_connection_required = False
        self.work_on_idle_check: AgentWorkload | None = None

    async def get_drain_snapshot(self, agent_id: UUID) -> AgentDrainSnapshot | None:
        return self.snapshots.get(agent_id)

    async def compare_and_set_status(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
        target_status: AgentStatus,
        require_idle: bool = False,
        require_active_connection: bool = False,
    ) -> AgentDrainSnapshot | None:
        self.cas_calls.append(
            (
                agent_id,
                expected_status,
                target_status,
                require_idle,
                require_active_connection,
            )
        )
        snapshot = self.snapshots[agent_id]
        if self.force_status_on_next_cas is not None:
            forced = self.force_status_on_next_cas
            self.force_status_on_next_cas = None
            self.snapshots[agent_id] = AgentDrainSnapshot(
                agent=snapshot.agent.model_copy(update={"status": forced}),
                workload=snapshot.workload,
                has_active_connection=snapshot.has_active_connection,
            )
            return None
        if require_active_connection and self.disconnect_on_connection_required:
            self.disconnect_on_connection_required = False
            self.snapshots[agent_id] = AgentDrainSnapshot(
                agent=snapshot.agent,
                workload=snapshot.workload,
                has_active_connection=False,
            )
            return None
        if require_idle and self.work_on_idle_check is not None:
            workload = self.work_on_idle_check
            self.work_on_idle_check = None
            self.snapshots[agent_id] = AgentDrainSnapshot(
                agent=snapshot.agent,
                workload=workload,
                has_active_connection=snapshot.has_active_connection,
            )
            return None
        if snapshot.agent.status is not expected_status:
            return None
        if require_idle and not snapshot.workload.idle:
            return None
        if require_active_connection and not snapshot.has_active_connection:
            return None
        updated = AgentDrainSnapshot(
            agent=snapshot.agent.model_copy(update={"status": target_status}),
            workload=snapshot.workload,
            has_active_connection=snapshot.has_active_connection,
        )
        self.snapshots[agent_id] = updated
        return updated

    async def cancel_queued_work(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
    ) -> int | None:
        snapshot = self.snapshots[agent_id]
        self.cancel_statuses.append(snapshot.agent.status)
        if snapshot.agent.status is not expected_status:
            return None
        cancelled = snapshot.workload.queued_ci_sessions
        self.snapshots[agent_id] = AgentDrainSnapshot(
            agent=snapshot.agent,
            workload=AgentWorkload(
                active_operations=snapshot.workload.active_operations,
                active_reservations=snapshot.workload.active_reservations,
            ),
            has_active_connection=snapshot.has_active_connection,
        )
        return cancelled

    def set_workload(self, agent_id: UUID, workload: AgentWorkload) -> None:
        snapshot = self.snapshots[agent_id]
        self.snapshots[agent_id] = AgentDrainSnapshot(
            agent=snapshot.agent,
            workload=workload,
            has_active_connection=snapshot.has_active_connection,
        )

    def set_connection(self, agent_id: UUID, active: bool) -> None:
        snapshot = self.snapshots[agent_id]
        self.snapshots[agent_id] = AgentDrainSnapshot(
            agent=snapshot.agent,
            workload=snapshot.workload,
            has_active_connection=active,
        )


def _drain_snapshot(
    status: AgentStatus,
    *,
    number: int = 1,
    workload: AgentWorkload | None = None,
    connected: bool = True,
) -> AgentDrainSnapshot:
    return AgentDrainSnapshot(
        agent=_agent(status, number=number),
        workload=workload or AgentWorkload(),
        has_active_connection=connected,
    )


def test_drain_closes_gate_and_waits_for_every_workload_class() -> None:
    async def scenario() -> None:
        repository = FakeDrainRepository(
            _drain_snapshot(
                AgentStatus.ONLINE,
                workload=AgentWorkload(
                    active_operations=1,
                    active_reservations=1,
                    queued_ci_sessions=1,
                ),
            )
        )
        service = AgentDrainService(repository)

        result = await service.drain(AGENT_ID)
        assert result.agent.status is AgentStatus.DRAINING
        with pytest.raises(AgentDrainingError):
            await service.require_accepting_new_work(AGENT_ID)

        repository.set_workload(
            AGENT_ID,
            AgentWorkload(active_operations=1),
        )
        assert (await service.refresh(AGENT_ID)).agent.status is AgentStatus.DRAINING
        repository.set_workload(AGENT_ID, AgentWorkload())
        final = await service.refresh(AGENT_ID)
        assert final.agent.status is AgentStatus.DRAINED
        assert repository.cas_calls[-1][3]

    asyncio.run(scenario())


def test_drain_optionally_cancels_queue_only_after_assignment_gate_closes() -> None:
    async def scenario() -> None:
        repository = FakeDrainRepository(
            _drain_snapshot(
                AgentStatus.ONLINE,
                workload=AgentWorkload(queued_ci_sessions=3),
            )
        )
        service = AgentDrainService(repository)

        result = await service.drain(AGENT_ID, cancel_queued_work=True)

        assert result.cancelled_queued_work == 3
        assert result.agent.status is AgentStatus.DRAINED
        assert repository.cancel_statuses == [AgentStatus.DRAINING]

    asyncio.run(scenario())


def test_idle_drain_transition_rechecks_work_atomically() -> None:
    async def scenario() -> None:
        repository = FakeDrainRepository(_drain_snapshot(AgentStatus.ONLINE))
        repository.work_on_idle_check = AgentWorkload(active_operations=1)
        service = AgentDrainService(repository)

        result = await service.drain(AGENT_ID)

        assert result.agent.status is AgentStatus.DRAINING
        assert result.workload.active_operations == 1

    asyncio.run(scenario())


def test_undrain_requires_connection_in_the_status_cas() -> None:
    async def scenario() -> None:
        repository = FakeDrainRepository(_drain_snapshot(AgentStatus.DRAINED, connected=False))
        service = AgentDrainService(repository)

        with pytest.raises(AgentOfflineError):
            await service.undrain(AGENT_ID)
        assert repository.snapshots[AGENT_ID].agent.status is AgentStatus.DRAINED

        repository.set_connection(AGENT_ID, True)
        repository.disconnect_on_connection_required = True
        with pytest.raises(AgentOfflineError):
            await service.undrain(AGENT_ID)
        assert repository.snapshots[AGENT_ID].agent.status is AgentStatus.DRAINED

        repository.set_connection(AGENT_ID, True)
        agent = await service.undrain(AGENT_ID)
        assert agent.status is AgentStatus.ONLINE
        assert repository.cas_calls[-1][4]
        assert (await service.require_accepting_new_work(AGENT_ID)).status is AgentStatus.ONLINE

    asyncio.run(scenario())


def test_drain_cas_conflict_never_overwrites_concurrent_offline_transition() -> None:
    async def scenario() -> None:
        repository = FakeDrainRepository(
            _drain_snapshot(
                AgentStatus.ONLINE,
                workload=AgentWorkload(active_operations=1),
            )
        )
        repository.force_status_on_next_cas = AgentStatus.OFFLINE
        service = AgentDrainService(repository)

        with pytest.raises(AgentDrainingError):
            await service.drain(AGENT_ID)
        assert repository.snapshots[AGENT_ID].agent.status is AgentStatus.OFFLINE

    asyncio.run(scenario())


def test_drain_status_validation_and_degraded_entry() -> None:
    async def scenario() -> None:
        degraded = _drain_snapshot(AgentStatus.DEGRADED, number=1)
        offline = _drain_snapshot(AgentStatus.OFFLINE, number=2, connected=False)
        incompatible = _drain_snapshot(AgentStatus.INCOMPATIBLE, number=3)
        revoked = _drain_snapshot(AgentStatus.REVOKED, number=4)
        repository = FakeDrainRepository(degraded, offline, incompatible, revoked)
        service = AgentDrainService(repository)

        assert (await service.drain(degraded.agent.id)).agent.status is AgentStatus.DRAINED
        with pytest.raises(AgentOfflineError):
            await service.drain(offline.agent.id)
        with pytest.raises(AgentIncompatibleError):
            await service.drain(incompatible.agent.id)
        with pytest.raises(AgentRevokedError):
            await service.drain(revoked.agent.id)
        with pytest.raises(AgentNotFoundError):
            await service.drain(UUID(int=999))

    asyncio.run(scenario())
