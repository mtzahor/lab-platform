from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import PlatformError
from lab_platform.models import (
    GlobalBenchRecord,
    PrincipalType,
    QueueEntry,
    QueueEntryStatus,
    ReservationOwner,
    ReservationSource,
)

_LOGGER = logging.getLogger("lab-platform.control-plane.reservation-queue")
_TERMINAL_LEASE_STATES = frozenset(
    {
        ReservationLeaseState.RELEASED,
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.REVOKED,
    }
)


class ReservationQueueRepository(Protocol):
    async def list_waiting(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 10_000,
    ) -> list[QueueEntry]: ...

    async def get(
        self,
        entry_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None: ...

    async def mark_promoted(
        self,
        entry_id: UUID,
        now: datetime,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None: ...


class QueueBenchDirectory(Protocol):
    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None: ...


class QueueReservationLeaseService(Protocol):
    async def grant(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        owner_principal: ReservationOwner | None = None,
        idempotency_key: str,
        reservation_duration_seconds: int | None = None,
        source: ReservationSource = ReservationSource.API,
        metadata: dict[str, str] | None = None,
        allow_internal_authorisation: bool = False,
        scheduled_protection_window_seconds: int = 0,
    ) -> CoordinatedReservationLease: ...

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        owner_principal: ReservationOwner | None = None,
        expected_lease_version: int,
        idempotency_key: str,
        allow_internal_authorisation: bool = False,
    ) -> CoordinatedReservationLease: ...


class CentralReservationQueueService:
    """Promote durable FIFO queue rows into authoritative coordinated leases.

    Queue persistence deliberately stays separate from lease persistence. A stable
    grant idempotency key closes the crash window: if the process stops after the
    central lease commits but before the queue row is marked, the next pass replays
    the same lease and then completes the queue transition.
    """

    def __init__(
        self,
        queue: ReservationQueueRepository,
        reservations: QueueReservationLeaseService,
        benches: QueueBenchDirectory,
        *,
        scheduled_protection_window_seconds: int = 300,
    ) -> None:
        if scheduled_protection_window_seconds < 0:
            raise ValueError("Scheduled protection window cannot be negative")
        self._queue = queue
        self._reservations = reservations
        self._benches = benches
        self._scheduled_protection_window_seconds = scheduled_protection_window_seconds
        self._promotion_lock = asyncio.Lock()

    async def promote_waiting(self, *, limit: int = 100) -> int:
        if limit <= 0:
            raise ValueError("Queue promotion limit must be positive")
        async with self._promotion_lock:
            waiting = await self._queue.list_waiting(limit=limit)
            attempted_benches: set[tuple[UUID, str]] = set()
            promoted = 0
            for entry in waiting:
                bench_key = (entry.organisation_id, entry.bench_id)
                # Never skip a tenant's FIFO head merely because a later row for
                # that same bench might be easier to grant.
                if bench_key in attempted_benches:
                    continue
                attempted_benches.add(bench_key)
                try:
                    promoted += int(await self._promote(entry))
                except PlatformError as exc:
                    _LOGGER.debug(
                        "Queue entry %s remains waiting after %s",
                        entry.id,
                        exc.code,
                    )
                except Exception:
                    # One corrupt/stale row must not prevent unrelated benches
                    # from being promoted during the same maintenance pass.
                    _LOGGER.exception("Queue entry %s could not be promoted", entry.id)
            return promoted

    async def _promote(self, entry: QueueEntry) -> bool:
        if entry.status is not QueueEntryStatus.WAITING:
            return False
        owner_principal = _queue_owner(entry)
        bench = await self._benches.get(
            entry.bench_id,
            organisation_id=entry.organisation_id,
        )
        if bench is None or bench.organisation_id != entry.organisation_id:
            return False

        grant_key = _grant_key(entry.id)
        terminal_grants: set[tuple[UUID, int]] = set()
        while True:
            granted = await self._reservations.grant(
                agent_id=bench.agent_id,
                bench_id=bench.id,
                owner=entry.owner,
                owner_principal=owner_principal,
                idempotency_key=grant_key,
                reservation_duration_seconds=entry.requested_duration_seconds,
                source=ReservationSource.API,
                metadata={
                    "reservation_queue_entry_id": str(entry.id),
                    "reservation_origin": "queue",
                },
                allow_internal_authorisation=True,
                scheduled_protection_window_seconds=(self._scheduled_protection_window_seconds),
            )
            if granted.state not in _TERMINAL_LEASE_STATES:
                break
            terminal_identity = (
                granted.reservation.id,
                granted.lease.lease_version,
            )
            if terminal_identity in terminal_grants:
                _LOGGER.error(
                    "Queue entry %s replayed the same terminal grant for a new retry key",
                    entry.id,
                )
                return False
            terminal_grants.add(terminal_identity)
            # A prior unconfirmed attempt can reconcile to EXPIRED/REVOKED while
            # the queue row correctly remains WAITING. Deriving the next stable
            # key from that terminal lease creates one retry without opening a
            # crash window or storing an in-memory attempt counter.
            grant_key = _retry_grant_key(entry.id, *terminal_identity)
        # ``grant`` can durably fence the bench in UNKNOWN when the Agent does
        # not confirm application of the lease.  That is intentionally not a
        # successful queue promotion: the queue row remains WAITING until
        # reconciliation confirms the same idempotent grant as ACTIVE.
        if granted.state is not ReservationLeaseState.ACTIVE:
            return False
        marked = await self._queue.mark_promoted(
            entry.id,
            datetime.now(UTC),
            organisation_id=entry.organisation_id,
        )
        if marked is not None:
            return True

        current = await self._queue.get(
            entry.id,
            organisation_id=entry.organisation_id,
        )
        if current is not None and current.status is QueueEntryStatus.PROMOTED:
            # Another maintenance worker replayed the same idempotent grant and
            # won the queue CAS; the durable outcome is already correct.
            return False
        if current is not None and current.status is QueueEntryStatus.WAITING:
            # A transient write anomaly can be retried safely with the stable key.
            return False

        # Cancellation/expiry may win the narrow grant-to-mark race. Compensate
        # the already committed central grant so a cancelled queue row never
        # silently leaves ownership behind.
        await self._release_cancelled_race(entry, owner_principal, granted)
        return False

    async def _release_cancelled_race(
        self,
        entry: QueueEntry,
        owner_principal: ReservationOwner | None,
        granted: CoordinatedReservationLease,
    ) -> None:
        if granted.state in _TERMINAL_LEASE_STATES:
            return
        await self._reservations.release(
            granted.reservation.id,
            owner=entry.owner,
            owner_principal=owner_principal,
            expected_lease_version=granted.lease.lease_version,
            idempotency_key=f"reservation-queue:{entry.id}:cancel-race-release",
            allow_internal_authorisation=True,
        )


def _queue_owner(entry: QueueEntry) -> ReservationOwner | None:
    if entry.owner_principal_id is None or entry.owner_principal_type is None:
        return None
    return ReservationOwner(
        principal_id=entry.owner_principal_id,
        principal_type=PrincipalType(entry.owner_principal_type),
        display_name=entry.owner,
    )


def _grant_key(entry_id: UUID) -> str:
    return f"reservation-queue:{entry_id}:grant"


def _retry_grant_key(entry_id: UUID, reservation_id: UUID, lease_version: int) -> str:
    return f"reservation-queue:{entry_id}:retry-after:{reservation_id}:{lease_version}"


__all__ = ["CentralReservationQueueService"]
