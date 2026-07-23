from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import uuid4

from lab_platform.core.clock import Clock, UtcClock
from lab_platform.core.queueing import FifoQueuePolicy, QueuePolicy
from lab_platform.core.reservation_ports import (
    BenchAvailability,
    OperationLockRepository,
    QueueRepository,
    ReservationEventRepository,
    TimedReservationRepository,
)
from lab_platform.models import (
    EventRecord,
    QueueEntry,
    Reservation,
    ReservationSource,
    ReservationStatus,
)


class SchedulingService:
    """Deterministic, restart-safe reservation state transitions.

    Calls are safe to repeat for the same timestamp. SQLite repositories perform
    the final compare-and-set transition inside ``BEGIN IMMEDIATE`` transactions.
    """

    def __init__(
        self,
        reservations: TimedReservationRepository,
        queues: QueueRepository,
        operation_locks: OperationLockRepository,
        events: ReservationEventRepository,
        availability: BenchAvailability,
        *,
        clock: Clock | None = None,
        queue_policy: QueuePolicy | None = None,
        scheduled_protection_window_seconds: int = 5 * 60,
        expiry_grace_seconds: int = 30,
    ) -> None:
        if scheduled_protection_window_seconds < 0 or expiry_grace_seconds < 0:
            raise ValueError("Scheduler durations cannot be negative")
        self._reservations = reservations
        self._queues = queues
        self._operation_locks = operation_locks
        self._events = events
        self._availability = availability
        self._clock = clock or UtcClock()
        self._queue_policy = queue_policy or FifoQueuePolicy()
        self._protection_window = timedelta(seconds=scheduled_protection_window_seconds)
        self._expiry_grace = timedelta(seconds=expiry_grace_seconds)
        self._bench_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_due_reservations(self) -> int:
        now = self._clock.now()
        activated = 0
        for candidate in await self._reservations.list_due(now):
            if candidate.ends_at is not None and candidate.ends_at <= now:
                continue
            async with self._bench_locks[candidate.bench_id]:
                if not self._availability.is_online(candidate.bench_id):
                    continue
                if await self._operation_locks.get(candidate.bench_id) is not None:
                    continue
                if await self._reservations.get_active(candidate.bench_id) is not None:
                    continue
                reservation = await self._reservations.activate_if_available(
                    candidate.id,
                    now,
                    event_factory=lambda activated: self._reservation_event(
                        activated,
                        "RESERVATION_ACTIVATED",
                        timestamp=now,
                    ),
                )
                if reservation is None:
                    continue
                activated += 1
        return activated

    async def expire_reservations(self) -> int:
        now = self._clock.now()
        expired = 0
        for candidate in await self._reservations.list_expired(now):
            async with self._bench_locks[candidate.bench_id]:
                reservation = await self._reservations.expire_if_due(
                    candidate.id,
                    now,
                    event_factory=lambda expired: self._expiry_event(expired, now),
                )
                if reservation is None:
                    continue
                expired += 1
        return expired

    async def promote_queues(self) -> int:
        now = self._clock.now()
        promoted_count = 0
        for bench_id in await self._queues.list_waiting_benches():
            async with self._bench_locks[bench_id]:
                if not self._availability.is_online(bench_id):
                    continue
                if await self._operation_locks.get(bench_id) is not None:
                    continue
                if await self._reservations.get_active(bench_id) is not None:
                    continue
                entries = await self._queues.list(bench_id=bench_id)
                selected = await self._queue_policy.select_next(entries)
                if selected is None:
                    continue
                requested_end = now + timedelta(seconds=selected.requested_duration_seconds)
                scheduled = await self._reservations.next_scheduled(bench_id, now)
                if (
                    scheduled is not None
                    and scheduled.starts_at is not None
                    and requested_end + self._protection_window > scheduled.starts_at
                ):
                    continue
                reservation = Reservation(
                    id=uuid4(),
                    bench_id=bench_id,
                    owner=selected.owner,
                    created_at=selected.created_at,
                    requested_at=selected.created_at,
                    starts_at=now,
                    ends_at=requested_end,
                    activated_at=now,
                    status=ReservationStatus.ACTIVE,
                    source=ReservationSource.SYSTEM,
                    metadata={"queue_entry_id": str(selected.id)},
                )
                promoted = await self._queues.promote(
                    selected.id,
                    reservation,
                    now,
                    protection_window=self._protection_window,
                    event_factory=lambda entry, created: self._promotion_events(
                        entry,
                        created,
                        now,
                    ),
                )
                if promoted is None:
                    continue
                promoted_count += 1
        return promoted_count

    async def process(self) -> tuple[int, int, int]:
        expired = await self.expire_reservations()
        activated = await self.process_due_reservations()
        promoted = await self.promote_queues()
        return activated, expired, promoted

    def _reservation_event(
        self,
        reservation: Reservation,
        event_type: str,
        *,
        timestamp: datetime,
        payload: dict[str, object] | None = None,
    ) -> EventRecord:
        event_payload: dict[str, object] = {
            "reservation_id": str(reservation.id),
            "status": reservation.status.value,
        }
        event_payload.update(payload or {})
        return EventRecord(
            timestamp=timestamp,
            type=event_type,
            source="scheduler",
            bench_id=reservation.bench_id,
            reservation_id=reservation.id,
            actor=reservation.owner,
            payload=event_payload,
            deduplication_key=f"reservation:{reservation.id}:{event_type}",
        )

    def _expiry_event(self, reservation: Reservation, timestamp: datetime) -> EventRecord:
        pending_operation = reservation.status is ReservationStatus.EXPIRED_PENDING_OPERATION
        payload: dict[str, object] = {
            "release_pending": reservation.release_pending,
            "grace_deadline": (reservation.ends_at + self._expiry_grace).isoformat()
            if reservation.ends_at is not None and pending_operation
            else None,
        }
        return self._reservation_event(
            reservation,
            "RESERVATION_EXPIRED",
            timestamp=timestamp,
            payload=payload,
        )

    def _promotion_events(
        self,
        entry: QueueEntry,
        reservation: Reservation,
        timestamp: datetime,
    ) -> tuple[EventRecord, EventRecord]:
        return (
            EventRecord(
                timestamp=timestamp,
                type="QUEUE_ENTRY_PROMOTED",
                source="scheduler",
                bench_id=reservation.bench_id,
                reservation_id=reservation.id,
                actor=reservation.owner,
                payload={
                    "queue_entry_id": str(entry.id),
                    "reservation_id": str(reservation.id),
                },
                deduplication_key=f"queue:{entry.id}:promoted",
            ),
            self._reservation_event(
                reservation,
                "RESERVATION_ACTIVATED",
                timestamp=timestamp,
            ),
        )
