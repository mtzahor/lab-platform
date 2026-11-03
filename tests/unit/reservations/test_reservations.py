from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import TypeVar
from uuid import UUID, uuid4

import pytest
from lab_platform.core import (
    BenchAlreadyReservedError,
    BenchOperationInProgressError,
    FakeClock,
    ReservationExtensionConflictError,
    ReservationOwnerMismatchError,
    ReservationTimeConflictError,
    SchedulingService,
)
from lab_platform.core.reservations import ReservationService
from lab_platform.models import (
    BenchOperationLock,
    EventRecord,
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
from lab_platform.simlab import SimLab

NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


_T = TypeVar("_T")


class AlwaysOnline:
    def is_online(self, bench_id: str) -> bool:
        return True


@dataclass(frozen=True)
class SchedulerStack:
    database: SQLiteDatabase
    reservations: SQLiteTimedReservationRepository
    queues: SQLiteQueueRepository
    locks: SQLiteOperationLockRepository
    events: SQLiteEventRepository
    service: ReservationService
    scheduler: SchedulingService
    clock: FakeClock


def _scheduler_stack(path: Path) -> SchedulerStack:
    database = SQLiteDatabase(path)
    database.initialize()
    reservations = SQLiteTimedReservationRepository(database)
    queues = SQLiteQueueRepository(database)
    locks = SQLiteOperationLockRepository(database)
    events = SQLiteEventRepository(database)
    clock = FakeClock(NOW)
    availability = AlwaysOnline()
    service = ReservationService(
        reservations,
        queues,
        events,
        clock=clock,
        availability=availability,
        operation_locks=locks,
    )
    scheduler = SchedulingService(
        reservations,
        queues,
        locks,
        events,
        availability,
        clock=clock,
        scheduled_protection_window_seconds=5 * 60,
    )
    return SchedulerStack(
        database,
        reservations,
        queues,
        locks,
        events,
        service,
        scheduler,
        clock,
    )


def _open_databases(path: Path, count: int) -> list[SQLiteDatabase]:
    databases = [SQLiteDatabase(path) for _ in range(count)]
    for database in databases:
        database.initialize()
    return databases


def _run_concurrently(count: int, operation: Callable[[int], _T]) -> list[_T]:
    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(operation, range(count)))


def test_concurrent_overlapping_schedules_have_one_atomic_winner(tmp_path: Path) -> None:
    worker_count = 16
    databases = _open_databases(tmp_path / "scheduled-race.db", worker_count)
    repositories = [SQLiteTimedReservationRepository(database) for database in databases]
    barrier = Barrier(worker_count, timeout=30)

    def create(index: int) -> Reservation | Exception:
        barrier.wait(timeout=5)
        try:
            return asyncio.run(
                repositories[index].create(
                    Reservation(
                        id=UUID(int=10_000 + index),
                        bench_id="scheduled-race",
                        owner=f"owner-{index}",
                        created_at=NOW,
                        requested_at=NOW,
                        starts_at=NOW + timedelta(hours=1),
                        ends_at=NOW + timedelta(hours=2),
                        status=ReservationStatus.SCHEDULED,
                    )
                )
            )
        except Exception as exc:
            return exc

    try:
        results = _run_concurrently(worker_count, create)
        assert sum(isinstance(item, Reservation) for item in results) == 1
        rejected = [item for item in results if isinstance(item, Exception)]
        assert len(rejected) == worker_count - 1
        assert all(isinstance(item, ReservationTimeConflictError) for item in rejected)
    finally:
        for database in databases:
            database.close()


class BarrierQueueRepository(SQLiteQueueRepository):
    """Align independent schedulers immediately before the transactional CAS."""

    def __init__(self, database: SQLiteDatabase, barrier: Barrier) -> None:
        super().__init__(database)
        self._promotion_barrier = barrier

    async def promote(
        self,
        entry_id: UUID,
        reservation: Reservation,
        now: datetime,
        *,
        protection_window: timedelta | None = None,
        event_factory: Callable[[QueueEntry, Reservation], Iterable[EventRecord]] | None = None,
    ) -> tuple[QueueEntry, Reservation] | None:
        self._promotion_barrier.wait(timeout=5)
        return await super().promote(
            entry_id,
            reservation,
            now,
            protection_window=protection_window,
            event_factory=event_factory,
        )


