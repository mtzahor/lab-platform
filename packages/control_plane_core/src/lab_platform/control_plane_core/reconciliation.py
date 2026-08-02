from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from lab_platform.agent_protocol.errors import ProtocolMessageInvalidError
from lab_platform.control_plane_core.errors import (
    AgentOfflineError,
    AgentRestartedDuringOperationError,
    RemoteCommandExpiredError,
    RemoteOperationReconciliationTimeoutError,
)
from lab_platform.models import (
    DistributedOperation,
    DistributedOperationStatus,
    ReconciliationBenchSnapshot,
    ReconciliationCommandState,
    ReconciliationReport,
    RemoteCommand,
    RemoteCommandStatus,
    ReservationLease,
)

_RECONCILABLE_COMMAND_STATUSES = frozenset({RemoteCommandStatus.UNKNOWN})
_RECONCILABLE_OPERATION_STATUSES = frozenset(
    {
        DistributedOperationStatus.UNKNOWN,
        DistributedOperationStatus.RECONCILING,
    }
)
_TERMINAL_REPORT_STATUSES = frozenset(
    {
        RemoteCommandStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED,
        RemoteCommandStatus.CANCELLED,
        RemoteCommandStatus.EXPIRED,
    }
)
_ACTIVE_REPORT_STATUSES = frozenset(
    {
        RemoteCommandStatus.ACCEPTED,
        RemoteCommandStatus.RUNNING,
    }
)


@dataclass(frozen=True, slots=True)
class AgentBootContext:
    """The current process and, when known, the process that lost the work."""

    active_boot_id: UUID
    interrupted_boot_id: UUID | None = None

    @property
    def restarted(self) -> bool:
        return (
            self.interrupted_boot_id is not None and self.interrupted_boot_id != self.active_boot_id
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    report_id: UUID
    report_digest: str
    agent_id: UUID
    boot_id: UUID
    restarted: bool
    reconciled_command_ids: frozenset[UUID]
    reconciled_operation_ids: frozenset[UUID]
    interrupted_operation_ids: frozenset[UUID]
    expired_reservation_ids: frozenset[UUID]
    stale_local_leases: frozenset[tuple[UUID, int]]
    inventory_reconciled: bool
    deduplicated: bool = False


class ReportClaimStatus(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    IN_PROGRESS = "IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class ReportClaim:
    status: ReportClaimStatus
    prior_result: ReconciliationResult | None = None


class ReconciliationReportStore(Protocol):
    """Durable, atomic report-id and content-digest deduplication boundary.

    A DUPLICATE claim must also bind the received report ID to that digest so the
    same ID cannot later be reused with different content.
    """

    async def claim_report(
        self,
        report_id: UUID,
        *,
        agent_id: UUID,
        content_digest: str,
        received_at: datetime,
    ) -> ReportClaim: ...

    async def complete_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
        result: ReconciliationResult,
    ) -> None: ...

    async def abandon_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
    ) -> None: ...


class ReconciliationBootRepository(Protocol):
    async def get_boot_context(self, agent_id: UUID) -> AgentBootContext | None: ...


class ReconciliationWorkRepository(Protocol):
    """CAS persistence for commands and their correlated operations."""

    async def get_command(self, command_id: UUID) -> RemoteCommand | None: ...

    async def list_commands(
        self,
        *,
        agent_id: UUID,
        statuses: Iterable[RemoteCommandStatus],
        limit: int,
    ) -> list[RemoteCommand]: ...

    async def update_command(
        self,
        command: RemoteCommand,
        *,
        expected_statuses: Iterable[RemoteCommandStatus],
    ) -> RemoteCommand | None: ...

    async def list_operations(
        self,
        *,
        agent_id: UUID | None,
        statuses: Iterable[DistributedOperationStatus],
        limit: int,
    ) -> list[DistributedOperation]: ...

    async def update_operation(
        self,
        operation: DistributedOperation,
        *,
        expected_statuses: Iterable[DistributedOperationStatus],
    ) -> DistributedOperation | None: ...


class ReconciliationLeaseRepository(Protocol):
    async def list_leases(self, agent_id: UUID) -> list[ReservationLease]: ...

    async def release_if_current(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        expected_lease_version: int,
        released_at: datetime,
    ) -> ReservationLease | None: ...


