from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from lab_platform.agent_protocol import MessageType, ReservationLeaseAppliedPayload
from lab_platform.control_plane.reservations import (
    CoordinatedReconciliationHandler,
    HubReservationLeaseSynchronizer,
)
from lab_platform.control_plane_core.errors import AgentOfflineError
from lab_platform.control_plane_core.reconciliation import ReconciliationResult
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import ReservationNotFoundError
from lab_platform.models import ReconciliationReport, ReservationLease

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
AGENT_ID = UUID(int=101)
RESERVATION_ID = UUID(int=102)


class RecordingHub:
    def __init__(self, *, connected: bool = True, fail_send: bool = False) -> None:
        self.connected = connected
        self.fail_send = fail_send
        self.sent: list[tuple[UUID, MessageType, object, UUID | None]] = []

    async def is_connected(self, _agent_id: UUID) -> bool:
        return self.connected

    async def send(
        self,
        agent_id: UUID,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None = None,
    ) -> object:
        if self.fail_send:
            raise ConnectionError("socket failed")
        self.sent.append((agent_id, message_type, payload, correlation_id))
        return payload


def _lease(*, reservation_id: UUID = RESERVATION_ID, version: int = 3) -> ReservationLease:
    return ReservationLease(
        reservation_id=reservation_id,
        agent_id=AGENT_ID,
        bench_id="home-lab/bench-01",
        owner="ci/build-42",
        valid_from=NOW,
        valid_until=NOW + timedelta(minutes=5),
        lease_version=version,
    )


def _result(*, stale: frozenset[tuple[UUID, int]] = frozenset()) -> ReconciliationResult:
    return ReconciliationResult(
        report_id=UUID(int=200),
        report_digest="a" * 64,
        agent_id=AGENT_ID,
        boot_id=UUID(int=201),
        restarted=False,
        reconciled_command_ids=frozenset(),
        reconciled_operation_ids=frozenset(),
        interrupted_operation_ids=frozenset(),
        expired_reservation_ids=frozenset(),
        stale_local_leases=stale,
        inventory_reconciled=True,
    )


def _report(*leases: ReservationLease) -> ReconciliationReport:
    return ReconciliationReport(
        agent_id=AGENT_ID,
        boot_id=UUID(int=201),
        generated_at=NOW,
        active_commands=(),
        recent_commands=(),
        local_reservation_leases=leases,
        bench_snapshots=(),
        buffered_event_count=0,
    )


def test_lease_synchronizer_coalesces_waiters_and_confirms_once() -> None:
    async def scenario() -> None:
        hub = RecordingHub()
        synchronizer = HubReservationLeaseSynchronizer(
            cast(Any, hub),
            confirmation_timeout_seconds=1,
        )
        lease = _lease()

        first = asyncio.create_task(synchronizer.apply_lease(lease))
        second = asyncio.create_task(synchronizer.apply_lease(lease))
        await asyncio.sleep(0)
        assert [message[1] for message in hub.sent] == [MessageType.RESERVATION_ACTIVATED]

        confirmed = await synchronizer.confirm(
            ReservationLeaseAppliedPayload(
                reservation_id=lease.reservation_id,
                agent_id=lease.agent_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                confirmed_at=NOW + timedelta(seconds=1),
            )
        )
        assert confirmed is True
        assert await first == await second
        assert (
            await synchronizer.confirm(
                ReservationLeaseAppliedPayload(
                    reservation_id=lease.reservation_id,
                    agent_id=lease.agent_id,
                    bench_id=lease.bench_id,
                    lease_version=lease.lease_version,
                    confirmed_at=NOW + timedelta(seconds=2),
                )
            )
            is False
        )

    asyncio.run(scenario())


