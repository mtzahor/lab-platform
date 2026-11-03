from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import MessageType, ReservationLeaseAppliedPayload
from lab_platform.control_plane.reservation_queue import CentralReservationQueueService
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
from lab_platform.core.errors import (
    BenchAlreadyReservedError,
    QueueOwnerMismatchError,
    ReservationNotFoundError,
)
from lab_platform.models import (
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    QueueEntry,
    QueueEntryStatus,
    ReconciliationReport,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
)
from lab_platform.persistence import SCHEMA_VERSION, SQLiteDatabase, SQLiteQueueRepository
from pydantic import ValidationError

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)


ORG_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


USER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


AGENT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


class FakeQueue:
    def __init__(self, entries: list[QueueEntry]) -> None:
        self.entries = {entry.id: entry for entry in entries}
        self.marked: list[UUID] = []
        self.cancel_during_mark = False

    async def list_waiting(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 10_000,
    ) -> list[QueueEntry]:
        return [
            entry
            for entry in self.entries.values()
            if entry.status is QueueEntryStatus.WAITING
            and (organisation_id is None or entry.organisation_id == organisation_id)
        ][:limit]

    async def get(
        self,
        entry_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None:
        entry = self.entries.get(entry_id)
        if entry is not None and (
            organisation_id is None or entry.organisation_id == organisation_id
        ):
            return entry
        return None

    async def mark_promoted(
        self,
        entry_id: UUID,
        now: datetime,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None:
        entry = await self.get(entry_id, organisation_id=organisation_id)
        if entry is None or entry.status is not QueueEntryStatus.WAITING:
            return None
        if self.cancel_during_mark:
            self.entries[entry_id] = entry.model_copy(
                update={"status": QueueEntryStatus.CANCELLED, "cancelled_at": now}
            )
            return None
        promoted = entry.model_copy(
            update={"status": QueueEntryStatus.PROMOTED, "promoted_at": now}
        )
        self.entries[entry_id] = promoted
        self.marked.append(entry_id)
        return promoted


class FakeBenches:
    def __init__(self, benches: list[GlobalBenchRecord]) -> None:
        self.benches = {(bench.organisation_id, bench.id): bench for bench in benches}

    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None:
        if organisation_id is None:
            return next(
                (bench for (_, key), bench in self.benches.items() if key == bench_id), None
            )
        return self.benches.get((organisation_id, bench_id))


class FakeReservations:
    def __init__(self) -> None:
        self.grant_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []
        self.fail_benches: set[str] = set()
        self.grant_state = ReservationLeaseState.ACTIVE
        self.grant_states: list[ReservationLeaseState] = []

    async def grant(self, **kwargs: Any) -> CoordinatedReservationLease:
        self.grant_calls.append(kwargs)
        if kwargs["bench_id"] in self.fail_benches:
            raise BenchAlreadyReservedError("busy")
        owner_principal = kwargs["owner_principal"]
        state = self.grant_states.pop(0) if self.grant_states else self.grant_state
        reservation_status = {
            ReservationLeaseState.ACTIVATING: ReservationStatus.SCHEDULED,
            ReservationLeaseState.ACTIVE: ReservationStatus.ACTIVE,
            ReservationLeaseState.RENEWING: ReservationStatus.ACTIVE,
            ReservationLeaseState.UNKNOWN: ReservationStatus.SCHEDULED,
            ReservationLeaseState.RELEASED: ReservationStatus.RELEASED,
            ReservationLeaseState.EXPIRED: ReservationStatus.EXPIRED,
            ReservationLeaseState.REVOKED: ReservationStatus.CANCELLED,
        }[state]
        terminal = state in {
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }
        reservation_id = uuid4()
        reservation = Reservation(
            id=reservation_id,
            organisation_id=ORG_ID,
            bench_id=kwargs["bench_id"],
            owner=kwargs["owner"],
            owner_principal_id=(
                owner_principal.principal_id if owner_principal is not None else None
            ),
            owner_principal_type=(
                owner_principal.principal_type.value if owner_principal is not None else None
            ),
            created_at=NOW,
            starts_at=NOW,
            ends_at=NOW + timedelta(seconds=kwargs["reservation_duration_seconds"]),
            activated_at=NOW if reservation_status is ReservationStatus.ACTIVE else None,
            released_at=NOW if reservation_status is ReservationStatus.RELEASED else None,
            expired_at=NOW if reservation_status is ReservationStatus.EXPIRED else None,
            status=reservation_status,
            source=kwargs["source"],
            metadata=kwargs["metadata"],
            idempotency_key=kwargs["idempotency_key"],
        )
        return CoordinatedReservationLease(
            reservation=reservation,
            lease=ReservationLease(
                reservation_id=reservation_id,
                agent_id=kwargs["agent_id"],
                bench_id=kwargs["bench_id"],
                owner=kwargs["owner"],
                valid_from=NOW,
                valid_until=NOW + timedelta(hours=1),
                lease_version=1,
                released_at=NOW if terminal else None,
            ),
            state=state,
            revision=2,
            unknown_since=NOW if state is ReservationLeaseState.UNKNOWN else None,
            reconciliation_deadline=(
                NOW + timedelta(minutes=5) if state is ReservationLeaseState.UNKNOWN else None
            ),
        )

    async def release(self, reservation_id: UUID, **kwargs: Any) -> CoordinatedReservationLease:
        self.release_calls.append({"reservation_id": reservation_id, **kwargs})
        grant = self.grant_calls[-1]
        record = await self.grant(**grant)
        return CoordinatedReservationLease(
            reservation=record.reservation.model_copy(
                update={"status": ReservationStatus.RELEASED, "released_at": NOW}
            ),
            lease=record.lease.model_copy(update={"released_at": NOW}),
            state=ReservationLeaseState.RELEASED,
            revision=record.revision + 1,
        )


def _entry(bench_id: str, *, principal: bool = True) -> QueueEntry:
    return QueueEntry(
        organisation_id=ORG_ID,
        bench_id=bench_id,
        owner="Alice",
        owner_principal_id=USER_ID if principal else None,
        owner_principal_type="USER" if principal else None,
        requested_duration_seconds=1_800,
        created_at=NOW,
        idempotency_key=f"queue-{bench_id}-{uuid4()}",
    )


def _bench(bench_id: str) -> GlobalBenchRecord:
    slug, local_id = bench_id.split("/", 1)
    return GlobalBenchRecord(
        id=bench_id,
        organisation_id=ORG_ID,
        agent_id=AGENT_ID,
        agent_slug=slug,
        local_bench_id=local_id,
        name=local_id,
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        created_at=NOW,
        updated_at=NOW,
    )


def test_queue_promotion_is_fifo_per_bench_and_uses_durable_internal_identity() -> None:
    first = _entry("agent/bench-a")
    second = _entry("agent/bench-a")
    other = _entry("agent/bench-b")
    queue = FakeQueue([first, second, other])
    reservations = FakeReservations()
    service = CentralReservationQueueService(
        queue,
        reservations,
        FakeBenches([_bench(first.bench_id), _bench(other.bench_id)]),
    )

    promoted = asyncio.run(service.promote_waiting())

    assert promoted == 2
    assert queue.marked == [first.id, other.id]
    assert [call["bench_id"] for call in reservations.grant_calls] == [
        first.bench_id,
        other.bench_id,
    ]
    call = reservations.grant_calls[0]
    assert call["allow_internal_authorisation"] is True
    assert call["owner_principal"].principal_id == USER_ID
    assert call["idempotency_key"] == f"reservation-queue:{first.id}:grant"
    assert call["source"] is ReservationSource.API


def test_failed_grant_does_not_consume_queue_rows_and_legacy_owner_is_preserved() -> None:
    busy = _entry("agent/bench-a")
    legacy = _entry("agent/bench-b", principal=False)
    queue = FakeQueue([busy, legacy])
    reservations = FakeReservations()
    reservations.fail_benches.add(busy.bench_id)
    service = CentralReservationQueueService(
        queue,
        reservations,
        FakeBenches([_bench(busy.bench_id), _bench(legacy.bench_id)]),
    )

    assert asyncio.run(service.promote_waiting()) == 1
    assert queue.marked == [legacy.id]
    assert queue.entries[busy.id].status is QueueEntryStatus.WAITING
    assert queue.entries[legacy.id].status is QueueEntryStatus.PROMOTED
    assert [call["bench_id"] for call in reservations.grant_calls] == [
        busy.bench_id,
        legacy.bench_id,
    ]
    assert reservations.grant_calls[-1]["owner"] == legacy.owner
    assert reservations.grant_calls[-1]["owner_principal"] is None


def test_unconfirmed_grant_keeps_fifo_head_waiting() -> None:
    entry = _entry("agent/bench-a")
    queue = FakeQueue([entry])
    reservations = FakeReservations()
    reservations.grant_state = ReservationLeaseState.UNKNOWN
    service = CentralReservationQueueService(
        queue,
        reservations,
        FakeBenches([_bench(entry.bench_id)]),
    )

    assert asyncio.run(service.promote_waiting()) == 0
    assert queue.marked == []
    assert queue.entries[entry.id].status is QueueEntryStatus.WAITING


def test_terminal_unconfirmed_attempt_retries_with_a_stable_derived_key() -> None:
    entry = _entry("agent/bench-a")
    queue = FakeQueue([entry])
    reservations = FakeReservations()
    reservations.grant_states = [
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.ACTIVE,
    ]
    service = CentralReservationQueueService(
        queue,
        reservations,
        FakeBenches([_bench(entry.bench_id)]),
    )

    assert asyncio.run(service.promote_waiting()) == 1
    assert queue.marked == [entry.id]
    assert len(reservations.grant_calls) == 2
    first, retry = reservations.grant_calls
    assert first["idempotency_key"] == f"reservation-queue:{entry.id}:grant"
    assert retry["idempotency_key"].startswith(f"reservation-queue:{entry.id}:retry-after:")


def test_queue_principal_identity_is_an_atomic_pair() -> None:
    payload = _entry("agent/bench-a").model_dump()
    payload["owner_principal_type"] = None
    with pytest.raises(ValidationError, match="must be provided together"):
        QueueEntry.model_validate(payload)


def test_cancel_winning_grant_to_mark_race_releases_the_central_lease() -> None:
    entry = _entry("agent/bench-a", principal=False)
    queue = FakeQueue([entry])
    queue.cancel_during_mark = True
    reservations = FakeReservations()
    service = CentralReservationQueueService(
        queue,
        reservations,
        FakeBenches([_bench(entry.bench_id)]),
    )

    assert asyncio.run(service.promote_waiting()) == 0
    assert queue.entries[entry.id].status is QueueEntryStatus.CANCELLED
    assert len(reservations.release_calls) == 1
    release = reservations.release_calls[0]
    assert release["allow_internal_authorisation"] is True
    assert release["owner_principal"] is None
    assert release["idempotency_key"] == (f"reservation-queue:{entry.id}:cancel-race-release")


def test_queue_repository_persists_principal_and_selects_one_fifo_head_per_bench(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "queue.db")
    database.initialize()
    repository = SQLiteQueueRepository(database)
    first = _entry("agent/bench-a")
    second = _entry("agent/bench-a")
    other = _entry("agent/bench-b")
    first = first.model_copy(update={"description": "  first request  "})

    async def exercise() -> None:
        await repository.create(first)
        await repository.create(second)
        await repository.create(other)

        stored = await repository.get(first.id, organisation_id=ORG_ID)
        assert stored is not None
        assert stored.owner_principal_id == USER_ID
        assert stored.owner_principal_type == "USER"
        assert stored.description == "first request"

        heads = await repository.list_waiting(organisation_id=ORG_ID)
        assert [entry.id for entry in heads] == [first.id, other.id]
        assert [entry.position for entry in heads] == [1, 1]

        await repository.mark_promoted(first.id, NOW, organisation_id=ORG_ID)
        next_heads = await repository.list_waiting(organisation_id=ORG_ID)
        assert [entry.id for entry in next_heads] == [second.id, other.id]

        legacy = _entry("agent/bench-c", principal=False)
        await repository.create(legacy)
        with pytest.raises(QueueOwnerMismatchError, match="owner"):
            await repository.cancel(
                legacy.id,
                "Mallory",
                NOW,
                organisation_id=ORG_ID,
            )
        cancelled = await repository.cancel(
            legacy.id,
            legacy.owner,
            NOW,
            organisation_id=ORG_ID,
        )
        assert cancelled.status is QueueEntryStatus.CANCELLED

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_schema_v11_upgrades_existing_queue_rows_without_changing_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue-upgrade.db"
    database = SQLiteDatabase(path)
    database.initialize()
    database.close()
    entry_id = uuid4()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO reservation_queue "
            "(id, organisation_id, bench_id, owner, requested_duration_seconds, status, "
            "created_at, idempotency_key) VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?)",
            (
                str(entry_id),
                str(ORG_ID),
                "agent/bench-a",
                "legacy-owner",
                1_800,
                NOW.isoformat(),
                "legacy-key",
            ),
        )
        connection.execute("DROP INDEX reservation_queue_waiting_order")
        connection.execute("DROP INDEX reservation_queue_order")
        connection.execute("ALTER TABLE reservation_queue DROP COLUMN queue_order")
        connection.execute("ALTER TABLE reservation_queue DROP COLUMN description")
        connection.execute("ALTER TABLE reservation_queue DROP COLUMN owner_principal_type")
        connection.execute("ALTER TABLE reservation_queue DROP COLUMN owner_principal_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")

    upgraded = SQLiteDatabase(path)
    upgraded.initialize()
    try:
        with upgraded.transaction() as connection:
            assert SCHEMA_VERSION == 12
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(reservation_queue)")
            }
            assert {
                "owner_principal_id",
                "owner_principal_type",
                "description",
                "queue_order",
            } <= columns
            row = connection.execute(
                "SELECT owner, owner_principal_id, owner_principal_type, description "
                "FROM reservation_queue WHERE id = ?",
                (str(entry_id),),
            ).fetchone()
            assert row is not None
            assert tuple(row) == ("legacy-owner", None, None, None)
    finally:
        upgraded.close()