class ReconciliationInventory(Protocol):
    async def reconcile_report_inventory(
        self,
        agent_id: UUID,
        *,
        boot_id: UUID,
        generated_at: datetime,
        snapshots: tuple[ReconciliationBenchSnapshot, ...],
        observed_at: datetime,
    ) -> object: ...


class ReconciliationService:
    """Apply Agent journal truth to work whose control-plane state is uncertain."""

    def __init__(
        self,
        reports: ReconciliationReportStore,
        boots: ReconciliationBootRepository,
        work: ReconciliationWorkRepository,
        leases: ReconciliationLeaseRepository,
        inventory: ReconciliationInventory,
        *,
        clock: Callable[[], datetime] | None = None,
        reconciliation_batch_limit: int = 10_000,
    ) -> None:
        if reconciliation_batch_limit <= 0:
            raise ValueError("reconciliation_batch_limit must be positive")
        self._reports = reports
        self._boots = boots
        self._work = work
        self._leases = leases
        self._inventory = inventory
        self._clock = clock or _utc_now
        self._batch_limit = reconciliation_batch_limit

    async def reconcile(
        self,
        report_id: UUID,
        report: ReconciliationReport,
    ) -> ReconciliationResult:
        now = _as_utc(self._clock(), field="reconciliation receipt timestamp")
        digest = reconciliation_report_digest(report)
        claim = await self._reports.claim_report(
            report_id,
            agent_id=report.agent_id,
            content_digest=digest,
            received_at=now,
        )
        if claim.status is ReportClaimStatus.DUPLICATE:
            if claim.prior_result is None:
                raise ProtocolMessageInvalidError(
                    "Completed reconciliation report is missing its durable result.",
                    report_id=str(report_id),
                )
            return replace(
                claim.prior_result,
                report_id=report_id,
                deduplicated=True,
            )
        if claim.status is ReportClaimStatus.CONFLICT:
            raise ProtocolMessageInvalidError(
                "Reconciliation report ID was reused with different content.",
                report_id=str(report_id),
                agent_id=str(report.agent_id),
            )
        if claim.status is ReportClaimStatus.IN_PROGRESS:
            raise ProtocolMessageInvalidError(
                "Reconciliation report is already being processed.",
                report_id=str(report_id),
                agent_id=str(report.agent_id),
            )

        try:
            result = await self._apply_report(report_id, digest, report, now=now)
            await self._reports.complete_report(
                report_id,
                content_digest=digest,
                result=result,
            )
        except BaseException:
            await self._reports.abandon_report(report_id, content_digest=digest)
            raise
        return result

    async def timeout_unreconciled_operations(self, *, limit: int = 500) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = _as_utc(self._clock(), field="reconciliation timeout timestamp")
        operations = await self._work.list_operations(
            agent_id=None,
            statuses=_RECONCILABLE_OPERATION_STATUSES,
            limit=limit,
        )
        timed_out = 0
        for operation in operations:
            if operation.reconciliation_deadline is None or operation.reconciliation_deadline > now:
                continue
            completed_at = _operation_effective_time(operation, now)
            candidate = operation.model_copy(
                update={
                    "status": DistributedOperationStatus.FAILED,
                    "completed_at": completed_at,
                    "last_agent_update_at": completed_at,
                    "reconciliation_deadline": None,
                    "error_code": RemoteOperationReconciliationTimeoutError.code,
                    "error_message": "Agent did not reconcile operation before its deadline",
                }
            )
            persisted = await self._work.update_operation(
                candidate,
                expected_statuses={operation.status},
            )
            if persisted is None:
                continue
            timed_out += 1
            command = await self._work.get_command(operation.remote_command_id)
            if command is None or command.status is not RemoteCommandStatus.UNKNOWN:
                continue
            command_completed_at = _command_effective_time(command, now)
            failed_command = command.model_copy(
                update={
                    "status": RemoteCommandStatus.FAILED,
                    "completed_at": command_completed_at,
                    "error_code": RemoteOperationReconciliationTimeoutError.code,
                    "error_message": "Agent did not reconcile operation before its deadline",
                }
            )
            await self._work.update_command(
                failed_command,
                expected_statuses={RemoteCommandStatus.UNKNOWN},
            )
        return timed_out

    async def _apply_report(
        self,
        report_id: UUID,
        digest: str,
        report: ReconciliationReport,
        *,
        now: datetime,
    ) -> ReconciliationResult:
        boot_context = await self._boots.get_boot_context(report.agent_id)
        if boot_context is None:
            raise AgentOfflineError(
                "Agent has no active connection for reconciliation.",
                agent_id=str(report.agent_id),
            )
        if boot_context.active_boot_id != report.boot_id:
            raise ProtocolMessageInvalidError(
                "Reconciliation report boot ID does not match the active Agent process.",
                agent_id=str(report.agent_id),
                expected_boot_id=str(boot_context.active_boot_id),
                received_boot_id=str(report.boot_id),
            )
        _validate_report_lease_owners(report)
        states = _resolve_report_command_states(report)

        commands = await self._work.list_commands(
            agent_id=report.agent_id,
            statuses=_RECONCILABLE_COMMAND_STATUSES,
            limit=self._batch_limit,
        )
        operations = await self._work.list_operations(
            agent_id=report.agent_id,
            statuses=_RECONCILABLE_OPERATION_STATUSES,
            limit=self._batch_limit,
        )
        reconciling_operations = await self._enter_reconciling(operations)

        reconciled_commands: set[UUID] = set()
        for command in commands:
            state = states.get(command.id)
            candidate = _reconciled_command(
                command,
                state,
                restarted=boot_context.restarted,
                now=now,
            )
            if candidate is None:
                continue
            if (
                await self._work.update_command(
                    candidate,
                    expected_statuses={RemoteCommandStatus.UNKNOWN},
                )
                is not None
            ):
                reconciled_commands.add(command.id)

        reconciled_operations: set[UUID] = set()
        interrupted_operations: set[UUID] = set()
        for operation in reconciling_operations:
            state = states.get(operation.remote_command_id)
            operation_candidate = _reconciled_operation(
                operation,
                state,
                restarted=boot_context.restarted,
                now=now,
            )
            if operation_candidate is None:
                continue
            persisted = await self._work.update_operation(
                operation_candidate,
                expected_statuses={DistributedOperationStatus.RECONCILING},
            )
            if persisted is None:
                continue
            reconciled_operations.add(operation.id)
            if persisted.error_code == AgentRestartedDuringOperationError.code:
                interrupted_operations.add(operation.id)

        expired_reservations, stale_local_leases = await self._reconcile_leases(
            report,
            now=now,
        )
        await self._inventory.reconcile_report_inventory(
            report.agent_id,
            boot_id=report.boot_id,
            generated_at=report.generated_at,
            snapshots=report.bench_snapshots,
            observed_at=now,
        )
        return ReconciliationResult(
            report_id=report_id,
            report_digest=digest,
            agent_id=report.agent_id,
            boot_id=report.boot_id,
            restarted=boot_context.restarted,
            reconciled_command_ids=frozenset(reconciled_commands),
            reconciled_operation_ids=frozenset(reconciled_operations),
            interrupted_operation_ids=frozenset(interrupted_operations),
            expired_reservation_ids=frozenset(expired_reservations),
            stale_local_leases=frozenset(stale_local_leases),
            inventory_reconciled=True,
        )

    async def _enter_reconciling(
        self,
        operations: list[DistributedOperation],
    ) -> list[DistributedOperation]:
        entered: list[DistributedOperation] = []
        for operation in operations:
            if operation.status is DistributedOperationStatus.RECONCILING:
                entered.append(operation)
                continue
            candidate = operation.model_copy(
                update={"status": DistributedOperationStatus.RECONCILING}
            )
            persisted = await self._work.update_operation(
                candidate,
                expected_statuses={DistributedOperationStatus.UNKNOWN},
            )
            if persisted is not None:
                entered.append(persisted)
        return entered

    async def _reconcile_leases(
        self,
        report: ReconciliationReport,
        *,
        now: datetime,
    ) -> tuple[set[UUID], set[tuple[UUID, int]]]:
        authoritative_leases = await self._leases.list_leases(report.agent_id)
        authoritative: dict[UUID, ReservationLease] = {}
        for lease in authoritative_leases:
            if lease.reservation_id in authoritative:
                raise RuntimeError("Lease repository returned duplicate reservation IDs")
            authoritative[lease.reservation_id] = lease

        expired: set[UUID] = set()
        for lease in authoritative.values():
            if lease.released_at is not None or lease.valid_until >= now:
                continue
            released = await self._leases.release_if_current(
                lease.reservation_id,
                agent_id=report.agent_id,
                expected_lease_version=lease.lease_version,
                released_at=now,
            )
            if released is not None:
                expired.add(lease.reservation_id)

        stale_local: set[tuple[UUID, int]] = set()
        for local in report.local_reservation_leases:
            if local.released_at is not None:
                continue
            current = authoritative.get(local.reservation_id)
            if (
                current is None
                or current.released_at is not None
                or current.lease_version != local.lease_version
                or current.agent_id != local.agent_id
                or current.bench_id != local.bench_id
                or current.owner != local.owner
                or local.valid_until < now
                or current.valid_until < now
            ):
                stale_local.add((local.reservation_id, local.lease_version))
        return expired, stale_local


