from __future__ import annotations

from datetime import datetime
from typing import Protocol

from lab_platform.core.clock import Clock, UtcClock
from lab_platform.core.reservation_ports import (
    OperationLockRepository,
    ReservationEventRepository,
)
from lab_platform.core.scheduling import SchedulingService
from lab_platform.models import EventRecord, RecoveryReport


class RecoverableOperationRepository(Protocol):
    async def recover_incomplete(self, now: datetime) -> int: ...


class RecoveryRecordRepository(Protocol):
    async def start(self, started_at: datetime) -> int: ...

    async def complete(self, record_id: int, report: RecoveryReport) -> None: ...


class RecoveryService:
    def __init__(
        self,
        operations: RecoverableOperationRepository,
        locks: OperationLockRepository,
        scheduler: SchedulingService,
        events: ReservationEventRepository,
        *,
        records: RecoveryRecordRepository | None = None,
        clock: Clock | None = None,
        automatic_assignment: bool = True,
    ) -> None:
        self._operations = operations
        self._locks = locks
        self._scheduler = scheduler
        self._events = events
        self._records = records
        self._clock = clock or UtcClock()
        self._automatic_assignment = automatic_assignment

    async def recover(
        self,
        *,
        interrupted_operations: int | None = None,
    ) -> RecoveryReport:
        started_at = self._clock.now()
        recovery_key = started_at.isoformat()
        record_id = await self._records.start(started_at) if self._records is not None else None
        await self._events.create(
            EventRecord(
                timestamp=started_at,
                type="RECOVERY_STARTED",
                source="recovery",
                payload={},
                deduplication_key=f"recovery:{recovery_key}:started",
            )
        )
        interrupted = (
            await self._operations.recover_incomplete(started_at)
            if interrupted_operations is None
            else interrupted_operations
        )
        locks_before_recovery = await self._locks.list()
        stale = await self._locks.recover_stale(set(), started_at)
        for lock in locks_before_recovery:
            await self._events.create(
                EventRecord(
                    timestamp=started_at,
                    type="STALE_LOCK_RECOVERED",
                    source="recovery",
                    bench_id=lock.bench_id,
                    payload={"operation_id": str(lock.operation_id)},
                    deduplication_key=(f"operation:{lock.operation_id}:stale-lock-recovered"),
                )
            )
        expired = await self._scheduler.expire_reservations()
        activated = await self._scheduler.process_due_reservations()
        promoted = await self._scheduler.promote_queues() if self._automatic_assignment else 0
        completed_at = self._clock.now()
        report = RecoveryReport(
            started_at=started_at,
            completed_at=completed_at,
            interrupted_operations=interrupted,
            stale_locks_removed=stale,
            reservations_expired=expired,
            reservations_activated=activated,
            queue_entries_promoted=promoted,
        )
        if self._records is not None and record_id is not None:
            await self._records.complete(record_id, report)
        await self._events.create(
            EventRecord(
                timestamp=completed_at,
                type="RECOVERY_COMPLETED",
                source="recovery",
                payload=report.model_dump(mode="json"),
                deduplication_key=f"recovery:{recovery_key}:completed",
            )
        )
        return report
