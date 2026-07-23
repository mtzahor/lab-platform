from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from lab_platform.models import (
    BenchOperationLock,
    BenchTimelineEntry,
    EventRecord,
    QueueEntry,
    QueueEntryStatus,
    Reservation,
    ReservationStatus,
    TimelineCategory,
)


class BenchAvailability(Protocol):
    def is_online(self, bench_id: str) -> bool: ...


class TimedReservationRepository(Protocol):
    async def create(self, reservation: Reservation) -> Reservation: ...

    async def get(self, reservation_id: UUID) -> Reservation | None: ...

    async def get_active(self, bench_id: str) -> Reservation | None: ...

    async def get_by_idempotency_key(
        self, bench_id: str, idempotency_key: str
    ) -> Reservation | None: ...

    async def update(
        self,
        reservation: Reservation,
        *,
        expected_status: ReservationStatus | None = None,
        expected_ends_at: datetime | None = None,
        require_unlocked: bool = False,
    ) -> Reservation: ...

    async def list(
        self,
        *,
        bench_id: str | None = None,
        owner: str | None = None,
        status: ReservationStatus | None = None,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        limit: int = 50,
    ) -> builtins.list[Reservation]: ...

    async def find_conflict(
        self,
        bench_id: str,
        starts_at: datetime,
        ends_at: datetime,
        *,
        exclude_id: UUID | None = None,
    ) -> Reservation | None: ...

    async def list_due(self, now: datetime) -> builtins.list[Reservation]: ...

    async def list_expired(self, now: datetime) -> builtins.list[Reservation]: ...

    async def activate_if_available(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None: ...

    async def expire_if_due(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None: ...

    async def next_scheduled(self, bench_id: str, after: datetime) -> Reservation | None: ...

    async def finalize_pending_expiry(self, bench_id: str, now: datetime) -> Reservation | None: ...


class QueueRepository(Protocol):
    async def create(self, entry: QueueEntry) -> QueueEntry: ...

    async def get(self, entry_id: UUID) -> QueueEntry | None: ...

    async def get_by_idempotency_key(
        self, bench_id: str, idempotency_key: str
    ) -> QueueEntry | None: ...

    async def list(
        self,
        *,
        bench_id: str,
        status: QueueEntryStatus | None = QueueEntryStatus.WAITING,
    ) -> builtins.list[QueueEntry]: ...

    async def list_waiting_benches(self) -> builtins.list[str]: ...

    async def cancel(self, entry_id: UUID, owner: str, now: datetime) -> QueueEntry: ...

    async def promote(
        self,
        entry_id: UUID,
        reservation: Reservation,
        now: datetime,
        *,
        protection_window: timedelta | None = None,
        event_factory: Callable[[QueueEntry, Reservation], Iterable[EventRecord]] | None = None,
    ) -> tuple[QueueEntry, Reservation] | None: ...


class OperationLockRepository(Protocol):
    async def acquire(self, lock: BenchOperationLock) -> BenchOperationLock: ...

    async def acquire_for_active(
        self,
        lock: BenchOperationLock,
        owner: str,
        now: datetime,
    ) -> BenchOperationLock: ...

    async def acquire_for_maintenance(
        self,
        lock: BenchOperationLock,
        now: datetime,
    ) -> BenchOperationLock: ...

    async def get(self, bench_id: str) -> BenchOperationLock | None: ...

    async def release(self, bench_id: str, operation_id: UUID) -> bool: ...

    async def list(self) -> builtins.list[BenchOperationLock]: ...

    async def recover_stale(self, live_operation_ids: set[UUID], now: datetime) -> int: ...


class TimelineRepository(Protocol):
    async def list_timeline(
        self,
        bench_id: str,
        *,
        category: TimelineCategory | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> builtins.list[BenchTimelineEntry]: ...


class ReservationEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...