NOW_CONTROL_PLANE_RESERVATION_SYNC = datetime(2026, 7, 29, 12, tzinfo=UTC)


AGENT_ID_CONTROL_PLANE_RESERVATION_SYNC = UUID(int=101)


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
        agent_id=AGENT_ID_CONTROL_PLANE_RESERVATION_SYNC,
        bench_id="home-lab/bench-01",
        owner="ci/build-42",
        valid_from=NOW_CONTROL_PLANE_RESERVATION_SYNC,
        valid_until=NOW_CONTROL_PLANE_RESERVATION_SYNC + timedelta(minutes=5),
        lease_version=version,
    )


def _result(*, stale: frozenset[tuple[UUID, int]] = frozenset()) -> ReconciliationResult:
    return ReconciliationResult(
        report_id=UUID(int=200),
        report_digest="a" * 64,
        agent_id=AGENT_ID_CONTROL_PLANE_RESERVATION_SYNC,
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
        agent_id=AGENT_ID_CONTROL_PLANE_RESERVATION_SYNC,
        boot_id=UUID(int=201),
        generated_at=NOW_CONTROL_PLANE_RESERVATION_SYNC,
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
                confirmed_at=NOW_CONTROL_PLANE_RESERVATION_SYNC + timedelta(seconds=1),
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
                    confirmed_at=NOW_CONTROL_PLANE_RESERVATION_SYNC + timedelta(seconds=2),
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
                    confirmed_at=NOW_CONTROL_PLANE_RESERVATION_SYNC,
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
