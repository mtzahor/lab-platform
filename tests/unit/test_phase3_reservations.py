from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core import (
    BenchOperationInProgressError,
    FakeClock,
    ReservationExtensionConflictError,
    ReservationOwnerMismatchError,
    SchedulingService,
)
from lab_platform.core.reservations import ReservationService
from lab_platform.models import (
    BenchOperationLock,
    QueueEntry,
    QueueEntryStatus,
    Reservation,
    ReservationStatus,
    TimelineCategory,
)
from lab_platform.persistence import (
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationLockRepository,
    SQLiteQueueRepository,
    SQLiteTimedReservationRepository,
    SQLiteTimelineRepository,
)

NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


@dataclass
class Availability:
    offline: set[str] = field(default_factory=set)

    def is_online(self, bench_id: str) -> bool:
        return bench_id not in self.offline


@dataclass
class Stack:
    database: SQLiteDatabase
    reservations: SQLiteTimedReservationRepository
    queues: SQLiteQueueRepository
    locks: SQLiteOperationLockRepository
    events: SQLiteEventRepository
    timeline: SQLiteTimelineRepository
    service: ReservationService
    scheduler: SchedulingService
    clock: FakeClock
    availability: Availability


def _stack(path: Path, *, clock: FakeClock | None = None) -> Stack:
    database = SQLiteDatabase(path)
    database.initialize()
    reservations = SQLiteTimedReservationRepository(database)
    queues = SQLiteQueueRepository(database)
    locks = SQLiteOperationLockRepository(database)
    events = SQLiteEventRepository(database)
    timeline = SQLiteTimelineRepository(database)
    fake_clock = clock or FakeClock(NOW)
    availability = Availability()
    service = ReservationService(
        reservations,
        queues,
        events,
        clock=fake_clock,
        availability=availability,
        operation_locks=locks,
        default_duration_seconds=30 * 60,
        maximum_duration_seconds=4 * 60 * 60,
    )
    scheduler = SchedulingService(
        reservations,
        queues,
        locks,
        events,
        availability,
        clock=fake_clock,
        scheduled_protection_window_seconds=5 * 60,
    )
    return Stack(
        database,
        reservations,
        queues,
        locks,
        events,
        timeline,
        service,
        scheduler,
        fake_clock,
        availability,
    )


def test_fake_clock_is_utc_and_never_sleeps() -> None:
    clock = FakeClock("2026-07-20T10:00:00+02:00")
    assert clock.now() == datetime(2026, 7, 20, 8, tzinfo=UTC)
    assert clock.advance(minutes=10) == datetime(2026, 7, 20, 8, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 7, 20, 10))


