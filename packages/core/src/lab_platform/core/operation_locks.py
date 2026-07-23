from __future__ import annotations

from datetime import timedelta
from typing import Protocol
from uuid import UUID

from lab_platform.core.clock import Clock, UtcClock
from lab_platform.core.reservation_ports import (
    OperationLockRepository,
    ReservationEventRepository,
    TimedReservationRepository,
)
from lab_platform.models import BenchOperationLock, EventRecord


class ReservationAuthorizer(Protocol):
    async def require_active(self, bench_id: str, owner: str) -> UUID: ...


class OperationLockService:
    def __init__(
        self,
        locks: OperationLockRepository,
        reservations: TimedReservationRepository,
        authorizer: ReservationAuthorizer,
        events: ReservationEventRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._locks = locks
        self._reservations = reservations
        self._authorizer = authorizer
        self._events = events
        self._clock = clock or UtcClock()

    async def acquire(
        self,
        bench_id: str,
        operation_id: UUID,
        owner: str,
        *,
        lease_seconds: int | None = None,
    ) -> BenchOperationLock:
        await self._authorizer.require_active(bench_id, owner)
        return await self._acquire(
            bench_id,
            operation_id,
            owner,
            lease_seconds=lease_seconds,
            maintenance=False,
        )

    async def acquire_maintenance(
        self,
        bench_id: str,
        operation_id: UUID,
        owner: str,
        *,
        lease_seconds: int | None = None,
    ) -> BenchOperationLock:
        """Lock an unreserved offline bench so a probe can restore health safely."""

        return await self._acquire(
            bench_id,
            operation_id,
            owner,
            lease_seconds=lease_seconds,
            maintenance=True,
        )

    async def _acquire(
        self,
        bench_id: str,
        operation_id: UUID,
        owner: str,
        *,
        lease_seconds: int | None,
        maintenance: bool,
    ) -> BenchOperationLock:
        if lease_seconds is not None and lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock.now()
        lock = BenchOperationLock(
            bench_id=bench_id,
            operation_id=operation_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=lease_seconds)
            if lease_seconds is not None
            else None,
        )
        acquired = (
            await self._locks.acquire_for_maintenance(lock, now)
            if maintenance
            else await self._locks.acquire_for_active(lock, owner, now)
        )
        try:
            await self._events.create(
                EventRecord(
                    timestamp=now,
                    type="OPERATION_LOCK_ACQUIRED",
                    source="platform",
                    bench_id=bench_id,
                    actor=owner,
                    payload={"operation_id": str(operation_id)},
                    deduplication_key=f"operation:{operation_id}:lock-acquired",
                )
            )
        except Exception:
            await self._locks.release(bench_id, operation_id)
            raise
        return acquired

    async def release(self, bench_id: str, operation_id: UUID) -> bool:
        released = await self._locks.release(bench_id, operation_id)
        if not released:
            return False
        now = self._clock.now()
        await self._reservations.finalize_pending_expiry(bench_id, now)
        await self._events.create(
            EventRecord(
                timestamp=now,
                type="OPERATION_LOCK_RELEASED",
                source="platform",
                bench_id=bench_id,
                payload={"operation_id": str(operation_id)},
                deduplication_key=f"operation:{operation_id}:lock-released",
            )
        )
        return True