def reconciliation_report_digest(report: ReconciliationReport) -> str:
    """Return an order-insensitive digest for report collections and JSON mappings."""

    payload = report.model_dump(mode="json")
    for field in (
        "active_commands",
        "recent_commands",
        "local_reservation_leases",
        "bench_snapshots",
    ):
        values = payload[field]
        assert isinstance(values, list)
        payload[field] = sorted(
            values,
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_report_command_states(
    report: ReconciliationReport,
) -> dict[UUID, ReconciliationCommandState]:
    resolved: dict[UUID, ReconciliationCommandState] = {}
    for state in (*report.active_commands, *report.recent_commands):
        previous = resolved.get(state.command_id)
        if previous is None or previous == state:
            resolved[state.command_id] = state
            continue
        resolved[state.command_id] = _prefer_journal_state(previous, state)
    return resolved


def _prefer_journal_state(
    left: ReconciliationCommandState,
    right: ReconciliationCommandState,
) -> ReconciliationCommandState:
    left_terminal = left.status in _TERMINAL_REPORT_STATUSES
    right_terminal = right.status in _TERMINAL_REPORT_STATUSES
    if left_terminal != right_terminal:
        return left if left_terminal else right
    if left.updated_at != right.updated_at:
        return max((left, right), key=lambda state: state.updated_at)
    if left_terminal:
        raise ProtocolMessageInvalidError(
            "Reconciliation journal has conflicting terminal states at the same timestamp.",
            command_id=str(left.command_id),
            left_status=left.status.value,
            right_status=right.status.value,
        )
    rank = {
        RemoteCommandStatus.RUNNING: 2,
        RemoteCommandStatus.ACCEPTED: 1,
    }
    return max((left, right), key=lambda state: rank.get(state.status, 0))


def _reconciled_command(
    command: RemoteCommand,
    state: ReconciliationCommandState | None,
    *,
    restarted: bool,
    now: datetime,
) -> RemoteCommand | None:
    if restarted and (state is None or state.status not in _TERMINAL_REPORT_STATUSES):
        completed_at = _command_effective_time(command, now)
        return command.model_copy(
            update={
                "status": RemoteCommandStatus.FAILED,
                "completed_at": completed_at,
                "error_code": AgentRestartedDuringOperationError.code,
                "error_message": "Agent restarted before active command execution completed",
            }
        )
    if state is None or state.status not in _ACTIVE_REPORT_STATUSES | _TERMINAL_REPORT_STATUSES:
        return None

    occurred_at = _command_effective_time(command, state.updated_at)
    if state.status is RemoteCommandStatus.ACCEPTED:
        return command.model_copy(
            update={
                "status": RemoteCommandStatus.ACCEPTED,
                "acknowledged_at": command.acknowledged_at or occurred_at,
                "started_at": None,
                "completed_at": None,
                "error_code": None,
                "error_message": None,
            }
        )
    if state.status is RemoteCommandStatus.RUNNING:
        acknowledged_at = command.acknowledged_at or occurred_at
        started_at = command.started_at or max(acknowledged_at, occurred_at)
        return command.model_copy(
            update={
                "status": RemoteCommandStatus.RUNNING,
                "acknowledged_at": acknowledged_at,
                "started_at": started_at,
                "completed_at": None,
                "error_code": None,
                "error_message": None,
            }
        )
    return command.model_copy(
        update={
            "status": state.status,
            "completed_at": occurred_at,
            "error_code": state.error_code,
            "error_message": state.error_message,
        }
    )


def _reconciled_operation(
    operation: DistributedOperation,
    state: ReconciliationCommandState | None,
    *,
    restarted: bool,
    now: datetime,
) -> DistributedOperation | None:
    if restarted and (state is None or state.status not in _TERMINAL_REPORT_STATUSES):
        completed_at = _operation_effective_time(operation, now)
        return operation.model_copy(
            update={
                "status": DistributedOperationStatus.FAILED,
                "completed_at": completed_at,
                "last_agent_update_at": completed_at,
                "reconciliation_deadline": None,
                "error_code": AgentRestartedDuringOperationError.code,
                "error_message": "Agent restarted before active command execution completed",
            }
        )
    if state is None or state.status not in _ACTIVE_REPORT_STATUSES | _TERMINAL_REPORT_STATUSES:
        return None

    occurred_at = _operation_effective_time(operation, state.updated_at)
    if state.status is RemoteCommandStatus.ACCEPTED:
        updates: dict[str, object] = {
            "status": DistributedOperationStatus.ACCEPTED,
            "started_at": None,
            "completed_at": None,
            "last_agent_update_at": occurred_at,
            "reconciliation_deadline": None,
            "error_code": None,
            "error_message": None,
        }
        if _state_has_current_result(operation, state):
            updates["result"] = state.result
        return operation.model_copy(update=updates)
    if state.status is RemoteCommandStatus.RUNNING:
        updates = {
            "status": DistributedOperationStatus.RUNNING,
            "started_at": operation.started_at or occurred_at,
            "completed_at": None,
            "last_agent_update_at": occurred_at,
            "reconciliation_deadline": None,
            "error_code": None,
            "error_message": None,
        }
        if _state_has_current_result(operation, state):
            updates["result"] = state.result
        return operation.model_copy(update=updates)
    operation_status = {
        RemoteCommandStatus.SUCCEEDED: DistributedOperationStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED: DistributedOperationStatus.FAILED,
        RemoteCommandStatus.CANCELLED: DistributedOperationStatus.CANCELLED,
        RemoteCommandStatus.EXPIRED: DistributedOperationStatus.FAILED,
    }[state.status]
    error_code = state.error_code
    error_message = state.error_message
    if state.status is RemoteCommandStatus.EXPIRED:
        error_code = error_code or RemoteCommandExpiredError.code
        error_message = error_message or "Remote command expired before reconciliation"
    updates = {
        "status": operation_status,
        "progress": (
            100 if operation_status is DistributedOperationStatus.SUCCEEDED else operation.progress
        ),
        "completed_at": occurred_at,
        "last_agent_update_at": occurred_at,
        "reconciliation_deadline": None,
        "error_code": error_code,
        "error_message": error_message,
    }
    if _state_has_current_result(operation, state):
        updates["result"] = state.result
    return operation.model_copy(update=updates)


def _state_has_current_result(
    operation: DistributedOperation,
    state: ReconciliationCommandState,
) -> bool:
    return state.result is not None and (
        operation.last_agent_update_at is None or state.updated_at >= operation.last_agent_update_at
    )


def _validate_report_lease_owners(report: ReconciliationReport) -> None:
    seen: set[tuple[UUID, int]] = set()
    for lease in report.local_reservation_leases:
        if lease.agent_id != report.agent_id:
            raise ProtocolMessageInvalidError(
                "Reconciliation report contains a lease owned by another Agent.",
                report_agent_id=str(report.agent_id),
                lease_agent_id=str(lease.agent_id),
                reservation_id=str(lease.reservation_id),
            )
        key = (lease.reservation_id, lease.lease_version)
        if key in seen:
            raise ProtocolMessageInvalidError(
                "Reconciliation report contains a duplicate reservation lease.",
                reservation_id=str(lease.reservation_id),
                lease_version=lease.lease_version,
            )
        seen.add(key)


def _command_effective_time(command: RemoteCommand, occurred_at: datetime) -> datetime:
    values = [command.created_at, _as_utc(occurred_at, field="command reconciliation timestamp")]
    values.extend(
        value
        for value in (
            command.dispatched_at,
            command.acknowledged_at,
            command.started_at,
        )
        if value is not None
    )
    return max(values)


def _operation_effective_time(
    operation: DistributedOperation,
    occurred_at: datetime,
) -> datetime:
    values = [
        operation.created_at,
        _as_utc(occurred_at, field="operation reconciliation timestamp"),
    ]
    values.extend(
        value
        for value in (
            operation.dispatched_at,
            operation.started_at,
            operation.last_agent_update_at,
        )
        if value is not None
    )
    return max(values)


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
