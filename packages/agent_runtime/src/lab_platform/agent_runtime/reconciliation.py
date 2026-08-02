from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, TypeAlias
from uuid import UUID

from lab_platform.agent_runtime.command_journal import (
    AgentCommandJournalRepository,
    StoredCommandJournalEntry,
)
from lab_platform.agent_runtime.event_buffer import AgentEventBufferRepository
from lab_platform.agent_runtime.leases import ReservationLeaseStore
from lab_platform.models import (
    ReconciliationBenchSnapshot,
    ReconciliationCommandState,
    ReconciliationReport,
    RemoteCommandStatus,
    ReservationLease,
)

Clock: TypeAlias = Callable[[], datetime]

_ACTIVE_COMMAND_STATUSES = (
    RemoteCommandStatus.ACCEPTED,
    RemoteCommandStatus.RUNNING,
)
_RECENT_COMMAND_STATUSES = (
    RemoteCommandStatus.SUCCEEDED,
    RemoteCommandStatus.FAILED,
    RemoteCommandStatus.CANCELLED,
    RemoteCommandStatus.EXPIRED,
)
_MAX_REPORT_ITEMS = 10_000


class AgentInventorySource(Protocol):
    """Agent-local inventory boundary used to construct reconciliation truth."""

    async def snapshot(self) -> Sequence[ReconciliationBenchSnapshot]: ...


class AgentReconciliationReportBuilder:
    """Build a deterministic reconciliation report from Agent-owned durable state."""

    def __init__(
        self,
        *,
        agent_id: UUID,
        boot_id: UUID,
        journal: AgentCommandJournalRepository,
        leases: ReservationLeaseStore,
        inventory: AgentInventorySource,
        events: AgentEventBufferRepository,
        recent_command_limit: int = 1_000,
        clock: Clock | None = None,
    ) -> None:
        if leases.agent_id != agent_id:
            raise ValueError("Reservation lease store belongs to a different Agent")
        if (
            isinstance(recent_command_limit, bool)
            or recent_command_limit < 1
            or recent_command_limit > _MAX_REPORT_ITEMS
        ):
            raise ValueError("recent_command_limit must be between 1 and 10,000")
        self._agent_id = agent_id
        self._boot_id = boot_id
        self._journal = journal
        self._leases = leases
        self._inventory = inventory
        self._events = events
        self._recent_command_limit = recent_command_limit
        self._clock = clock or _utc_now

    @property
    def agent_id(self) -> UUID:
        return self._agent_id

    @property
    def boot_id(self) -> UUID:
        return self._boot_id

    async def build(self, *, generated_at: datetime | None = None) -> ReconciliationReport:
        active_records: list[StoredCommandJournalEntry] = []
        for status in _ACTIVE_COMMAND_STATUSES:
            active_records.extend(await self._journal.list(status=status, limit=_MAX_REPORT_ITEMS))

        recent_records: list[StoredCommandJournalEntry] = []
        for status in _RECENT_COMMAND_STATUSES:
            recent_records.extend(await self._journal.list(status=status, limit=_MAX_REPORT_ITEMS))

        active_commands = _command_states(active_records)[:_MAX_REPORT_ITEMS]
        recent_commands = _command_states(recent_records)[: self._recent_command_limit]

        stored_leases = await self._leases.list()
        active_leases = tuple(
            sorted(
                (lease for lease in stored_leases if isinstance(lease, ReservationLease)),
                key=lambda lease: (lease.bench_id, lease.lease_version, lease.reservation_id.hex),
            )[:_MAX_REPORT_ITEMS]
        )
        bench_snapshots = tuple(
            sorted(
                await self._inventory.snapshot(),
                key=lambda bench: (bench.local_bench_id, bench.backend_id),
            )[:_MAX_REPORT_ITEMS]
        )
        event_stats = await self._events.stats()

        return ReconciliationReport(
            agent_id=self._agent_id,
            boot_id=self._boot_id,
            generated_at=_as_utc(generated_at or self._clock()),
            active_commands=tuple(active_commands),
            recent_commands=tuple(recent_commands),
            local_reservation_leases=active_leases,
            bench_snapshots=bench_snapshots,
            buffered_event_count=event_stats.buffered_events,
        )


def _command_states(
    records: Sequence[StoredCommandJournalEntry],
) -> list[ReconciliationCommandState]:
    # Keeping the sort and conversion here makes report ordering independent of the storage
    # backend's query plan.
    by_command: dict[UUID, ReconciliationCommandState] = {}
    for record in records:
        entry = record.entry
        updated_at = entry.completed_at or entry.started_at or entry.received_at
        state = ReconciliationCommandState(
            command_id=entry.command_id,
            status=entry.status,
            updated_at=updated_at,
            result=entry.result,
            error_code=entry.error_code,
            error_message=entry.error_message,
        )
        previous = by_command.get(state.command_id)
        if previous is None or previous.updated_at < state.updated_at:
            by_command[state.command_id] = state
    return sorted(
        by_command.values(),
        key=lambda state: (state.updated_at, state.command_id.hex),
        reverse=True,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reconciliation clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