def test_100_benches_promote_and_expire_1000_fifo_requests(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _scheduler_stack(tmp_path / "scale.db")
        bench_count = 100
        requests_per_bench = 10
        try:
            for slot in range(requests_per_bench):
                for bench_index in range(bench_count):
                    ordinal = slot * bench_count + bench_index
                    await stack.queues.create(
                        QueueEntry(
                            id=UUID(int=ordinal + 1),
                            bench_id=f"bench-{bench_index + 1:03d}",
                            owner=f"user-{(slot * 10 + bench_index) % 100:03d}",
                            requested_duration_seconds=60,
                            created_at=NOW - timedelta(seconds=1) + timedelta(microseconds=ordinal),
                        )
                    )

            for bench_index in range(bench_count):
                bench_id = f"bench-{bench_index + 1:03d}"
                waiting = await stack.queues.list(bench_id=bench_id)
                assert [entry.owner for entry in waiting] == [
                    f"user-{(slot * 10 + bench_index) % 100:03d}"
                    for slot in range(requests_per_bench)
                ]
                assert [entry.position for entry in waiting] == list(
                    range(1, requests_per_bench + 1)
                )

            for _slot in range(requests_per_bench):
                assert await stack.scheduler.promote_queues() == bench_count
                active = [
                    await stack.reservations.get_active(f"bench-{index + 1:03d}")
                    for index in range(bench_count)
                ]
                assert all(reservation is not None for reservation in active)
                assert {reservation.owner for reservation in active if reservation is not None} == {
                    f"user-{index:03d}" for index in range(100)
                }

                stack.clock.advance(seconds=60)
                assert await stack.scheduler.expire_reservations() == bench_count
                after_expiry = [
                    await stack.reservations.get_active(f"bench-{index + 1:03d}")
                    for index in range(bench_count)
                ]
                assert all(reservation is None for reservation in after_expiry)

            persisted = await stack.reservations.list(limit=bench_count * requests_per_bench + 1)
            assert len(persisted) == bench_count * requests_per_bench
            assert all(item.status is ReservationStatus.EXPIRED for item in persisted)
            for bench_index in range(bench_count):
                bench_id = f"bench-{bench_index + 1:03d}"
                assert await stack.queues.list(bench_id=bench_id) == []
                promoted = await stack.queues.list(
                    bench_id=bench_id,
                    status=QueueEntryStatus.PROMOTED,
                )
                assert [entry.owner for entry in promoted] == [
                    f"user-{(slot * 10 + bench_index) % 100:03d}"
                    for slot in range(requests_per_bench)
                ]
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_simultaneous_immediate_submissions_create_one_reservation(tmp_path: Path) -> None:
    worker_count = 12
    databases = _open_databases(tmp_path / "submission-race.db", worker_count)
    barrier = Barrier(worker_count, timeout=30)
    services = [
        ReservationService(
            SQLiteTimedReservationRepository(database),
            SQLiteQueueRepository(database),
            SQLiteEventRepository(database),
            clock=FakeClock(NOW),
            availability=AlwaysOnline(),
        )
        for database in databases
    ]

    def submit(index: int) -> Reservation | QueueEntry | Exception:
        barrier.wait(timeout=5)
        try:
            return asyncio.run(
                services[index].create(
                    "shared-bench",
                    f"owner-{index:02d}",
                    duration_seconds=10 * 60,
                )
            )
        except Exception as exc:  # the losing domain error is part of the result
            return exc

    try:
        results = _run_concurrently(worker_count, submit)
        winners = [result for result in results if isinstance(result, Reservation)]
        failures = [result for result in results if isinstance(result, Exception)]

        assert len(winners) == 1
        assert len(failures) == worker_count - 1
        assert all(
            isinstance(error, (BenchAlreadyReservedError, ReservationTimeConflictError))
            for error in failures
        )
        stored = asyncio.run(
            SQLiteTimedReservationRepository(databases[0]).list(
                bench_id="shared-bench",
                limit=worker_count,
            )
        )
        assert stored == winners
    finally:
        for database in databases:
            database.close()


def test_concurrent_schedulers_promote_exactly_one_fifo_entry(tmp_path: Path) -> None:
    worker_count = 8
    databases = _open_databases(tmp_path / "promotion-race.db", worker_count)
    setup_queues = SQLiteQueueRepository(databases[0])
    setup_events = SQLiteEventRepository(databases[0])
    setup_clock = FakeClock(NOW)
    setup_service = ReservationService(
        SQLiteTimedReservationRepository(databases[0]),
        setup_queues,
        setup_events,
        clock=setup_clock,
        availability=AlwaysOnline(),
    )
    queued: list[QueueEntry] = []
    for index in range(20):
        queued.append(
            asyncio.run(
                setup_service.enqueue(
                    "fifo-bench",
                    f"owner-{index:02d}",
                    duration_seconds=60,
                )
            )
        )
        setup_clock.advance(microseconds=1)

    promotion_barrier = Barrier(worker_count, timeout=30)
    schedulers = []
    for database in databases:
        clock = FakeClock(NOW + timedelta(microseconds=20))
        schedulers.append(
            SchedulingService(
                SQLiteTimedReservationRepository(database),
                BarrierQueueRepository(database, promotion_barrier),
                SQLiteOperationLockRepository(database),
                SQLiteEventRepository(database),
                AlwaysOnline(),
                clock=clock,
            )
        )

    def promote(index: int) -> int | Exception:
        try:
            return asyncio.run(schedulers[index].promote_queues())
        except Exception as exc:
            return exc

    try:
        results = _run_concurrently(worker_count, promote)
        assert results.count(1) == 1
        assert results.count(0) == worker_count - 1
        assert not any(isinstance(result, Exception) for result in results)

        reservations = SQLiteTimedReservationRepository(databases[0])
        active = asyncio.run(reservations.get_active("fifo-bench"))
        assert active is not None
        assert active.owner == "owner-00"
        assert active.metadata["queue_entry_id"] == str(queued[0].id)

        first = asyncio.run(setup_queues.get(queued[0].id))
        assert first is not None and first.status is QueueEntryStatus.PROMOTED
        remaining = asyncio.run(setup_queues.list(bench_id="fifo-bench"))
        assert [entry.id for entry in remaining] == [entry.id for entry in queued[1:]]
        assert [entry.position for entry in remaining] == list(range(1, len(queued)))
        promoted_events = asyncio.run(
            setup_events.list(
                bench_id="fifo-bench",
                event_type="QUEUE_ENTRY_PROMOTED",
                limit=worker_count,
            )
        )
        assert len(promoted_events) == 1
    finally:
        for database in databases:
            database.close()


def test_operation_lock_contention_is_atomic_and_benches_are_independent(
    tmp_path: Path,
) -> None:
    worker_count = 12
    databases = _open_databases(tmp_path / "lock-race.db", worker_count)
    repositories = [SQLiteOperationLockRepository(database) for database in databases]
    shared_barrier = Barrier(worker_count, timeout=30)

    def acquire_shared(index: int) -> BenchOperationLock | Exception:
        shared_barrier.wait(timeout=5)
        try:
            return asyncio.run(
                repositories[index].acquire(
                    BenchOperationLock(
                        bench_id="shared-bench",
                        operation_id=UUID(int=index + 1),
                        acquired_at=NOW,
                    )
                )
            )
        except Exception as exc:
            return exc

    try:
        shared_results = _run_concurrently(worker_count, acquire_shared)
        acquired = [item for item in shared_results if isinstance(item, BenchOperationLock)]
        rejected = [item for item in shared_results if isinstance(item, Exception)]
        assert len(acquired) == 1
        assert len(rejected) == worker_count - 1
        assert all(isinstance(error, BenchOperationInProgressError) for error in rejected)

        winner = acquired[0]
        assert asyncio.run(repositories[0].acquire(winner)) == winner
        assert not asyncio.run(repositories[0].release("shared-bench", UUID(int=999)))
        assert asyncio.run(repositories[0].release("shared-bench", winner.operation_id))

        independent_barrier = Barrier(worker_count, timeout=30)

        def acquire_independent(index: int) -> BenchOperationLock | Exception:
            independent_barrier.wait(timeout=5)
            try:
                return asyncio.run(
                    repositories[index].acquire(
                        BenchOperationLock(
                            bench_id=f"bench-{index + 1:03d}",
                            operation_id=UUID(int=1000 + index),
                            acquired_at=NOW,
                        )
                    )
                )
            except Exception as exc:
                return exc

        independent = _run_concurrently(worker_count, acquire_independent)
        assert all(isinstance(item, BenchOperationLock) for item in independent)
        assert len(asyncio.run(repositories[0].list())) == worker_count
    finally:
        for database in databases:
            database.close()


def test_scheduled_protection_window_allows_only_the_exact_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _scheduler_stack(tmp_path / "protection.db")
        try:
            for bench_id in ("exact-boundary", "one-second-too-long"):
                scheduled = await stack.service.create(
                    bench_id,
                    "scheduled-owner",
                    starts_at=NOW + timedelta(minutes=20),
                    duration_seconds=10 * 60,
                )
                assert isinstance(scheduled, Reservation)

            exact = await stack.service.enqueue(
                "exact-boundary",
                "queued-owner",
                duration_seconds=15 * 60,
            )
            blocked = await stack.service.enqueue(
                "one-second-too-long",
                "queued-owner",
                duration_seconds=15 * 60 + 1,
            )

            assert await stack.scheduler.promote_queues() == 1
            active = await stack.reservations.get_active("exact-boundary")
            assert active is not None and active.owner == exact.owner
            assert await stack.reservations.get_active("one-second-too-long") is None
            blocked_after = await stack.queues.get(blocked.id)
            assert blocked_after is not None
            assert blocked_after.status is QueueEntryStatus.WAITING
            assert blocked_after.position == 1
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_simlab_runs_1000_overlapping_operations_across_100_benches() -> None:
    async def scenario() -> None:
        simlab = SimLab(bench_count=100, clock_mode="manual")
        await simlab.start()
        operation_count = 0
        try:
            bench_ids = [bench.id for bench in simlab.bench_snapshots()]
            assert len(bench_ids) == 100
            assert len(set(bench_ids)) == 100

            for _ in range(10):
                operations = [
                    asyncio.create_task(simlab.power_cycle(bench_id)) for bench_id in bench_ids
                ]
                operation_count += len(operations)
                await asyncio.sleep(0)
                assert all(not bench.powered for bench in simlab.bench_snapshots())
                simlab.tick(2)
                await asyncio.gather(*operations)
                assert all(bench.powered for bench in simlab.bench_snapshots())

            assert operation_count == 1000
        finally:
            await simlab.shutdown()

    asyncio.run(scenario())


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
