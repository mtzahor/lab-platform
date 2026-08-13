from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from lab_platform.core.errors import BenchOperationInProgressError
from lab_platform.models import (
    EventRecord,
    FirmwareArtifact,
    Operation,
    OperationArtifact,
    OperationStatus,
    OperationType,
    Reservation,
    ReservationStatus,
)
from lab_platform.persistence.migrations import SCHEMA_VERSION as SCHEMA_VERSION
from lab_platform.persistence.migrations import apply_migrations


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, bench_id TEXT NOT NULL, owner TEXT NOT NULL,
                    created_at TEXT NOT NULL, released_at TEXT, status TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS reservations_one_active_per_bench
                    ON reservations(bench_id) WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, bench_id TEXT NOT NULL, type TEXT NOT NULL,
                    status TEXT NOT NULL, requested_by TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, progress INTEGER, message TEXT,
                    error_code TEXT, error_message TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS operations_one_active_per_bench
                    ON operations(bench_id)
                    WHERE status IN ('pending', 'running', 'cancel_requested');
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, type TEXT NOT NULL,
                    source TEXT NOT NULL, bench_id TEXT, operation_id TEXT, actor TEXT,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS events_bench ON events(bench_id, timestamp DESC);
                CREATE TABLE IF NOT EXISTS firmware_artifacts (
                    sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, local_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, version TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_artifacts (
                    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, type TEXT NOT NULL,
                    path TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS operation_artifacts_operation
                    ON operation_artifacts(operation_id, created_at);
                """
            )
            apply_migrations(connection)
            connection.commit()
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not initialized")
        return self._connection


class SQLiteReservationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, reservation: Reservation) -> Reservation:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE bench_id = ? AND status = 'active'",
                (reservation.bench_id,),
            ).fetchone()
            if row is not None:
                return _reservation_from_row(row)
            connection.execute(
                "INSERT INTO reservations "
                "(id, bench_id, owner, owner_principal_id, owner_principal_type, "
                "organisation_id, created_at, released_at, status, requested_at, "
                "starts_at, ends_at, activated_at, expired_at, source, metadata, "
                "idempotency_key, release_pending) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(reservation.id),
                    reservation.bench_id,
                    reservation.owner,
                    (
                        str(reservation.owner_principal_id)
                        if reservation.owner_principal_id
                        else None
                    ),
                    reservation.owner_principal_type,
                    str(reservation.organisation_id),
                    reservation.created_at.isoformat(),
                    None,
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
                ),
            )
        return reservation

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
                f"SELECT * FROM reservations WHERE bench_id = ? AND status = 'active'{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _reservation_from_row(row) if row is not None else None

    async def release(self, reservation: Reservation) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reservations SET status = ?, released_at = ? WHERE id = ?",
                (
                    reservation.status.value,
                    _datetime_value(reservation.released_at),
                    str(reservation.id),
                ),
            )


class SQLiteOperationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, operation: Operation) -> Operation:
        try:
            with self._database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO operations "
                    "(id, bench_id, type, status, requested_by, created_at, started_at, "
                    "completed_at, progress, message, error_code, error_message) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _operation_values(operation),
                )
        except sqlite3.IntegrityError as exc:
            if "operations.bench_id" in str(exc):
                raise BenchOperationInProgressError(
                    f"Bench {operation.bench_id} already has an operation in progress.",
                    bench_id=operation.bench_id,
                ) from exc
            raise
        return operation

    async def get(self, operation_id: UUID) -> Operation | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (str(operation_id),)
            ).fetchone()
        return _operation_from_row(row) if row is not None else None

    async def update(self, operation: Operation) -> Operation:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE operations SET bench_id = ?, type = ?, status = ?, "
                "requested_by = ?, created_at = ?, started_at = ?, completed_at = ?, "
                "progress = ?, message = ?, error_code = ?, error_message = ? WHERE id = ?",
                (*_operation_values(operation)[1:], str(operation.id)),
            )
        return operation

    async def list(
        self,
        *,
        bench_id: str | None = None,
        status: OperationStatus | None = None,
        operation_type: OperationType | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        conditions: list[str] = []
        values: list[object] = []
        if bench_id is not None:
            conditions.append("bench_id = ?")
            values.append(bench_id)
        if status is not None:
            conditions.append("status = ?")
            values.append(status.value)
        if operation_type is not None:
            conditions.append("type = ?")
            values.append(operation_type.value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM operations{where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                values,
            ).fetchall()
        return [_operation_from_row(row) for row in rows]

    async def recover_incomplete(self, now: datetime) -> int:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE operations SET status = 'failed', completed_at = ?, "
                "message = 'Agent restarted before operation completed', "
                "error_code = 'AGENT_RESTARTED', "
                "error_message = 'The Agent restarted while this operation was active' "
                "WHERE status IN ('pending', 'running', 'cancel_requested')",
                (now.isoformat(),),
            )
            return cursor.rowcount


class SQLiteEventRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, event: EventRecord) -> EventRecord:
        with self._database.transaction(immediate=True) as connection:
            insert_event(connection, event)
        return event

    async def list(
        self,
        *,
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        conditions: list[str] = []
        values: list[object] = []
        filters = (
            ("bench_id = ?", bench_id),
            ("type = ?", event_type),
            ("timestamp > ?", _datetime_value(after)),
            ("timestamp < ?", _datetime_value(before)),
        )
        for condition, value in filters:
            if value is not None:
                conditions.append(condition)
                values.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM events{where} ORDER BY timestamp DESC LIMIT ?",  # noqa: S608
                values,
            ).fetchall()
        return [_event_from_row(row) for row in rows]


class SQLiteArtifactRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def save(self, artifact: FirmwareArtifact) -> FirmwareArtifact:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO firmware_artifacts "
                "(sha256, filename, local_path, size_bytes, version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(sha256) DO UPDATE SET "
                "filename = excluded.filename, local_path = excluded.local_path, "
                "size_bytes = excluded.size_bytes, version = excluded.version",
                (
                    artifact.sha256,
                    artifact.filename,
                    str(artifact.local_path),
                    artifact.size_bytes,
                    artifact.version,
                    artifact.created_at.isoformat(),
                ),
            )
        return artifact


class SQLiteOperationArtifactRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def save(self, artifact: OperationArtifact) -> OperationArtifact:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO operation_artifacts "
                "(id, operation_id, type, path, size_bytes, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(artifact.id),
                    str(artifact.operation_id),
                    artifact.type,
                    str(artifact.path),
                    artifact.size_bytes,
                    artifact.sha256,
                    artifact.created_at.isoformat(),
                ),
            )
        return artifact

    async def list_for_operation(self, operation_id: UUID) -> list[OperationArtifact]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_artifacts WHERE operation_id = ? ORDER BY created_at",
                (str(operation_id),),
            ).fetchall()
        return [_operation_artifact_from_row(row) for row in rows]


def _operation_values(operation: Operation) -> tuple[object, ...]:
    return (
        str(operation.id),
        operation.bench_id,
        operation.type.value,
        operation.status.value,
        operation.requested_by,
        operation.created_at.isoformat(),
        _datetime_value(operation.started_at),
        _datetime_value(operation.completed_at),
        operation.progress,
        operation.message,
        operation.error_code,
        operation.error_message,
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
        source=row["source"],
        metadata=json.loads(row["metadata"]),
        idempotency_key=row["idempotency_key"],
        release_pending=bool(row["release_pending"]),
    )


def _operation_from_row(row: sqlite3.Row) -> Operation:
    return Operation(
        id=UUID(row["id"]),
        bench_id=row["bench_id"],
        type=OperationType(row["type"]),
        status=OperationStatus(row["status"]),
        requested_by=row["requested_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        progress=row["progress"],
        message=row["message"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def insert_event(connection: sqlite3.Connection, event: EventRecord) -> None:
    """Insert an idempotent event inside an existing SQLite unit of work."""

    connection.execute(
        "INSERT OR IGNORE INTO events "
        "(id, timestamp, type, source, bench_id, reservation_id, operation_id, "
        "actor, payload, deduplication_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(event.id),
            event.timestamp.isoformat(),
            event.type,
            event.source,
            event.bench_id,
            str(event.reservation_id) if event.reservation_id else None,
            str(event.operation_id) if event.operation_id else None,
            event.actor,
            json.dumps(event.payload, sort_keys=True),
            event.deduplication_key,
        ),
    )


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        id=UUID(row["id"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        type=row["type"],
        source=row["source"],
        bench_id=row["bench_id"],
        reservation_id=UUID(row["reservation_id"]) if row["reservation_id"] else None,
        operation_id=UUID(row["operation_id"]) if row["operation_id"] else None,
        actor=row["actor"],
        payload=json.loads(row["payload"]),
        deduplication_key=row["deduplication_key"],
    )


def _operation_artifact_from_row(row: sqlite3.Row) -> OperationArtifact:
    return OperationArtifact(
        id=UUID(row["id"]),
        operation_id=UUID(row["operation_id"]),
        type=row["type"],
        path=Path(row["path"]),
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