def test_lease_synchronizer_offline_timeout_send_failure_and_release_paths() -> None:
    async def scenario() -> None:
        lease = _lease()
        offline = RecordingHub(connected=False)
        synchronizer = HubReservationLeaseSynchronizer(cast(Any, offline))
        with pytest.raises(AgentOfflineError):
            await synchronizer.apply_lease(lease)
        await synchronizer.release_lease(lease)
        assert offline.sent == []

        failing = RecordingHub(fail_send=True)
        failing_sync = HubReservationLeaseSynchronizer(cast(Any, failing))
        with pytest.raises(ConnectionError, match="socket failed"):
            await failing_sync.apply_lease(lease)

        timeout_hub = RecordingHub()
        timeout_sync = HubReservationLeaseSynchronizer(
            cast(Any, timeout_hub),
            confirmation_timeout_seconds=0.001,
        )
        with pytest.raises(TimeoutError):
            await timeout_sync.apply_lease(lease)
        assert (
            await timeout_sync.confirm(
                ReservationLeaseAppliedPayload(
                    reservation_id=lease.reservation_id,
                    agent_id=lease.agent_id,
                    bench_id=lease.bench_id,
                    lease_version=lease.lease_version,
                    confirmed_at=NOW,
                )
            )
            is False
        )

        connected = RecordingHub()
        release_sync = HubReservationLeaseSynchronizer(cast(Any, connected))
        await release_sync.release_lease(lease)
        assert connected.sent[0][1] is MessageType.RESERVATION_RELEASED
        assert connected.sent[0][3] == lease.reservation_id

    with pytest.raises(ValueError, match="positive"):
        HubReservationLeaseSynchronizer(cast(Any, RecordingHub()), confirmation_timeout_seconds=0)
    asyncio.run(scenario())


class BaseReconciler:
    def __init__(self, result: ReconciliationResult) -> None:
        self.result = result

    async def reconcile(
        self,
        _report_id: UUID,
        _report: ReconciliationReport,
    ) -> ReconciliationResult:
        return self.result


class ReservationLookup:
    def __init__(self, records: dict[UUID, object]) -> None:
        self.records = records
        self.reconciled: list[tuple[ReservationLease, str]] = []

    async def get(self, reservation_id: UUID) -> object:
        value = self.records.get(reservation_id)
        if value is None:
            raise ReservationNotFoundError("missing")
        return value

    async def reconcile_after_reconnect(
        self,
        lease: ReservationLease,
        *,
        idempotency_key: str,
    ) -> object:
        self.reconciled.append((lease, idempotency_key))
        return lease


class RecordingLeaseSynchronizer:
    def __init__(self) -> None:
        self.released: list[ReservationLease] = []

    async def release_lease(self, lease: ReservationLease) -> None:
        self.released.append(lease)


def test_coordinated_reconciliation_restores_matching_unknown_and_fences_stale() -> None:
    async def scenario() -> None:
        stale = _lease(reservation_id=UUID(int=301), version=1)
        missing = _lease(reservation_id=UUID(int=302), version=2)
        matching = _lease(reservation_id=UUID(int=303), version=3)
        already_active = _lease(reservation_id=UUID(int=304), version=4)
        result = _result(stale=frozenset({(stale.reservation_id, stale.lease_version)}))
        reservations = ReservationLookup(
            {
                matching.reservation_id: cast(
                    CoordinatedReservationLease,
                    type(
                        "Record",
                        (),
                        {"state": ReservationLeaseState.UNKNOWN, "lease": matching},
                    )(),
                ),
                already_active.reservation_id: cast(
                    CoordinatedReservationLease,
                    type(
                        "Record",
                        (),
                        {"state": ReservationLeaseState.ACTIVE, "lease": already_active},
                    )(),
                ),
            }
        )
        synchronizer = RecordingLeaseSynchronizer()
        callbacks: list[ReconciliationResult] = []

        async def on_reconciled(value: ReconciliationResult) -> None:
            callbacks.append(value)

        handler = CoordinatedReconciliationHandler(
            BaseReconciler(result),
            cast(CentralReservationLeaseService, reservations),
            cast(HubReservationLeaseSynchronizer, synchronizer),
            on_reconciled=on_reconciled,
        )
        report_id = UUID(int=305)
        assert (
            await handler.reconcile(
                report_id,
                _report(stale, missing, matching, already_active),
            )
            is result
        )

        assert {lease.reservation_id for lease in synchronizer.released} == {
            stale.reservation_id,
            missing.reservation_id,
        }
        assert all(lease.released_at is not None for lease in synchronizer.released)
        assert reservations.reconciled == [
            (
                matching,
                f"reconcile:{report_id}:{matching.reservation_id}:{matching.lease_version}",
            )
        ]
        assert callbacks == [result]

    asyncio.run(scenario())
