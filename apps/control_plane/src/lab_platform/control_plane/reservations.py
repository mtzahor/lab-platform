from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lab_platform.agent_protocol import MessageType, ReservationLeaseAppliedPayload
from lab_platform.agent_protocol.commands import (
    ReservationActivatedPayload,
    ReservationReleasedPayload,
)
from lab_platform.control_plane.gateway import AgentConnectionHub
from lab_platform.control_plane_core.errors import AgentOfflineError
from lab_platform.control_plane_core.reconciliation import ReconciliationResult
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    LeaseApplicationReceipt,
    ReservationLeaseState,
)
from lab_platform.core.errors import ReservationNotFoundError
from lab_platform.models import ReconciliationReport, ReservationLease


class HubReservationLeaseSynchronizer:
    """Send lease fences and await an Agent's semantic apply confirmation."""

    def __init__(
        self,
        hub: AgentConnectionHub,
        *,
        confirmation_timeout_seconds: float = 15.0,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("Lease confirmation timeout must be positive")
        self._hub = hub
        self._timeout = confirmation_timeout_seconds
        self._pending: dict[tuple[UUID, int], asyncio.Future[LeaseApplicationReceipt]] = {}
        self._lock = asyncio.Lock()

    async def is_available(self, agent_id: UUID) -> bool:
        """Return whether a scheduled lease can be offered without guessing from DB presence."""

        return await self._hub.is_connected(agent_id)

    async def apply_lease(self, lease: ReservationLease) -> LeaseApplicationReceipt:
        if not await self._hub.is_connected(lease.agent_id):
            raise AgentOfflineError(
                "Agent is not connected to apply its reservation lease.",
                agent_id=str(lease.agent_id),
                reservation_id=str(lease.reservation_id),
            )
        key = (lease.reservation_id, lease.lease_version)
        leader = False
        async with self._lock:
            future = self._pending.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._pending[key] = future
                leader = True
        if leader:
            try:
                await self._hub.send(
                    lease.agent_id,
                    MessageType.RESERVATION_ACTIVATED,
                    ReservationActivatedPayload(lease=lease),
                    correlation_id=lease.reservation_id,
                )
            except BaseException:
                async with self._lock:
                    if self._pending.get(key) is future:
                        self._pending.pop(key, None)
                raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except TimeoutError:
            # ``shield`` deliberately prevents one waiter from cancelling a shared
            # confirmation.  Once the bounded confirmation window elapses, however,
            # the request is no longer eligible to keep a stale future in the map.
            async with self._lock:
                if self._pending.get(key) is future:
                    self._pending.pop(key, None)
                    future.cancel()
            raise
        finally:
            if future.done():
                async with self._lock:
                    if self._pending.get(key) is future:
                        self._pending.pop(key, None)

    async def confirm(self, payload: ReservationLeaseAppliedPayload) -> bool:
        key = (payload.reservation_id, payload.lease_version)
        async with self._lock:
            future = self._pending.get(key)
        if future is None or future.done():
            return False
        future.set_result(
            LeaseApplicationReceipt(
                reservation_id=payload.reservation_id,
                agent_id=payload.agent_id,
                bench_id=payload.bench_id,
                lease_version=payload.lease_version,
                confirmed_at=payload.confirmed_at,
            )
        )
        return True

    async def release_lease(self, lease: ReservationLease) -> None:
        if not await self._hub.is_connected(lease.agent_id):
            return
        released_at = lease.released_at or datetime.now(UTC)
        await self._hub.send(
            lease.agent_id,
            MessageType.RESERVATION_RELEASED,
            ReservationReleasedPayload(
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                released_at=released_at,
            ),
            correlation_id=lease.reservation_id,
        )


class ReconciliationHandler(Protocol):
    async def reconcile(
        self,
        report_id: UUID,
        report: ReconciliationReport,
    ) -> ReconciliationResult: ...


class CoordinatedReconciliationHandler:
    """Apply journal/inventory reconciliation, then restore or fence local leases."""

    def __init__(
        self,
        base: ReconciliationHandler,
        reservations: CentralReservationLeaseService,
        synchronizer: HubReservationLeaseSynchronizer,
        *,
        on_reconciled: Callable[[ReconciliationResult], Awaitable[None]] | None = None,
    ) -> None:
        self._base = base
        self._reservations = reservations
        self._synchronizer = synchronizer
        self._on_reconciled = on_reconciled

    async def reconcile(
        self,
        report_id: UUID,
        report: ReconciliationReport,
    ) -> ReconciliationResult:
        result = await self._base.reconcile(report_id, report)
        stale = result.stale_local_leases
        for lease in report.local_reservation_leases:
            key = (lease.reservation_id, lease.lease_version)
            if key in stale:
                await self._synchronizer.release_lease(
                    lease.model_copy(update={"released_at": datetime.now(UTC)})
                )
                continue
            try:
                current = await self._reservations.get(lease.reservation_id)
            except ReservationNotFoundError:
                await self._synchronizer.release_lease(
                    lease.model_copy(update={"released_at": datetime.now(UTC)})
                )
                continue
            if current.state is ReservationLeaseState.UNKNOWN and current.lease == lease:
                await self._reservations.reconcile_after_reconnect(
                    lease,
                    idempotency_key=(
                        f"reconcile:{report_id}:{lease.reservation_id}:{lease.lease_version}"
                    ),
                )
        if self._on_reconciled is not None:
            await self._on_reconciled(result)
        return result
