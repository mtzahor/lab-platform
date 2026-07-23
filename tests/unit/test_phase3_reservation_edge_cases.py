from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core import (
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    BenchOperationInProgressError,
    FakeClock,
    OperationLockService,
    QueueDisabledError,
    QueueEntryNotFoundError,
    QueueOwnerMismatchError,
    RecoveryService,
    ReservationAlreadyExpiredError,
    ReservationExtensionConflictError,
    ReservationMaxDurationExceededError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
    ReservationTimeConflictError,
    SchedulingService,
)
from lab_platform.core.reservations import ReservationService
from lab_platform.models import (
    BenchOperationLock,
    EventRecord,
    Operation,
    OperationStatus,
    OperationType,
    QueueEntry,
    QueueEntryStatus,
    Reservation,
    ReservationSource,
    ReservationStatus,
    TimelineCategory,
)
from lab_platform.persistence import (
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationLockRepository,
    SQLiteOperationRepository,
    SQLiteQueueRepository,
    SQLiteRecoveryRepository,
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
    operations: SQLiteOperationRepository
    recoveries: SQLiteRecoveryRepository
    service: ReservationService
    scheduler: SchedulingService
    clock: FakeClock
    availability: Availability


def _stack(
    path: Path,
    *,
    queue_enabled: bool = True,
    default_duration_seconds: int = 30 * 60,
    maximum_duration_seconds: int = 4 * 60 * 60,
) -> Stack:
    database = SQLiteDatabase(path)
    database.initialize()
    reservations = SQLiteTimedReservationRepository(database)
    queues = SQLiteQueueRepository(database)
    locks = SQLiteOperationLockRepository(database)
    events = SQLiteEventRepository(database)
    timeline = SQLiteTimelineRepository(database)
    operations = SQLiteOperationRepository(database)
    recoveries = SQLiteRecoveryRepository(database)
    clock = FakeClock(NOW)
    availability = Availability()
    service = ReservationService(
        reservations,
        queues,
        events,
        clock=clock,
        availability=availability,
        operation_locks=locks,
        default_duration_seconds=default_duration_seconds,
        maximum_duration_seconds=maximum_duration_seconds,
        queue_enabled=queue_enabled,
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
    return Stack(
        database=database,
        reservations=reservations,
        queues=queues,
        locks=locks,
        events=events,
        timeline=timeline,
        operations=operations,
        recoveries=recoveries,
        service=service,
        scheduler=scheduler,
        clock=clock,
        availability=availability,
    )


def _reservation(
    bench_id: str,
    owner: str,
    status: ReservationStatus,
    *,
    starts_at: datetime | None = NOW,
    ends_at: datetime | None = NOW + timedelta(minutes=10),
    reservation_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> Reservation:
    return Reservation(
        id=reservation_id or uuid4(),
        bench_id=bench_id,
        owner=owner,
        created_at=NOW - timedelta(hours=1),
        requested_at=NOW - timedelta(hours=1),
        starts_at=starts_at,
        ends_at=ends_at,
        activated_at=starts_at if status is ReservationStatus.ACTIVE else None,
        expired_at=NOW
        if status in {ReservationStatus.EXPIRED, ReservationStatus.EXPIRED_PENDING_OPERATION}
        else None,
        released_at=NOW
        if status in {ReservationStatus.RELEASED, ReservationStatus.CANCELLED}
        else None,
        status=status,
        source=ReservationSource.API,
        idempotency_key=idempotency_key,
        release_pending=status is ReservationStatus.EXPIRED_PENDING_OPERATION,
    )


class ConflictOnCreateRepository(SQLiteTimedReservationRepository):
    async def create(self, reservation: Reservation) -> Reservation:
        raise ReservationTimeConflictError("simulated create race")


class ConflictOnUpdateRepository(SQLiteTimedReservationRepository):
    async def update(
        self,
        reservation: Reservation,
        *,
        expected_status: ReservationStatus | None = None,
        expected_ends_at: datetime | None = None,
        require_unlocked: bool = False,
    ) -> Reservation:
        del expected_status, expected_ends_at, require_unlocked
        raise ReservationTimeConflictError("simulated update race")


class ActivateRaceRepository(SQLiteTimedReservationRepository):
    async def activate_if_available(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None:
        return None


class ExpireRaceRepository(SQLiteTimedReservationRepository):
    async def expire_if_due(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None:
        return None


class PromoteRaceQueueRepository(SQLiteQueueRepository):
    async def promote(
        self,
        entry_id: UUID,
        reservation: Reservation,
        now: datetime,
        *,
        protection_window: timedelta | None = None,
        event_factory: Callable[[QueueEntry, Reservation], Iterable[EventRecord]] | None = None,
    ) -> tuple[QueueEntry, Reservation] | None:
        return None


class NeverSelectPolicy:
    async def select_next(self, entries: list[QueueEntry]) -> QueueEntry | None:
        return None


@dataclass
class RecordingAuthorizer:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        self.calls.append((bench_id, owner))
        return UUID(int=1)


class FailingEvents:
    async def create(self, event: EventRecord) -> EventRecord:
        raise RuntimeError("event persistence failed")


def test_service_constructor_and_duration_identity_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "validation.db")
        try:
            with pytest.raises(ValueError, match="default_duration_seconds"):
                ReservationService(
                    stack.reservations,
                    stack.queues,
                    stack.events,
                    default_duration_seconds=0,
                )
            with pytest.raises(ValueError, match="shorter than the default"):
                ReservationService(
                    stack.reservations,
                    stack.queues,
                    stack.events,
                    default_duration_seconds=60,
                    maximum_duration_seconds=59,
                )
            with pytest.raises(ValueError, match="duration_seconds must be positive"):
                await stack.service.create("zero-create", "alice", duration_seconds=0)
            with pytest.raises(ValueError, match="duration_seconds must be positive"):
                await stack.service.enqueue("zero-enqueue", "alice", duration_seconds=0)
            with pytest.raises(ValueError, match="duration_seconds must be positive"):
                await stack.service.create("negative", "alice", duration_seconds=-1)
            with pytest.raises(ReservationMaxDurationExceededError):
                await stack.service.create("too-long", "alice", duration_seconds=4 * 60 * 60 + 1)
            for bench_id, owner in ((" ", "alice"), ("bench", " ")):
                with pytest.raises(ValueError, match="non-empty"):
                    await stack.service.create(bench_id, owner, duration_seconds=60)
            with pytest.raises(ReservationTimeConflictError, match="past"):
                await stack.service.create(
                    "past", "alice", starts_at=NOW - timedelta(seconds=1), duration_seconds=60
                )
            with pytest.raises(ValueError, match="timezone-aware"):
                await stack.service.create(
                    "naive", "alice", starts_at=datetime(2026, 7, 20, 10), duration_seconds=60
                )

            with pytest.raises(ValueError, match="Scheduler durations"):
                SchedulingService(
                    stack.reservations,
                    stack.queues,
                    stack.locks,
                    stack.events,
                    stack.availability,
                    scheduled_protection_window_seconds=-1,
                )
            with pytest.raises(ValueError, match="Scheduler durations"):
                SchedulingService(
                    stack.reservations,
                    stack.queues,
                    stack.locks,
                    stack.events,
                    stack.availability,
                    expiry_grace_seconds=-1,
                )
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_offline_busy_future_and_create_race_queue_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "create-paths.db")
        try:
            stack.availability.offline.add("offline")
            with pytest.raises(BenchOfflineError):
                await stack.service.create("offline", "alice", duration_seconds=60)
            queued_offline = await stack.service.create(
                "offline",
                "alice",
                duration_seconds=60,
                queue_if_busy=True,
                idempotency_key="offline-request",
            )
            assert isinstance(queued_offline, QueueEntry)
            stack.availability.offline.remove("offline")
            assert (
                await stack.service.create(
                    "offline",
                    "alice",
                    duration_seconds=120,
                    queue_if_busy=True,
                    idempotency_key="offline-request",
                )
                == queued_offline
            )

            active = await stack.service.create("busy", "owner", duration_seconds=10 * 60)
            assert isinstance(active, Reservation)
            with pytest.raises(BenchAlreadyReservedError):
                await stack.service.create("busy", "other", duration_seconds=60)
            queued_busy = await stack.service.create(
                "busy", "other", duration_seconds=60, queue_if_busy=True
            )
            assert isinstance(queued_busy, QueueEntry)
            with pytest.raises(BenchAlreadyReservedError):
                await stack.service.reserve("busy", "other")

            future = await stack.service.schedule(
                "future",
                "alice",
                starts_at=NOW + timedelta(minutes=10),
                duration_seconds=10 * 60,
            )
            assert isinstance(future, Reservation)
            with pytest.raises(ReservationTimeConflictError) as conflict:
                await stack.service.create(
                    "future",
                    "bob",
                    starts_at=NOW + timedelta(minutes=15),
                    duration_seconds=60,
                    queue_if_busy=True,
                )
            assert conflict.value.details["conflicting_reservation_id"] == str(future.id)

            race_reservations = ConflictOnCreateRepository(stack.database)
            race_service = ReservationService(
                race_reservations,
                stack.queues,
                stack.events,
                clock=stack.clock,
                availability=stack.availability,
            )
            raced_to_queue = await race_service.create(
                "race-queued", "alice", duration_seconds=60, queue_if_busy=True
            )
            assert isinstance(raced_to_queue, QueueEntry)
            with pytest.raises(ReservationTimeConflictError, match="simulated create race"):
                await race_service.create("race-error", "alice", duration_seconds=60)
        finally:
            stack.database.close()

        disabled = _stack(tmp_path / "queue-disabled.db", queue_enabled=False)
        try:
            with pytest.raises(QueueDisabledError):
                await disabled.service.enqueue("bench", "alice")
        finally:
            disabled.database.close()

    asyncio.run(scenario())


def test_queue_cancellation_service_and_repository_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "queue-cancel.db")
        try:
            missing_id = uuid4()
            with pytest.raises(QueueEntryNotFoundError):
                await stack.service.cancel_queue_entry(missing_id, "alice")
            with pytest.raises(QueueEntryNotFoundError):
                await stack.queues.cancel(missing_id, "alice", NOW)

            entry = await stack.service.enqueue("bench", "alice", idempotency_key="cancel-me")
            assert (
                await stack.service.enqueue("bench", "alice", idempotency_key="cancel-me") == entry
            )
            with pytest.raises(QueueOwnerMismatchError):
                await stack.service.cancel_queue_entry(entry.id, "bob")
            with pytest.raises(QueueOwnerMismatchError):
                await stack.queues.cancel(entry.id, "bob", NOW)
            cancelled = await stack.service.cancel_queue_entry(entry.id, "alice")
            assert cancelled.status is QueueEntryStatus.CANCELLED
            assert await stack.service.cancel_queue_entry(entry.id, "alice") == cancelled
            assert await stack.queues.cancel(entry.id, "alice", NOW) == cancelled

            promotable = await stack.service.enqueue("promoted", "alice", duration_seconds=60)
            assert await stack.scheduler.promote_queues() == 1
            with pytest.raises(QueueEntryNotFoundError, match="no longer waiting"):
                await stack.queues.cancel(promotable.id, "alice", NOW)

            events = await stack.events.list(
                bench_id="bench", event_type="QUEUE_ENTRY_CANCELLED", limit=10
            )
            assert len(events) == 1
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_owner_guards_release_cancel_status_and_lock_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "owner-status.db")
        try:
            missing_id = uuid4()
            with pytest.raises(ReservationNotFoundError):
                await stack.service.get(missing_id)
            assert await stack.service.get_reservation("missing") is None
            with pytest.raises(ReservationNotActiveError):
                await stack.service.require_active("missing", "alice")
            with pytest.raises(BenchNotReservedError):
                await stack.service.require_owner("missing", "alice")

            active = await stack.service.create("active", "alice", duration_seconds=60)
            assert isinstance(active, Reservation)
            assert await stack.service.get_active("active") == active
            assert await stack.service.get_reservation("active") == active
            assert await stack.service.require_active("active", "alice") == active.id
            with pytest.raises(ReservationOwnerMismatchError):
                await stack.service.require_active("active", "bob")
            with pytest.raises(ReservationOwnerMismatchError):
                await stack.service.cancel(active.id, "bob")

            operation_id = uuid4()
            await stack.locks.acquire(
                BenchOperationLock(
                    bench_id="active",
                    operation_id=operation_id,
                    acquired_at=NOW,
                )
            )
            with pytest.raises(BenchOperationInProgressError):
                await stack.service.release(active.id, "alice")
            with pytest.raises(BenchOperationInProgressError):
                await stack.service.cancel(active.id, "alice")
            assert await stack.locks.release("active", operation_id)
            released = await stack.service.release("active", "alice")
            assert released is not None and released.status is ReservationStatus.RELEASED
            assert await stack.service.release(released.id, "alice") == released

            scheduled = await stack.service.create(
                "scheduled",
                "alice",
                starts_at=NOW + timedelta(minutes=5),
                duration_seconds=60,
            )
            assert isinstance(scheduled, Reservation)
            with pytest.raises(ReservationNotActiveError):
                await stack.service.release(scheduled.id, "alice")
            cancelled = await stack.service.cancel(scheduled.id, "alice")
            assert cancelled.status is ReservationStatus.CANCELLED
            assert await stack.service.cancel(scheduled.id, "alice") == cancelled

            expired = _reservation(
                "expired",
                "alice",
                ReservationStatus.EXPIRED,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            await stack.reservations.create(expired)
            with pytest.raises(ReservationNotActiveError):
                await stack.service.cancel(expired.id, "alice")

            ended_active = _reservation(
                "ended",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=1),
                ends_at=NOW,
            )
            await stack.reservations.create(ended_active)
            with pytest.raises(ReservationNotActiveError):
                await stack.service.require_active("ended", "alice")
            with pytest.raises(ReservationAlreadyExpiredError):
                await stack.service.extend(ended_active.id, "alice", 60)

            freshly_reserved = await stack.service.reserve("fresh-reserve", "alice")
            assert freshly_reserved.status is ReservationStatus.ACTIVE

            no_lock_service = ReservationService(
                stack.reservations,
                stack.queues,
                stack.events,
                clock=stack.clock,
            )
            no_lock_release = await no_lock_service.create(
                "no-lock-release", "alice", duration_seconds=60
            )
            assert isinstance(no_lock_release, Reservation)
            assert await no_lock_service.release(no_lock_release.id, "alice") is not None
            no_lock_cancel = await no_lock_service.create(
                "no-lock-cancel", "alice", duration_seconds=60
            )
            assert isinstance(no_lock_cancel, Reservation)
            assert (
                await no_lock_service.cancel(no_lock_cancel.id, "alice")
            ).status is ReservationStatus.CANCELLED

            unlocked_active = await stack.service.create(
                "unlocked-active", "alice", duration_seconds=60
            )
            assert isinstance(unlocked_active, Reservation)
            assert (
                await stack.service.cancel(unlocked_active.id, "alice")
            ).status is ReservationStatus.CANCELLED
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_immediate_reservation_cannot_activate_through_maintenance_lock(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "maintenance-reservation.db")
        try:
            maintenance = BenchOperationLock(
                bench_id="maintenance",
                operation_id=uuid4(),
                acquired_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
            await stack.locks.acquire_for_maintenance(maintenance, NOW)

            with pytest.raises(BenchOperationInProgressError):
                await stack.service.create("maintenance", "alice", duration_seconds=60)
            with pytest.raises(BenchOperationInProgressError):
                await stack.service.create(
                    "maintenance",
                    "alice",
                    starts_at=NOW + timedelta(seconds=10),
                    duration_seconds=60,
                )
            after_lease = await stack.service.create(
                "maintenance",
                "alice",
                starts_at=NOW + timedelta(seconds=31),
                duration_seconds=60,
            )
            assert isinstance(after_lease, Reservation)
            queued = await stack.service.create(
                "maintenance",
                "alice",
                duration_seconds=60,
                queue_if_busy=True,
            )
            assert isinstance(queued, QueueEntry)
            assert queued.status is QueueEntryStatus.WAITING

            await stack.locks.release("maintenance", maintenance.operation_id)
            await stack.service.cancel(after_lease.id, "alice")
            assert await stack.scheduler.promote_queues() == 1
            active = await stack.service.get_active("maintenance")
            assert active is not None and active.owner == "alice"

            scheduled_first = await stack.service.create(
                "schedule-first",
                "alice",
                starts_at=NOW + timedelta(seconds=10),
                duration_seconds=60,
            )
            assert isinstance(scheduled_first, Reservation)
            with pytest.raises(BenchOperationInProgressError):
                await stack.locks.acquire_for_maintenance(
                    BenchOperationLock(
                        bench_id="schedule-first",
                        operation_id=uuid4(),
                        acquired_at=NOW,
                        expires_at=NOW + timedelta(seconds=30),
                    ),
                    NOW,
                )
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_extension_success_maximum_conflict_inactive_and_update_race(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(
            tmp_path / "extend.db",
            default_duration_seconds=60,
            maximum_duration_seconds=10 * 60,
        )
        try:
            active = await stack.service.create("extend", "alice", duration_seconds=2 * 60)
            assert isinstance(active, Reservation)
            with pytest.raises(ValueError, match="positive"):
                await stack.service.extend(active.id, "alice", 0)
            extended = await stack.service.extend(active.id, "alice", 60)
            assert extended.ends_at == NOW + timedelta(minutes=3)
            with pytest.raises(ReservationMaxDurationExceededError):
                await stack.service.extend(active.id, "alice", 8 * 60)

            future = await stack.service.create(
                "conflict",
                "alice",
                duration_seconds=2 * 60,
            )
            assert isinstance(future, Reservation)
            scheduled = await stack.service.create(
                "conflict",
                "bob",
                starts_at=NOW + timedelta(minutes=3),
                duration_seconds=60,
            )
            assert isinstance(scheduled, Reservation)
            with pytest.raises(ReservationExtensionConflictError):
                await stack.service.extend(future.id, "alice", 2 * 60)
            with pytest.raises(ReservationNotActiveError):
                await stack.service.extend(scheduled.id, "bob", 1)

            race = await stack.service.create("update-race", "alice", duration_seconds=60)
            assert isinstance(race, Reservation)
            race_service = ReservationService(
                ConflictOnUpdateRepository(stack.database),
                stack.queues,
                stack.events,
                clock=stack.clock,
                default_duration_seconds=60,
                maximum_duration_seconds=10 * 60,
            )
            with pytest.raises(ReservationExtensionConflictError, match="simulated update race"):
                await race_service.extend(race.id, "alice", 1)
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_timed_repository_conflict_idempotency_update_and_list_filters(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "timed-repository.db")
        try:
            keyed = _reservation(
                "keyed",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW + timedelta(minutes=1),
                ends_at=NOW + timedelta(minutes=2),
                idempotency_key="same",
            )
            assert await stack.reservations.create(keyed) == keyed
            duplicate_key = keyed.model_copy(update={"id": uuid4(), "owner": "bob"})
            assert await stack.reservations.create(duplicate_key) == keyed
            with pytest.raises(ReservationTimeConflictError):
                await stack.reservations.create(
                    keyed.model_copy(update={"idempotency_key": "different"})
                )

            duplicate_id = _reservation("duplicate", "alice", ReservationStatus.RELEASED)
            await stack.reservations.create(duplicate_id)
            with pytest.raises(ReservationTimeConflictError):
                await stack.reservations.create(duplicate_id)
            assert await stack.reservations.get(uuid4()) is None

            missing_update = _reservation("missing", "alice", ReservationStatus.RELEASED)
            with pytest.raises(KeyError, match=str(missing_update.id)):
                await stack.reservations.update(missing_update)

            first = _reservation(
                "overlap",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW + timedelta(minutes=10),
                ends_at=NOW + timedelta(minutes=20),
            )
            second = _reservation(
                "overlap",
                "bob",
                ReservationStatus.SCHEDULED,
                starts_at=NOW + timedelta(minutes=20),
                ends_at=NOW + timedelta(minutes=30),
            )
            await stack.reservations.create(first)
            await stack.reservations.create(second)
            assert (
                await stack.reservations.find_conflict(
                    "overlap",
                    NOW + timedelta(minutes=12),
                    NOW + timedelta(minutes=13),
                )
                == first
            )
            assert (
                await stack.reservations.find_conflict(
                    "overlap",
                    NOW + timedelta(minutes=12),
                    NOW + timedelta(minutes=13),
                    exclude_id=first.id,
                )
                is None
            )
            with pytest.raises(ReservationTimeConflictError):
                await stack.reservations.update(
                    second.model_copy(
                        update={
                            "starts_at": NOW + timedelta(minutes=15),
                            "ends_at": NOW + timedelta(minutes=25),
                        }
                    )
                )

            other_owner = _reservation(
                "other",
                "bob",
                ReservationStatus.CANCELLED,
                starts_at=NOW + timedelta(minutes=40),
                ends_at=NOW + timedelta(minutes=41),
            )
            await stack.reservations.create(other_owner)
            assert await stack.service.list(bench_id="overlap", owner="alice") == [first]
            assert await stack.service.list(status=ReservationStatus.CANCELLED) == [other_owner]
            assert await stack.service.list(
                starts_after=NOW + timedelta(minutes=19),
                starts_before=NOW + timedelta(minutes=35),
            ) == [second]
            assert len(await stack.service.list(limit=2)) == 2
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_timed_repository_activation_expiry_and_finalize_status_branches(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "reservation-transitions.db")
        try:
            too_early = _reservation(
                "too-early",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW + timedelta(minutes=1),
                ends_at=NOW + timedelta(minutes=2),
            )
            ended = _reservation(
                "ended-scheduled",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            due = _reservation(
                "due",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(seconds=1),
                ends_at=NOW + timedelta(minutes=1),
            )
            for reservation in (too_early, ended, due):
                await stack.reservations.create(reservation)
            assert too_early in await stack.reservations.list_due(NOW + timedelta(minutes=1))
            assert ended in await stack.reservations.list_expired(NOW)
            assert await stack.reservations.activate_if_available(too_early.id, NOW) is None
            assert await stack.reservations.activate_if_available(ended.id, NOW) is None
            activated = await stack.reservations.activate_if_available(due.id, NOW)
            assert activated is not None and activated.status is ReservationStatus.ACTIVE
            assert await stack.reservations.activate_if_available(due.id, NOW) is None

            forced_race = _reservation(
                "forced-activation-race",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(seconds=1),
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(forced_race)
            with stack.database.transaction(immediate=True) as connection:
                connection.execute(
                    "CREATE TRIGGER force_activation_race "
                    "BEFORE UPDATE OF status ON reservations "
                    "WHEN NEW.id = '"
                    + str(forced_race.id)
                    + "' BEGIN SELECT RAISE(ABORT, 'forced_activation_race'); END"
                )
            assert await stack.reservations.activate_if_available(forced_race.id, NOW) is None
            with stack.database.transaction(immediate=True) as connection:
                connection.execute("DROP TRIGGER force_activation_race")

            stale_active = _reservation(
                "blocked",
                "owner",
                ReservationStatus.ACTIVE,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=1),
            )
            blocked = _reservation(
                "blocked",
                "scheduled",
                ReservationStatus.SCHEDULED,
                starts_at=NOW + timedelta(minutes=1),
                ends_at=NOW + timedelta(minutes=2),
            )
            await stack.reservations.create(stale_active)
            await stack.reservations.create(blocked)
            assert (
                await stack.reservations.activate_if_available(
                    blocked.id, NOW + timedelta(minutes=1)
                )
                is None
            )

            assert await stack.reservations.expire_if_due(uuid4(), NOW) is None
            no_end = _reservation(
                "no-end",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=1),
                ends_at=None,
            )
            future = _reservation(
                "future-end",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW + timedelta(minutes=1),
            )
            scheduled_expired = _reservation(
                "scheduled-expired",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            active_expired = _reservation(
                "active-expired",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            pending = _reservation(
                "pending",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            terminal = _reservation(
                "terminal",
                "alice",
                ReservationStatus.RELEASED,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            finalizable = _reservation(
                "finalizable",
                "alice",
                ReservationStatus.EXPIRED_PENDING_OPERATION,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            for reservation in (
                no_end,
                future,
                scheduled_expired,
                active_expired,
                pending,
                terminal,
                finalizable,
            ):
                await stack.reservations.create(reservation)
            assert await stack.reservations.expire_if_due(no_end.id, NOW) is None
            assert await stack.reservations.expire_if_due(future.id, NOW) is None
            scheduled_result = await stack.reservations.expire_if_due(scheduled_expired.id, NOW)
            assert (
                scheduled_result is not None
                and scheduled_result.status is ReservationStatus.EXPIRED
            )
            active_result = await stack.reservations.expire_if_due(active_expired.id, NOW)
            assert active_result is not None and active_result.status is ReservationStatus.EXPIRED
            pending_lock = BenchOperationLock(
                bench_id=pending.bench_id,
                operation_id=uuid4(),
                acquired_at=NOW,
            )
            await stack.locks.acquire(pending_lock)
            pending_result = await stack.reservations.expire_if_due(pending.id, NOW)
            assert pending_result is not None
            assert pending_result.status is ReservationStatus.EXPIRED_PENDING_OPERATION
            assert await stack.reservations.expire_if_due(pending.id, NOW) is None
            await stack.locks.release(pending.bench_id, pending_lock.operation_id)
            finalized_by_expiry = await stack.reservations.expire_if_due(pending.id, NOW)
            assert finalized_by_expiry is not None
            assert finalized_by_expiry.status is ReservationStatus.EXPIRED
            assert await stack.reservations.expire_if_due(terminal.id, NOW) is None

            assert await stack.reservations.finalize_pending_expiry("missing", NOW) is None
            finalized = await stack.reservations.finalize_pending_expiry("finalizable", NOW)
            assert finalized is not None and finalized.status is ReservationStatus.EXPIRED
            assert await stack.reservations.next_scheduled("missing", NOW) is None
            assert await stack.reservations.next_scheduled("too-early", NOW) == too_early
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_scheduler_transition_rolls_back_when_atomic_event_creation_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "atomic-scheduler-events.db")

        def fail_reservation_event(_: Reservation) -> EventRecord:
            raise RuntimeError("event creation failed")

        def fail_promotion_events(
            _: QueueEntry,
            __: Reservation,
        ) -> Iterable[EventRecord]:
            raise RuntimeError("event creation failed")

        try:
            due = _reservation(
                "activation-rollback",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(seconds=1),
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(due)
            with pytest.raises(RuntimeError, match="event creation failed"):
                await stack.reservations.activate_if_available(
                    due.id,
                    NOW,
                    event_factory=fail_reservation_event,
                )
            unchanged_due = await stack.reservations.get(due.id)
            assert unchanged_due is not None
            assert unchanged_due.status is ReservationStatus.SCHEDULED

            expired = _reservation(
                "expiry-rollback",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            await stack.reservations.create(expired)
            with pytest.raises(RuntimeError, match="event creation failed"):
                await stack.reservations.expire_if_due(
                    expired.id,
                    NOW,
                    event_factory=fail_reservation_event,
                )
            unchanged_expired = await stack.reservations.get(expired.id)
            assert unchanged_expired is not None
            assert unchanged_expired.status is ReservationStatus.ACTIVE

            queued = QueueEntry(
                bench_id="promotion-rollback",
                owner="alice",
                requested_duration_seconds=60,
                created_at=NOW,
            )
            await stack.queues.create(queued)
            promoted_reservation = _promotion_reservation(queued)
            with pytest.raises(RuntimeError, match="event creation failed"):
                await stack.queues.promote(
                    queued.id,
                    promoted_reservation,
                    NOW,
                    event_factory=fail_promotion_events,
                )
            unchanged_queue_entry = await stack.queues.get(queued.id)
            assert unchanged_queue_entry is not None
            assert unchanged_queue_entry.status is QueueEntryStatus.WAITING
            assert await stack.reservations.get(promoted_reservation.id) is None
        finally:
            stack.database.close()

    asyncio.run(scenario())


def _promotion_reservation(entry: QueueEntry, *, reservation_id: UUID | None = None) -> Reservation:
    return Reservation(
        id=reservation_id or uuid4(),
        bench_id=entry.bench_id,
        owner=entry.owner,
        created_at=entry.created_at,
        requested_at=entry.created_at,
        starts_at=NOW,
        ends_at=NOW + timedelta(minutes=1),
        activated_at=NOW,
        status=ReservationStatus.ACTIVE,
        source=ReservationSource.SYSTEM,
    )


def test_queue_repository_idempotency_listing_and_promotion_guards(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "queue-repository.db")
        try:
            keyed = QueueEntry(
                bench_id="keyed",
                owner="alice",
                requested_duration_seconds=60,
                created_at=NOW,
                idempotency_key="same",
            )
            created = await stack.queues.create(keyed)
            assert created.position == 1
            duplicate_key = keyed.model_copy(update={"id": uuid4(), "owner": "bob"})
            assert await stack.queues.create(duplicate_key) == created
            with pytest.raises(sqlite3.IntegrityError):
                await stack.queues.create(keyed.model_copy(update={"idempotency_key": "different"}))
            assert await stack.queues.get_by_idempotency_key("keyed", "missing") is None
            assert await stack.queues.get(uuid4()) is None
            with pytest.raises(sqlite3.IntegrityError):
                await stack.queues.create(keyed.model_copy(update={"idempotency_key": None}))

            cancelled = await stack.queues.cancel(keyed.id, "alice", NOW)
            assert cancelled.position is None
            with (
                pytest.raises(sqlite3.IntegrityError, match="queue_invalid_status_transition"),
                stack.database.transaction(immediate=True) as connection,
            ):
                connection.execute(
                    "UPDATE reservation_queue SET status = 'waiting' WHERE id = ?",
                    (str(cancelled.id),),
                )
            all_keyed = await stack.queues.list(bench_id="keyed", status=None)
            assert all_keyed == [cancelled]
            assert await stack.queues.get(cancelled.id) == cancelled

            missing = QueueEntry(
                bench_id="missing",
                owner="alice",
                requested_duration_seconds=60,
                created_at=NOW,
            )
            assert (
                await stack.queues.promote(missing.id, _promotion_reservation(missing), NOW) is None
            )

            active_queue = await stack.queues.create(
                QueueEntry(
                    bench_id="active",
                    owner="queued",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            await stack.reservations.create(
                _reservation("active", "owner", ReservationStatus.ACTIVE)
            )
            assert (
                await stack.queues.promote(
                    active_queue.id, _promotion_reservation(active_queue), NOW
                )
                is None
            )

            protected_queue = await stack.queues.create(
                QueueEntry(
                    bench_id="protected",
                    owner="queued",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            await stack.reservations.create(
                _reservation(
                    "protected",
                    "scheduled",
                    ReservationStatus.SCHEDULED,
                    starts_at=NOW + timedelta(minutes=2),
                    ends_at=NOW + timedelta(minutes=3),
                )
            )
            assert (
                await stack.queues.promote(
                    protected_queue.id,
                    _promotion_reservation(protected_queue),
                    NOW,
                    protection_window=timedelta(minutes=2),
                )
                is None
            )

            conflicting_id = uuid4()
            await stack.reservations.create(
                _reservation(
                    "existing-id",
                    "owner",
                    ReservationStatus.RELEASED,
                    reservation_id=conflicting_id,
                )
            )
            integrity_queue = await stack.queues.create(
                QueueEntry(
                    bench_id="integrity",
                    owner="queued",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            assert (
                await stack.queues.promote(
                    integrity_queue.id,
                    _promotion_reservation(integrity_queue, reservation_id=conflicting_id),
                    NOW,
                )
                is None
            )

            cursor_race_queue = await stack.queues.create(
                QueueEntry(
                    bench_id="cursor-race",
                    owner="queued",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            cursor_race_reservation = _promotion_reservation(cursor_race_queue)
            with stack.database.transaction(immediate=True) as connection:
                connection.execute(
                    "CREATE TRIGGER force_queue_promotion_race "
                    "AFTER INSERT ON reservations "
                    "WHEN NEW.id = '"
                    + str(cursor_race_reservation.id)
                    + "' BEGIN UPDATE reservation_queue SET status = 'cancelled' "
                    "WHERE id = '" + str(cursor_race_queue.id) + "'; END"
                )
            assert (
                await stack.queues.promote(cursor_race_queue.id, cursor_race_reservation, NOW)
                is None
            )
            with stack.database.transaction(immediate=True) as connection:
                connection.execute("DROP TRIGGER force_queue_promotion_race")
            assert (await stack.queues.get(cursor_race_queue.id)).status is QueueEntryStatus.WAITING  # type: ignore[union-attr]
            assert await stack.reservations.get(cursor_race_reservation.id) is None

            success_queue = await stack.queues.create(
                QueueEntry(
                    bench_id="success",
                    owner="queued",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            promoted = await stack.queues.promote(
                success_queue.id, _promotion_reservation(success_queue), NOW
            )
            assert promoted is not None
            assert promoted[0].status is QueueEntryStatus.PROMOTED
            assert set(await stack.queues.list_waiting_benches()) == {
                "active",
                "cursor-race",
                "integrity",
                "protected",
            }
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_operation_lock_service_finalize_and_repository_stale_rules(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "operation-locks.db")
        authorizer = RecordingAuthorizer()
        service = OperationLockService(
            stack.locks,
            stack.reservations,
            authorizer,
            stack.events,
            clock=stack.clock,
        )
        try:
            with pytest.raises(ValueError, match="lease_seconds"):
                await service.acquire("invalid", uuid4(), "alice", lease_seconds=0)
            active = _reservation(
                "pending",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(active)
            operation_id = uuid4()
            acquired = await service.acquire("pending", operation_id, "alice", lease_seconds=60)
            assert acquired.expires_at == NOW + timedelta(seconds=60)
            assert (
                await service.acquire("pending", operation_id, "alice", lease_seconds=60)
                == acquired
            )
            with pytest.raises(BenchOperationInProgressError):
                await service.acquire("pending", uuid4(), "alice")
            assert not await service.release("pending", uuid4())

            await stack.reservations.create(
                _reservation(
                    "event-failure",
                    "alice",
                    ReservationStatus.ACTIVE,
                    starts_at=NOW - timedelta(minutes=1),
                    ends_at=NOW + timedelta(minutes=1),
                )
            )
            failing_service = OperationLockService(
                stack.locks,
                stack.reservations,
                authorizer,
                FailingEvents(),
                clock=stack.clock,
            )
            with pytest.raises(RuntimeError, match="event persistence failed"):
                await failing_service.acquire("event-failure", uuid4(), "alice")
            assert await stack.locks.get("event-failure") is None

            pending = active.model_copy(
                update={
                    "status": ReservationStatus.EXPIRED_PENDING_OPERATION,
                    "ends_at": NOW - timedelta(minutes=1),
                    "expired_at": NOW,
                    "release_pending": True,
                }
            )
            await stack.reservations.update(
                pending,
                expected_status=ReservationStatus.ACTIVE,
            )
            assert await service.release("pending", operation_id)
            finalized = await stack.reservations.get(pending.id)
            assert finalized is not None and finalized.status is ReservationStatus.EXPIRED

            await stack.reservations.create(
                _reservation(
                    "no-lease",
                    "alice",
                    ReservationStatus.ACTIVE,
                    starts_at=NOW - timedelta(minutes=1),
                    ends_at=NOW + timedelta(minutes=1),
                )
            )
            no_lease = await service.acquire("no-lease", uuid4(), "alice")
            assert no_lease.expires_at is None
            live_id = no_lease.operation_id
            expired_id = uuid4()
            nonlive_id = uuid4()
            await stack.locks.acquire(
                BenchOperationLock(
                    bench_id="expired-lease",
                    operation_id=expired_id,
                    acquired_at=NOW - timedelta(minutes=2),
                    expires_at=NOW,
                )
            )
            await stack.locks.acquire(
                BenchOperationLock(
                    bench_id="non-live",
                    operation_id=nonlive_id,
                    acquired_at=NOW,
                )
            )
            assert await stack.locks.recover_stale({live_id, expired_id}, NOW) == 2
            assert await stack.locks.get("no-lease") == no_lease
            assert await stack.locks.recover_stale({live_id}, NOW) == 0
            assert len(await stack.locks.list()) == 1
            assert authorizer.calls[0] == ("invalid", "alice")

            acquired_events = await stack.events.list(
                event_type="OPERATION_LOCK_ACQUIRED", limit=10
            )
            released_events = await stack.events.list(
                event_type="OPERATION_LOCK_RELEASED", limit=10
            )
            assert len(acquired_events) == 2
            assert len(released_events) == 1
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_scheduler_skip_race_policy_and_process_branches(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "scheduler-branches.db")
        try:
            ended = _reservation(
                "ended",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            offline = _reservation(
                "offline",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(ended)
            await stack.reservations.create(offline)
            stack.availability.offline.add("offline")
            assert await stack.scheduler.process_due_reservations() == 0

            active = _reservation(
                "active-block",
                "owner",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            blocked = _reservation(
                "active-block",
                "scheduled",
                ReservationStatus.SCHEDULED,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(active)
            await stack.reservations.create(blocked)
            assert await stack.scheduler.process_due_reservations() == 0

            race_candidate = _reservation(
                "activation-race",
                "alice",
                ReservationStatus.SCHEDULED,
                starts_at=NOW,
                ends_at=NOW + timedelta(minutes=1),
            )
            await stack.reservations.create(race_candidate)
            activation_race_scheduler = SchedulingService(
                ActivateRaceRepository(stack.database),
                stack.queues,
                stack.locks,
                stack.events,
                stack.availability,
                clock=stack.clock,
            )
            assert await activation_race_scheduler.process_due_reservations() == 0

            expiry_race = _reservation(
                "expiry-race",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            await stack.reservations.create(expiry_race)
            expiry_race_scheduler = SchedulingService(
                ExpireRaceRepository(stack.database),
                stack.queues,
                stack.locks,
                stack.events,
                stack.availability,
                clock=stack.clock,
            )
            assert await expiry_race_scheduler.expire_reservations() == 0

            policy_entry = await stack.queues.create(
                QueueEntry(
                    bench_id="policy-none",
                    owner="alice",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            never_scheduler = SchedulingService(
                stack.reservations,
                stack.queues,
                stack.locks,
                stack.events,
                stack.availability,
                clock=stack.clock,
                queue_policy=NeverSelectPolicy(),
            )
            assert await never_scheduler.promote_queues() == 0
            assert (await stack.queues.get(policy_entry.id)).status is QueueEntryStatus.WAITING  # type: ignore[union-attr]

            promote_race_queue = PromoteRaceQueueRepository(stack.database)
            promote_race_entry = await promote_race_queue.create(
                QueueEntry(
                    bench_id="promotion-race",
                    owner="alice",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            promote_race_scheduler = SchedulingService(
                stack.reservations,
                promote_race_queue,
                stack.locks,
                stack.events,
                stack.availability,
                clock=stack.clock,
            )
            assert await promote_race_scheduler.promote_queues() == 0
            assert (
                await promote_race_queue.get(promote_race_entry.id)
            ).status is QueueEntryStatus.WAITING  # type: ignore[union-attr]

            empty = _stack(tmp_path / "scheduler-empty.db")
            try:
                assert await empty.scheduler.process() == (0, 0, 0)
            finally:
                empty.database.close()
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_timeline_categories_operation_ids_summaries_and_filters(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "timeline.db")
        try:
            operation = Operation.pending("timeline", OperationType.RESET, "alice", now=NOW)
            await stack.operations.create(operation)
            payload_operation_id = uuid4()
            event_specs: list[tuple[str, dict[str, object], UUID | None]] = [
                ("RESERVATION_CREATED", {}, None),
                ("OPERATION_STARTED", {}, operation.id),
                ("FLASH_FINISHED", {"operation_id": str(payload_operation_id)}, None),
                ("BENCH_HEALTH_CHANGED", {}, None),
                ("BENCH_OFFLINE", {}, None),
                ("WORKFLOW_STARTED", {}, None),
                ("USB_ATTACHED", {}, None),
                ("RECOVERY_COMPLETED", {"summary": "Recovery complete"}, None),
            ]
            for index, (event_type, payload, operation_id) in enumerate(event_specs):
                await stack.events.create(
                    EventRecord(
                        timestamp=NOW + timedelta(seconds=index),
                        type=event_type,
                        source="test",
                        bench_id="timeline",
                        operation_id=operation_id,
                        actor="alice",
                        payload=payload,
                    )
                )

            timeline = await stack.timeline.list_timeline("timeline")
            by_type = {entry.event_type: entry for entry in timeline}
            assert by_type["RESERVATION_CREATED"].category is TimelineCategory.RESERVATION
            assert by_type["OPERATION_STARTED"].category is TimelineCategory.OPERATION
            assert by_type["FLASH_FINISHED"].category is TimelineCategory.OPERATION
            assert by_type["BENCH_HEALTH_CHANGED"].category is TimelineCategory.HEALTH
            assert by_type["BENCH_OFFLINE"].category is TimelineCategory.HEALTH
            assert by_type["WORKFLOW_STARTED"].category is TimelineCategory.WORKFLOW
            assert by_type["USB_ATTACHED"].category is TimelineCategory.HARDWARE
            assert by_type["RECOVERY_COMPLETED"].category is TimelineCategory.SYSTEM
            assert by_type["OPERATION_STARTED"].operation_id == operation.id
            assert by_type["FLASH_FINISHED"].operation_id == payload_operation_id
            assert by_type["RECOVERY_COMPLETED"].summary == "Recovery complete"
            assert by_type["USB_ATTACHED"].summary == "Usb Attached"

            assert [
                entry.event_type
                for entry in await stack.timeline.list_timeline(
                    "timeline", category=TimelineCategory.HEALTH
                )
            ] == ["BENCH_OFFLINE", "BENCH_HEALTH_CHANGED"]
            window = await stack.timeline.list_timeline(
                "timeline",
                after=NOW + timedelta(seconds=1),
                before=NOW + timedelta(seconds=6),
                limit=2,
            )
            assert [entry.event_type for entry in window] == [
                "WORKFLOW_STARTED",
                "BENCH_OFFLINE",
            ]
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_recovery_reconciles_operations_locks_reservations_and_queues(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "recovery.db")
        try:
            operation = Operation.pending(
                "expired", OperationType.POWER_CYCLE, "alice", now=NOW - timedelta(minutes=2)
            )
            await stack.operations.create(operation)
            expired = _reservation(
                "expired",
                "alice",
                ReservationStatus.ACTIVE,
                starts_at=NOW - timedelta(minutes=2),
                ends_at=NOW - timedelta(minutes=1),
            )
            due = _reservation(
                "due",
                "bob",
                ReservationStatus.SCHEDULED,
                starts_at=NOW - timedelta(seconds=1),
                ends_at=NOW + timedelta(minutes=2),
            )
            await stack.reservations.create(expired)
            await stack.reservations.create(due)
            lock = BenchOperationLock(
                bench_id="expired",
                operation_id=operation.id,
                acquired_at=NOW - timedelta(minutes=2),
            )
            await stack.locks.acquire(lock)
            await stack.queues.create(
                QueueEntry(
                    bench_id="queued",
                    owner="carol",
                    requested_duration_seconds=60,
                    created_at=NOW - timedelta(minutes=1),
                )
            )

            recovery = RecoveryService(
                stack.operations,
                stack.locks,
                stack.scheduler,
                stack.events,
                records=stack.recoveries,
                clock=stack.clock,
            )
            report = await recovery.recover()
            assert report.interrupted_operations == 1
            assert report.stale_locks_removed == 1
            assert report.reservations_expired == 1
            assert report.reservations_activated == 1
            assert report.queue_entries_promoted == 1
            assert await stack.recoveries.list() == [report]
            assert await stack.locks.get("expired") is None
            recovered_operation = await stack.operations.get(operation.id)
            assert recovered_operation is not None
            assert recovered_operation.status is OperationStatus.FAILED
            assert (await stack.reservations.get(expired.id)).status is ReservationStatus.EXPIRED  # type: ignore[union-attr]
            assert (await stack.reservations.get(due.id)).status is ReservationStatus.ACTIVE  # type: ignore[union-attr]
            assert (await stack.reservations.get_active("queued")).owner == "carol"  # type: ignore[union-attr]

            assert len(await stack.events.list(event_type="RECOVERY_STARTED")) == 1
            assert len(await stack.events.list(event_type="STALE_LOCK_RECOVERED")) == 1
            assert len(await stack.events.list(event_type="RECOVERY_COMPLETED")) == 1

            repeated = await recovery.recover()
            assert repeated.interrupted_operations == 0
            assert repeated.stale_locks_removed == 0
            assert repeated.reservations_expired == 0
            assert repeated.reservations_activated == 0
            assert repeated.queue_entries_promoted == 0
            assert len(await stack.events.list(event_type="RECOVERY_STARTED")) == 1
            assert await stack.recoveries.list() == [report, repeated]

            still_pending = Operation.pending("untouched", OperationType.RESET, "dana", now=NOW)
            await stack.operations.create(still_pending)
            supplied = await recovery.recover(interrupted_operations=7)
            assert supplied.interrupted_operations == 7
            assert await stack.recoveries.list() == [report, repeated, supplied]
            assert (await stack.operations.get(still_pending.id)).status is OperationStatus.PENDING  # type: ignore[union-attr]
        finally:
            stack.database.close()

    asyncio.run(scenario())


def test_recovery_leaves_queue_waiting_when_automatic_assignment_is_disabled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "manual-recovery.db")
        try:
            queued = await stack.queues.create(
                QueueEntry(
                    bench_id="manual",
                    owner="alice",
                    requested_duration_seconds=60,
                    created_at=NOW,
                )
            )
            recovery = RecoveryService(
                stack.operations,
                stack.locks,
                stack.scheduler,
                stack.events,
                clock=stack.clock,
                automatic_assignment=False,
            )

            report = await recovery.recover(interrupted_operations=0)

            assert report.queue_entries_promoted == 0
            assert await stack.reservations.get_active("manual") is None
            persisted = await stack.queues.get(queued.id)
            assert persisted is not None
            assert persisted.status is QueueEntryStatus.WAITING
            assert persisted.position == 1
        finally:
            stack.database.close()

    asyncio.run(scenario())
