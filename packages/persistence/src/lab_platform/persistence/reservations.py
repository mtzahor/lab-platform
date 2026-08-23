from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from uuid import UUID

from lab_platform.core.errors import (
    BenchOperationInProgressError,
    QueueEntryNotFoundError,
    QueueOwnerMismatchError,
    ReservationNotActiveError,
    ReservationOwnerMismatchError,
    ReservationTimeConflictError,
)
from lab_platform.models import (
    BenchOperationLock,
    BenchTimelineEntry,
    EventRecord,
    QueueEntry,
    QueueEntryStatus,
    RecoveryReport,
    Reservation,
    ReservationSource,
    ReservationStatus,
    TimelineCategory,
)
from lab_platform.models.domain import LEGACY_ORGANISATION_ID
from lab_platform.persistence.database import SQLiteDatabase, insert_event


class SQLiteTimedReservationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, reservation: Reservation) -> Reservation:
        try:
            with self._database.transaction(immediate=True) as connection:
                if reservation.status is ReservationStatus.ACTIVE:
                    _require_no_operation_lock(connection, reservation.bench_id)
                elif reservation.status is ReservationStatus.SCHEDULED:
                    _require_no_maintenance_lock_overlap(connection, reservation)
                if reservation.status in {
                    ReservationStatus.SCHEDULED,
                    ReservationStatus.ACTIVE,
                    ReservationStatus.EXPIRED_PENDING_OPERATION,
                }:
                    _require_no_reservation_overlap(connection, reservation)
                connection.execute(
                    "INSERT INTO reservations "
                    "(id, bench_id, owner, owner_principal_id, owner_principal_type, "
                    "organisation_id, created_at, released_at, status, requested_at, "
                    "starts_at, ends_at, activated_at, expired_at, source, metadata, "
                    "idempotency_key, release_pending) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _reservation_values(reservation),
                )
        except sqlite3.IntegrityError as exc:
            if reservation.idempotency_key:
                existing = await self.get_by_idempotency_key(
                    reservation.bench_id,
                    reservation.idempotency_key,
                    organisation_id=reservation.organisation_id,
                )
                if existing is not None:
                    return existing
            raise _reservation_integrity_error(reservation, exc) from exc
        return reservation

    async def get(
        self,
        reservation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> Reservation | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(reservation_id), str(organisation_id))
            if organisation_id is not None
            else (str(reservation_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM reservations WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def get_active(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> Reservation | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (bench_id, str(organisation_id)) if organisation_id is not None else (bench_id,)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE bench_id = ? "
                "AND status IN ('active', 'expired_pending_operation') "
                f"{scope} "  # noqa: S608
                "ORDER BY starts_at LIMIT 1",
                values,
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def get_by_idempotency_key(
        self,
        bench_id: str,
        idempotency_key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> Reservation | None:
        scope_id = organisation_id or LEGACY_ORGANISATION_ID
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE bench_id = ? AND idempotency_key = ? "
                "AND organisation_id = ?",
                (bench_id, idempotency_key, str(scope_id)),
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def update(
        self,
        reservation: Reservation,
        *,
        expected_status: ReservationStatus | None = None,
        expected_ends_at: datetime | None = None,
        require_unlocked: bool = False,
    ) -> Reservation:
        try:
            with self._database.transaction(immediate=True) as connection:
                if require_unlocked:
                    _require_no_operation_lock(connection, reservation.bench_id)
                if reservation.status is ReservationStatus.SCHEDULED:
                    _require_no_maintenance_lock_overlap(connection, reservation)
                if reservation.status in {
                    ReservationStatus.SCHEDULED,
                    ReservationStatus.ACTIVE,
                    ReservationStatus.EXPIRED_PENDING_OPERATION,
                }:
                    _require_no_reservation_overlap(
                        connection,
                        reservation,
                        exclude_id=reservation.id,
                    )
                status_clause = " AND status = ?" if expected_status is not None else ""
                ends_clause = " AND ends_at = ?" if expected_ends_at is not None else ""
                values: tuple[object, ...] = (
                    *_reservation_values(reservation)[1:],
                    str(reservation.id),
                )
                if expected_status is not None:
                    values = (*values, expected_status.value)
                if expected_ends_at is not None:
                    values = (*values, expected_ends_at.isoformat())
                cursor = connection.execute(
                    "UPDATE reservations SET bench_id = ?, owner = ?, owner_principal_id = ?, "
                    "owner_principal_type = ?, organisation_id = ?, created_at = ?, "
                    "released_at = ?, status = ?, "
                    "requested_at = ?, starts_at = ?, ends_at = ?, activated_at = ?, "
                    "expired_at = ?, source = ?, "
                    "metadata = ?, idempotency_key = ?, release_pending = ? WHERE id = ?"
                    f"{status_clause}{ends_clause}",  # noqa: S608
                    values,
                )
                if cursor.rowcount == 0:
                    if (
                        expected_status is not None or expected_ends_at is not None
                    ) and connection.execute(
                        "SELECT 1 FROM reservations WHERE id = ?", (str(reservation.id),)
                    ).fetchone():
                        raise ReservationNotActiveError(
                            f"Reservation {reservation.id} changed concurrently.",
                            reservation_id=str(reservation.id),
                        )
                    raise KeyError(str(reservation.id))
        except sqlite3.IntegrityError as exc:
            raise _reservation_integrity_error(reservation, exc) from exc
        return reservation

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        bench_id: str | None = None,
        owner: str | None = None,
        status: ReservationStatus | None = None,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        limit: int = 50,
    ) -> list[Reservation]:
        conditions: list[str] = []
        values: list[object] = []
        for condition, value in (
            (
                "organisation_id = ?",
                str(organisation_id) if organisation_id is not None else None,
            ),
            ("bench_id = ?", bench_id),
            ("owner = ?", owner),
            ("status = ?", status.value if status else None),
            ("starts_at > ?", _datetime_value(starts_after)),
            ("starts_at < ?", _datetime_value(starts_before)),
        ):
            if value is not None:
                conditions.append(condition)
                values.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM reservations{where} "  # noqa: S608
                "ORDER BY starts_at DESC, created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [_reservation_from_row(row) for row in rows]

    async def find_conflict(
        self,
        bench_id: str,
        starts_at: datetime,
        ends_at: datetime,
        *,
        exclude_id: UUID | None = None,
    ) -> Reservation | None:
        values: list[object] = [bench_id, ends_at.isoformat(), starts_at.isoformat()]
        exclude = ""
        if exclude_id is not None:
            exclude = " AND id != ?"
            values.append(str(exclude_id))
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE bench_id = ? "
                "AND status IN ('scheduled', 'active', 'expired_pending_operation') "
                "AND (starts_at IS NULL OR ends_at IS NULL "
                "OR (starts_at < ? AND ends_at > ?))"
                f"{exclude} ORDER BY starts_at LIMIT 1",  # noqa: S608
                values,
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def list_due(self, now: datetime) -> builtins.list[Reservation]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM reservations WHERE status = 'scheduled' "
                "AND starts_at <= ? ORDER BY starts_at, created_at, id",
                (now.isoformat(),),
            ).fetchall()
        return [_reservation_from_row(row) for row in rows]

    async def list_expired(self, now: datetime) -> builtins.list[Reservation]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM reservations "
                "WHERE status IN ('scheduled', 'active', 'expired_pending_operation') "
                "AND ends_at IS NOT NULL AND ends_at <= ? ORDER BY ends_at, id",
                (now.isoformat(),),
            ).fetchall()
        return [_reservation_from_row(row) for row in rows]

    async def activate_if_available(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None:
        try:
            with self._database.transaction(immediate=True) as connection:
                cursor = connection.execute(
                    "UPDATE reservations SET status = 'active', activated_at = ? "
                    "WHERE id = ? AND status = 'scheduled' AND starts_at <= ? "
                    "AND ends_at > ? AND NOT EXISTS ("
                    "SELECT 1 FROM reservations active "
                    "WHERE active.bench_id = reservations.bench_id "
                    "AND active.id != reservations.id "
                    "AND active.status IN ('active', 'expired_pending_operation')) "
                    "AND NOT EXISTS (SELECT 1 FROM operation_locks locked "
                    "WHERE locked.bench_id = reservations.bench_id)",
                    (now.isoformat(), str(reservation_id), now.isoformat(), now.isoformat()),
                )
                if cursor.rowcount == 0:
                    return None
                row = connection.execute(
                    "SELECT * FROM reservations WHERE id = ?", (str(reservation_id),)
                ).fetchone()
                activated = _reservation_from_row(row)
                if event_factory is not None:
                    insert_event(connection, event_factory(activated))
        except sqlite3.IntegrityError:
            return None
        return activated

    async def expire_if_due(
        self,
        reservation_id: UUID,
        now: datetime,
        *,
        event_factory: Callable[[Reservation], EventRecord] | None = None,
    ) -> Reservation | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE id = ?", (str(reservation_id),)
            ).fetchone()
            if row is None or row["ends_at"] is None or row["ends_at"] > now.isoformat():
                return None
            pending_operation = (
                connection.execute(
                    "SELECT 1 FROM operation_locks WHERE bench_id = ?", (row["bench_id"],)
                ).fetchone()
                is not None
            )
            current = ReservationStatus(row["status"])
            if current is ReservationStatus.EXPIRED_PENDING_OPERATION:
                if pending_operation:
                    return None
                target = ReservationStatus.EXPIRED
            elif current is ReservationStatus.ACTIVE:
                target = (
                    ReservationStatus.EXPIRED_PENDING_OPERATION
                    if pending_operation
                    else ReservationStatus.EXPIRED
                )
            elif current is ReservationStatus.SCHEDULED:
                target = ReservationStatus.EXPIRED
            else:
                return None
            connection.execute(
                "UPDATE reservations SET status = ?, expired_at = ?, release_pending = ? "
                "WHERE id = ? AND status = ?",
                (
                    target.value,
                    now.isoformat(),
                    int(target is ReservationStatus.EXPIRED_PENDING_OPERATION),
                    str(reservation_id),
                    current.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM reservations WHERE id = ?", (str(reservation_id),)
            ).fetchone()
            expired = _reservation_from_row(updated)
            if event_factory is not None:
                insert_event(connection, event_factory(expired))
        return expired

    async def next_scheduled(self, bench_id: str, after: datetime) -> Reservation | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE bench_id = ? AND status = 'scheduled' "
                "AND starts_at >= ? ORDER BY starts_at, created_at, id LIMIT 1",
                (bench_id, after.isoformat()),
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def finalize_pending_expiry(self, bench_id: str, now: datetime) -> Reservation | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id FROM reservations WHERE bench_id = ? "
                "AND status = 'expired_pending_operation' LIMIT 1",
                (bench_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE reservations SET status = 'expired', release_pending = 0, "
                "expired_at = COALESCE(expired_at, ?) WHERE id = ?",
                (now.isoformat(), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM reservations WHERE id = ?", (row["id"],)
            ).fetchone()
        return _reservation_from_row(updated)


class SQLiteQueueRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, entry: QueueEntry) -> QueueEntry:
        try:
            with self._database.transaction(immediate=True) as connection:
                next_order_row = connection.execute(
                    "SELECT COALESCE(MAX(queue_order), 0) + 1 AS next_order FROM reservation_queue"
                ).fetchone()
                queue_order = int(next_order_row["next_order"])
                values = _queue_values(entry)
                connection.execute(
                    "INSERT INTO reservation_queue "
                    "(id, organisation_id, bench_id, owner, owner_principal_id, "
                    "owner_principal_type, requested_duration_seconds, description, queue_order, "
                    "status, created_at, promoted_at, cancelled_at, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values[:8], queue_order, *values[8:]),
                )
        except sqlite3.IntegrityError:
            if entry.idempotency_key:
                existing = await self.get_by_idempotency_key(
                    entry.bench_id,
                    entry.idempotency_key,
                    organisation_id=entry.organisation_id,
                )
                if existing is not None:
                    return existing
            raise
        return await self._with_position(entry)

    async def get(
        self,
        entry_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(entry_id), str(organisation_id))
            if organisation_id is not None
            else (str(entry_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM reservation_queue WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        if row is None:
            return None
        return await self._with_position(_queue_from_row(row))

    async def get_by_idempotency_key(
        self,
        bench_id: str,
        idempotency_key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None:
        scope_id = organisation_id or LEGACY_ORGANISATION_ID
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservation_queue WHERE bench_id = ? AND idempotency_key = ?"
                " AND organisation_id = ?",
                (bench_id, idempotency_key, str(scope_id)),
            ).fetchone()
        if row is None:
            return None
        return await self._with_position(_queue_from_row(row))

    async def list(
        self,
        *,
        bench_id: str,
        organisation_id: UUID | None = None,
        status: QueueEntryStatus | None = QueueEntryStatus.WAITING,
    ) -> builtins.list[QueueEntry]:
        query = "SELECT * FROM reservation_queue WHERE bench_id = ?"
        values: list[object] = [bench_id]
        if organisation_id is not None:
            query += " AND organisation_id = ?"
            values.append(str(organisation_id))
        if status is not None:
            query += " AND status = ?"
            values.append(status.value)
        query += " ORDER BY queue_order, id"
        with self._database.transaction() as connection:
            rows = connection.execute(query, values).fetchall()
        entries = [_queue_from_row(row) for row in rows]
        if status is QueueEntryStatus.WAITING:
            return [
                entry.model_copy(update={"position": index})
                for index, entry in enumerate(entries, 1)
            ]
        return entries

    async def list_waiting_benches(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> builtins.list[str]:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (str(organisation_id),) if organisation_id is not None else ()
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT bench_id, MIN(queue_order) AS first_order FROM reservation_queue "
                f"WHERE status = 'waiting'{scope} "  # noqa: S608
                "GROUP BY bench_id ORDER BY first_order, bench_id",
                values,
            ).fetchall()
        return [row["bench_id"] for row in rows]

    async def list_waiting(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 10_000,
    ) -> builtins.list[QueueEntry]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        scope = " AND queued.organisation_id = ?" if organisation_id is not None else ""
        values: builtins.list[object] = []
        if organisation_id is not None:
            values.append(str(organisation_id))
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT queued.* FROM reservation_queue queued "
                "WHERE queued.status = 'waiting'"
                f"{scope} AND NOT EXISTS ("  # noqa: S608
                "SELECT 1 FROM reservation_queue earlier "
                "WHERE earlier.organisation_id = queued.organisation_id "
                "AND earlier.bench_id = queued.bench_id "
                "AND earlier.status = 'waiting' "
                "AND earlier.queue_order < queued.queue_order) "
                "ORDER BY queued.queue_order, queued.id LIMIT ?",
                values,
            ).fetchall()
        return [await self._with_position(_queue_from_row(row)) for row in rows]

    async def cancel(
        self,
        entry_id: UUID,
        owner: str,
        now: datetime,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(entry_id), str(organisation_id))
            if organisation_id is not None
            else (str(entry_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                f"SELECT * FROM reservation_queue WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
            if row is None:
                raise QueueEntryNotFoundError(f"Queue entry {entry_id} was not found.")
            if row["owner"] != owner:
                raise QueueOwnerMismatchError("Only the queue entry owner may cancel it.")
            if row["status"] == QueueEntryStatus.CANCELLED.value:
                return _queue_from_row(row)
            if row["status"] != QueueEntryStatus.WAITING.value:
                raise QueueEntryNotFoundError(f"Queue entry {entry_id} is no longer waiting.")
            connection.execute(
                "UPDATE reservation_queue SET status = 'cancelled', cancelled_at = ? "
                f"WHERE id = ? AND status = 'waiting'{scope}",  # noqa: S608
                (now.isoformat(), *values),
            )
            updated = connection.execute(
                f"SELECT * FROM reservation_queue WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _queue_from_row(updated)

    async def mark_promoted(
        self,
        entry_id: UUID,
        now: datetime,
        *,
        organisation_id: UUID | None = None,
    ) -> QueueEntry | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(entry_id), str(organisation_id))
            if organisation_id is not None
            else (str(entry_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE reservation_queue SET status = 'promoted', promoted_at = ? "
                f"WHERE id = ? AND status = 'waiting'{scope}",  # noqa: S608
                (now.isoformat(), *values),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                f"SELECT * FROM reservation_queue WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _queue_from_row(row) if row is not None else None

    async def promote(
        self,
        entry_id: UUID,
        reservation: Reservation,
        now: datetime,
        *,
        protection_window: timedelta | None = None,
        event_factory: Callable[[QueueEntry, Reservation], Iterable[EventRecord]] | None = None,
    ) -> tuple[QueueEntry, Reservation] | None:
        try:
            with self._database.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM reservation_queue WHERE id = ? AND organisation_id = ? "
                    "AND status = 'waiting'",
                    (str(entry_id), str(reservation.organisation_id)),
                ).fetchone()
                if row is None:
                    return None
                active = connection.execute(
                    "SELECT 1 FROM reservations WHERE bench_id = ? "
                    "AND organisation_id = ? "
                    "AND status IN ('active', 'expired_pending_operation') LIMIT 1",
                    (row["bench_id"], row["organisation_id"]),
                ).fetchone()
                if active is not None:
                    return None
                if connection.execute(
                    "SELECT 1 FROM operation_locks WHERE bench_id = ? AND organisation_id = ?",
                    (row["bench_id"], row["organisation_id"]),
                ).fetchone():
                    return None
                next_scheduled = connection.execute(
                    "SELECT starts_at FROM reservations WHERE bench_id = ? "
                    "AND organisation_id = ? AND status = 'scheduled' AND starts_at >= ? "
                    "ORDER BY starts_at LIMIT 1",
                    (row["bench_id"], row["organisation_id"], now.isoformat()),
                ).fetchone()
                if (
                    next_scheduled is not None
                    and reservation.ends_at is not None
                    and reservation.ends_at + (protection_window or timedelta())
                    > datetime.fromisoformat(next_scheduled["starts_at"])
                ):
                    return None
                connection.execute(
                    "INSERT INTO reservations "
                    "(id, bench_id, owner, owner_principal_id, owner_principal_type, "
                    "organisation_id, created_at, released_at, status, requested_at, "
                    "starts_at, ends_at, activated_at, expired_at, source, metadata, "
                    "idempotency_key, release_pending) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _reservation_values(reservation),
                )
                cursor = connection.execute(
                    "UPDATE reservation_queue SET status = 'promoted', promoted_at = ? "
                    "WHERE id = ? AND organisation_id = ? AND status = 'waiting'",
                    (now.isoformat(), str(entry_id), row["organisation_id"]),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("queue_promotion_race")
                promoted_row = connection.execute(
                    "SELECT * FROM reservation_queue WHERE id = ? AND organisation_id = ?",
                    (str(entry_id), row["organisation_id"]),
                ).fetchone()
                promoted_entry = _queue_from_row(promoted_row)
                if event_factory is not None:
                    for event in event_factory(promoted_entry, reservation):
                        insert_event(connection, event)
        except sqlite3.IntegrityError:
            return None
        return promoted_entry, reservation

    async def _with_position(self, entry: QueueEntry) -> QueueEntry:
        if entry.status is not QueueEntryStatus.WAITING:
            return entry.model_copy(update={"position": None})
        query = (
            "SELECT COUNT(*) AS position FROM reservation_queue queued "
            "WHERE queued.bench_id = ? AND queued.organisation_id = ? "
            "AND queued.status = 'waiting' AND queued.queue_order <= ("
            "SELECT current.queue_order FROM reservation_queue current "
            "WHERE current.id = ? AND current.organisation_id = ?)"
        )
        values = (
            entry.bench_id,
            str(entry.organisation_id),
            str(entry.id),
            str(entry.organisation_id),
        )
        with self._database.transaction() as connection:
            row = connection.execute(query, values).fetchone()
        return entry.model_copy(update={"position": row["position"]})


class SQLiteOperationLockRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def acquire(self, lock: BenchOperationLock) -> BenchOperationLock:
        try:
            with self._database.transaction(immediate=True) as connection:
                _insert_operation_lock(connection, lock)
        except sqlite3.IntegrityError as exc:
            return await self._resolve_integrity_error(lock, exc)
        return lock

    async def acquire_for_active(
        self,
        lock: BenchOperationLock,
        owner: str,
        now: datetime,
    ) -> BenchOperationLock:
        """Atomically authorize an owner and acquire the bench operation lock."""

        try:
            with self._database.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT owner, status, ends_at FROM reservations WHERE bench_id = ? "
                    "AND status IN ('active', 'expired_pending_operation') LIMIT 1",
                    (lock.bench_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != ReservationStatus.ACTIVE.value
                    or row["ends_at"] is not None
                    and row["ends_at"] <= now.isoformat()
                ):
                    raise ReservationNotActiveError(
                        f"Bench {lock.bench_id} requires an active reservation.",
                        bench_id=lock.bench_id,
                    )
                if row["owner"] != owner:
                    raise ReservationOwnerMismatchError(
                        "Only the reservation owner may operate this bench.",
                        bench_id=lock.bench_id,
                    )
                _insert_operation_lock(connection, lock)
        except sqlite3.IntegrityError as exc:
            return await self._resolve_integrity_error(lock, exc)
        return lock

    async def acquire_for_maintenance(
        self,
        lock: BenchOperationLock,
        now: datetime,
    ) -> BenchOperationLock:
        """Atomically acquire a lock only while no reservation owns the bench."""

        try:
            with self._database.transaction(immediate=True) as connection:
                reservation = connection.execute(
                    "SELECT id FROM reservations WHERE bench_id = ? AND ("
                    "status IN ('active', 'expired_pending_operation') OR ("
                    "status = 'scheduled' AND starts_at <= ?)) LIMIT 1",
                    (
                        lock.bench_id,
                        _datetime_value(lock.expires_at) or now.isoformat(),
                    ),
                ).fetchone()
                if reservation is not None:
                    raise BenchOperationInProgressError(
                        f"Bench {lock.bench_id} is reserved and cannot be maintenance-probed.",
                        bench_id=lock.bench_id,
                        reservation_id=reservation["id"],
                    )
                _insert_operation_lock(connection, lock)
        except sqlite3.IntegrityError as exc:
            return await self._resolve_integrity_error(lock, exc)
        return lock

    async def _resolve_integrity_error(
        self,
        lock: BenchOperationLock,
        exc: sqlite3.IntegrityError,
    ) -> BenchOperationLock:
        existing = await self.get(lock.bench_id)
        if existing is not None and existing.operation_id == lock.operation_id:
            return existing
        raise BenchOperationInProgressError(
            f"Bench {lock.bench_id} already has a mutating operation.",
            bench_id=lock.bench_id,
        ) from exc

    async def get(self, bench_id: str) -> BenchOperationLock | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operation_locks WHERE bench_id = ?", (bench_id,)
            ).fetchone()
        return _lock_from_row(row) if row is not None else None

    async def release(self, bench_id: str, operation_id: UUID) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM operation_locks WHERE bench_id = ? AND operation_id = ?",
                (bench_id, str(operation_id)),
            )
        return cursor.rowcount == 1

    async def list(self) -> list[BenchOperationLock]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_locks ORDER BY acquired_at, bench_id"
            ).fetchall()
        return [_lock_from_row(row) for row in rows]

    async def recover_stale(self, live_operation_ids: set[UUID], now: datetime) -> int:
        live = {str(operation_id) for operation_id in live_operation_ids}
        with self._database.transaction(immediate=True) as connection:
            rows = connection.execute("SELECT * FROM operation_locks").fetchall()
            stale = [
                row["bench_id"]
                for row in rows
                if row["operation_id"] not in live
                or row["expires_at"] is not None
                and row["expires_at"] <= now.isoformat()
            ]
            if not stale:
                return 0
            placeholders = ", ".join("?" for _ in stale)
            cursor = connection.execute(
                f"DELETE FROM operation_locks WHERE bench_id IN ({placeholders})",  # noqa: S608
                stale,
            )
        return cursor.rowcount


class SQLiteRecoveryRepository:
    """Persist recovery attempts, leaving incomplete rows visible after a crash."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def start(self, started_at: datetime) -> int:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO recovery_records(started_at, report) VALUES (?, '{}')",
                (started_at.isoformat(),),
            )
            record_id = cursor.lastrowid
        if record_id is None:  # pragma: no cover - SQLite assigns AUTOINCREMENT IDs
            raise RuntimeError("Recovery record was not persisted")
        return record_id

    async def complete(self, record_id: int, report: RecoveryReport) -> None:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE recovery_records SET completed_at = ?, report = ? WHERE id = ?",
                (
                    report.completed_at.isoformat(),
                    json.dumps(report.model_dump(mode="json"), sort_keys=True),
                    record_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(record_id)

    async def list(self) -> list[RecoveryReport]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT report FROM recovery_records WHERE completed_at IS NOT NULL ORDER BY id"
            ).fetchall()
        return [RecoveryReport.model_validate(json.loads(row["report"])) for row in rows]


class SQLiteTimelineRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def list_timeline(
        self,
        bench_id: str,
        *,
        category: TimelineCategory | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[BenchTimelineEntry]:
        conditions = ["bench_id = ?"]
        values: list[object] = [bench_id]
        if after is not None:
            conditions.append("timestamp > ?")
            values.append(after.isoformat())
        if before is not None:
            conditions.append("timestamp < ?")
            values.append(before.isoformat())
        values.append(limit * 8 if category is not None else limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE "
                + " AND ".join(conditions)
                + " ORDER BY timestamp DESC, id DESC LIMIT ?",
                values,
            ).fetchall()
        timeline = [_timeline_from_row(row) for row in rows]
        if category is not None:
            timeline = [entry for entry in timeline if entry.category is category]
        return timeline[:limit]


def _reservation_values(reservation: Reservation) -> tuple[object, ...]:
    return (
        str(reservation.id),
        reservation.bench_id,
        reservation.owner,
        str(reservation.owner_principal_id) if reservation.owner_principal_id else None,
        reservation.owner_principal_type,
        str(reservation.organisation_id),
        reservation.created_at.isoformat(),
        _datetime_value(reservation.released_at),
        reservation.status.value,
        _datetime_value(reservation.requested_at),
        _datetime_value(reservation.starts_at),
        _datetime_value(reservation.ends_at),
        _datetime_value(reservation.activated_at),
        _datetime_value(reservation.expired_at),
        reservation.source.value,
        json.dumps(reservation.metadata, sort_keys=True),
        reservation.idempotency_key,
        int(reservation.release_pending),
    )


def _reservation_from_row(row: sqlite3.Row) -> Reservation:
    return Reservation(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        owner_principal_id=(UUID(row["owner_principal_id"]) if row["owner_principal_id"] else None),
        owner_principal_type=row["owner_principal_type"],
        created_at=datetime.fromisoformat(row["created_at"]),
        released_at=_parse_datetime(row["released_at"]),
        status=ReservationStatus(row["status"]),
        requested_at=_parse_datetime(row["requested_at"]),
        starts_at=_parse_datetime(row["starts_at"]),
        ends_at=_parse_datetime(row["ends_at"]),
        activated_at=_parse_datetime(row["activated_at"]),
        expired_at=_parse_datetime(row["expired_at"]),
        source=ReservationSource(row["source"]),
        metadata=json.loads(row["metadata"]),
        idempotency_key=row["idempotency_key"],
        release_pending=bool(row["release_pending"]),
    )


def _queue_values(entry: QueueEntry) -> tuple[object, ...]:
    return (
        str(entry.id),
        str(entry.organisation_id),
        entry.bench_id,
        entry.owner,
        str(entry.owner_principal_id) if entry.owner_principal_id is not None else None,
        entry.owner_principal_type,
        entry.requested_duration_seconds,
        entry.description,
        entry.status.value,
        entry.created_at.isoformat(),
        _datetime_value(entry.promoted_at),
        _datetime_value(entry.cancelled_at),
        entry.idempotency_key,
    )


def _queue_from_row(row: sqlite3.Row) -> QueueEntry:
    return QueueEntry(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        owner_principal_id=(UUID(row["owner_principal_id"]) if row["owner_principal_id"] else None),
        owner_principal_type=row["owner_principal_type"],
        requested_duration_seconds=row["requested_duration_seconds"],
        description=row["description"],
        status=QueueEntryStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        promoted_at=_parse_datetime(row["promoted_at"]),
        cancelled_at=_parse_datetime(row["cancelled_at"]),
        idempotency_key=row["idempotency_key"],
    )


def _lock_from_row(row: sqlite3.Row) -> BenchOperationLock:
    return BenchOperationLock(
        bench_id=row["bench_id"],
        operation_id=UUID(row["operation_id"]),
        acquired_at=datetime.fromisoformat(row["acquired_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
    )


def _insert_operation_lock(
    connection: sqlite3.Connection,
    lock: BenchOperationLock,
) -> None:
    connection.execute(
        "INSERT INTO operation_locks "
        "(bench_id, operation_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (
            lock.bench_id,
            str(lock.operation_id),
            lock.acquired_at.isoformat(),
            _datetime_value(lock.expires_at),
        ),
    )


def _require_no_operation_lock(connection: sqlite3.Connection, bench_id: str) -> None:
    row = connection.execute(
        "SELECT operation_id FROM operation_locks WHERE bench_id = ?", (bench_id,)
    ).fetchone()
    if row is not None:
        raise BenchOperationInProgressError(
            f"Bench {bench_id} already has a mutating operation.",
            bench_id=bench_id,
            operation_id=row["operation_id"],
        )


def _require_no_maintenance_lock_overlap(
    connection: sqlite3.Connection,
    reservation: Reservation,
) -> None:
    if reservation.starts_at is None:
        return
    row = connection.execute(
        "SELECT operation_id FROM operation_locks WHERE bench_id = ? "
        "AND expires_at IS NOT NULL AND expires_at >= ? LIMIT 1",
        (reservation.bench_id, reservation.starts_at.isoformat()),
    ).fetchone()
    if row is not None:
        raise BenchOperationInProgressError(
            f"Bench {reservation.bench_id} is maintenance-locked through the requested start.",
            bench_id=reservation.bench_id,
            operation_id=row["operation_id"],
        )


def _require_no_reservation_overlap(
    connection: sqlite3.Connection,
    reservation: Reservation,
    *,
    exclude_id: UUID | None = None,
) -> None:
    if reservation.starts_at is None or reservation.ends_at is None:
        return
    conditions = [
        "bench_id = ?",
        "status IN ('scheduled', 'active', 'expired_pending_operation')",
        "(starts_at IS NULL OR ends_at IS NULL OR (starts_at < ? AND ends_at > ?))",
    ]
    values: list[object] = [
        reservation.bench_id,
        reservation.ends_at.isoformat(),
        reservation.starts_at.isoformat(),
    ]
    if exclude_id is not None:
        conditions.append("id != ?")
        values.append(str(exclude_id))
    if reservation.idempotency_key is not None:
        conditions.append("NOT (organisation_id = ? AND idempotency_key = ?)")
        values.extend((str(reservation.organisation_id), reservation.idempotency_key))
    row = connection.execute(
        f"SELECT id FROM reservations WHERE {' AND '.join(conditions)} LIMIT 1",  # noqa: S608
        values,
    ).fetchone()
    if row is not None:
        raise ReservationTimeConflictError(
            f"Reservation for bench {reservation.bench_id} conflicts with an existing reservation.",
            bench_id=reservation.bench_id,
            conflicting_reservation_id=row["id"],
        )


def _timeline_from_row(row: sqlite3.Row) -> BenchTimelineEntry:
    payload = json.loads(row["payload"])
    event_type = row["type"]
    payload_operation_id = payload.get("operation_id")
    return BenchTimelineEntry(
        id=UUID(row["id"]),
        bench_id=row["bench_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        category=_timeline_category(event_type),
        event_type=event_type,
        actor=row["actor"],
        reservation_id=UUID(row["reservation_id"]) if row["reservation_id"] else None,
        operation_id=(
            UUID(row["operation_id"])
            if row["operation_id"]
            else UUID(payload_operation_id)
            if isinstance(payload_operation_id, str)
            else None
        ),
        summary=str(payload.get("summary") or event_type.replace("_", " ").title()),
        payload=payload,
    )


def _timeline_category(event_type: str) -> TimelineCategory:
    if event_type.startswith(("RESERVATION_", "QUEUE_", "BENCH_RESERVED", "BENCH_RELEASED")):
        return TimelineCategory.RESERVATION
    if event_type.startswith(("OPERATION_", "FLASH_", "POWER_", "SERIAL_", "RESET_")):
        return TimelineCategory.OPERATION
    if "HEALTH" in event_type or event_type.endswith(("ONLINE", "OFFLINE")):
        return TimelineCategory.HEALTH
    if event_type.startswith("WORKFLOW_"):
        return TimelineCategory.WORKFLOW
    if event_type.startswith(("USB_", "HARDWARE_")):
        return TimelineCategory.HARDWARE
    return TimelineCategory.SYSTEM


def _reservation_integrity_error(
    reservation: Reservation, exc: sqlite3.IntegrityError
) -> ReservationTimeConflictError:
    return ReservationTimeConflictError(
        f"Reservation for bench {reservation.bench_id} conflicts with an existing reservation.",
        bench_id=reservation.bench_id,
        database_error=str(exc),
    )


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