def test_immediate_future_idempotency_owner_and_extension_rules(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "reservations.db")
        try:
            first = await stack.service.create(
                "bench-01",
                "alice",
                duration_seconds=10 * 60,
                idempotency_key="request-1",
            )
            assert isinstance(first, Reservation)
            repeated = await stack.service.create(
                "bench-01",
                "alice",
                duration_seconds=60,
                idempotency_key="request-1",
            )
            assert repeated == first
            assert await stack.service.reserve("bench-01", "alice") == first

            future = await stack.service.create(
                "bench-01",
                "bob",
                starts_at=NOW + timedelta(minutes=15),
                duration_seconds=10 * 60,
            )
            assert isinstance(future, Reservation)
            assert future.status is ReservationStatus.SCHEDULED
            with pytest.raises(ReservationExtensionConflictError):
                await stack.service.extend(first.id, "alice", 6 * 60)
            with pytest.raises(ReservationOwnerMismatchError):
                await stack.service.release(first.id, "bob")

            released = await stack.service.release(first.id, "alice")
            assert released is not None
            assert released.status is ReservationStatus.RELEASED
            assert await stack.service.release(first.id, "alice") == released
            assert await stack.service.release("missing-bench", "alice") is None
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_future_activation_expiry_and_scheduler_idempotency(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "scheduler.db")
        try:
            scheduled = await stack.service.create(
                "bench-01",
                "alice",
                starts_at=NOW + timedelta(minutes=10),
                duration_seconds=5 * 60,
            )
            assert isinstance(scheduled, Reservation)
            assert await stack.scheduler.process_due_reservations() == 0
            stack.clock.advance(minutes=10)
            assert await stack.scheduler.process_due_reservations() == 1
            assert await stack.scheduler.process_due_reservations() == 0
            active = await stack.service.get(scheduled.id)
            assert active.status is ReservationStatus.ACTIVE

            stack.clock.advance(minutes=5)
            assert await stack.scheduler.expire_reservations() == 1
            assert await stack.scheduler.expire_reservations() == 0
            expired = await stack.service.get(scheduled.id)
            assert expired.status is ReservationStatus.EXPIRED
            events = await stack.events.list(
                bench_id="bench-01", event_type="RESERVATION_EXPIRED", limit=10
            )
            assert len(events) == 1
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_fifo_queue_promotion_and_scheduled_protection_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "queues.db")
        try:
            current = await stack.service.create("fifo", "owner", duration_seconds=60)
            assert isinstance(current, Reservation)
            alice = await stack.service.enqueue("fifo", "alice", duration_seconds=5 * 60)
            stack.clock.advance(seconds=1)
            bob = await stack.service.enqueue("fifo", "bob", duration_seconds=5 * 60)
            assert (alice.position, bob.position) == (1, 2)
            await stack.service.release(current.id, "owner")
            assert await stack.scheduler.promote_queues() == 1
            assert await stack.scheduler.promote_queues() == 0
            active = await stack.service.get_active("fifo")
            assert active is not None and active.owner == "alice"
            assert (await stack.queues.get(alice.id)).status is QueueEntryStatus.PROMOTED  # type: ignore[union-attr]
            assert (await stack.queues.get(bob.id)).position == 1  # type: ignore[union-attr]

            future = await stack.service.create(
                "protected",
                "scheduled-owner",
                starts_at=stack.clock.now() + timedelta(minutes=20),
                duration_seconds=10 * 60,
            )
            assert isinstance(future, Reservation)
            await stack.service.enqueue("protected", "queued-owner", duration_seconds=16 * 60)
            assert await stack.scheduler.promote_queues() == 0
            assert await stack.service.get_active("protected") is None

            stack.availability.offline.add("offline")
            offline = await stack.service.enqueue("offline", "carol", duration_seconds=60)
            assert await stack.scheduler.promote_queues() == 0
            assert (await stack.queues.get(offline.id)).status is QueueEntryStatus.WAITING  # type: ignore[union-attr]
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_fifo_uses_insert_order_when_timestamps_are_equal(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "fifo-ties.db")
        try:
            first = await stack.queues.create(
                QueueEntry(
                    id=UUID(int=2),
                    bench_id="bench-01",
                    owner="first",
                    requested_duration_seconds=60,
                    created_at=stack.clock.now(),
                )
            )
            second = await stack.queues.create(
                QueueEntry(
                    id=UUID(int=1),
                    bench_id="bench-01",
                    owner="second",
                    requested_duration_seconds=60,
                    created_at=stack.clock.now(),
                )
            )

            assert (first.position, second.position) == (1, 2)
            assert [entry.owner for entry in await stack.queues.list(bench_id="bench-01")] == [
                "first",
                "second",
            ]
            assert await stack.scheduler.promote_queues() == 1
            active = await stack.service.get_active("bench-01")
            assert active is not None and active.owner == "first"
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_expiry_waits_for_operation_and_persistent_locks_recover(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "locks.db"
        stack = _stack(path)
        try:
            reservation = await stack.service.create("bench-01", "alice", duration_seconds=60)
            assert isinstance(reservation, Reservation)
            operation_id = uuid4()
            lock = BenchOperationLock(
                bench_id="bench-01",
                operation_id=operation_id,
                acquired_at=stack.clock.now(),
            )
            assert await stack.locks.acquire(lock) == lock
            with pytest.raises(BenchOperationInProgressError):
                await stack.locks.acquire(lock.model_copy(update={"operation_id": uuid4()}))
            stack.clock.advance(minutes=1)
            assert await stack.scheduler.expire_reservations() == 1
            pending = await stack.service.get(reservation.id)
            assert pending.status is ReservationStatus.EXPIRED_PENDING_OPERATION
            assert pending.release_pending
            stack.database.close()

            restarted = _stack(path, clock=stack.clock)
            try:
                persisted = await restarted.locks.get("bench-01")
                assert persisted is not None and persisted.operation_id == operation_id
                assert await restarted.locks.recover_stale(set(), restarted.clock.now()) == 1
                assert await restarted.scheduler.expire_reservations() == 1
                final = await restarted.service.get(reservation.id)
                assert final.status is ReservationStatus.EXPIRED
            finally:
                restarted.database.close()
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_queue_order_and_timeline_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "restart.db"
        first = _stack(path)
        one = await first.service.enqueue("bench-01", "alice", idempotency_key="one")
        first.clock.advance(seconds=1)
        two = await first.service.enqueue("bench-01", "bob", idempotency_key="two")
        first.database.close()

        second = _stack(path, clock=first.clock)
        try:
            queue = await second.queues.list(bench_id="bench-01")
            assert [entry.id for entry in queue] == [one.id, two.id]
            assert [entry.position for entry in queue] == [1, 2]
            timeline = await second.timeline.list_timeline(
                "bench-01", category=TimelineCategory.RESERVATION
            )
            assert [entry.event_type for entry in timeline] == [
                "QUEUE_ENTRY_CREATED",
                "QUEUE_ENTRY_CREATED",
            ]
            repeated = await second.service.enqueue("bench-01", "alice", idempotency_key="one")
            assert repeated.id == one.id
            assert len(await second.queues.list(bench_id="bench-01")) == 2
        finally:
            second.database.close()

    asyncio.run(scenario())
