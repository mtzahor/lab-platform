from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast, overload

from lab_platform.core.errors import ConfigurationError
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.migrations import SCHEMA_VERSION, apply_migrations

PSYCOPG_INTEGRITY_ERRORS: tuple[type[BaseException], ...] = ()

_BASE_SCHEMA_SQL = """
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

_COMPATIBILITY_FUNCTIONS_SQL = (
    """
    CREATE OR REPLACE FUNCTION lab_platform_is_valid_json(value TEXT)
    RETURNS BOOLEAN
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    AS $$
    BEGIN
        PERFORM value::jsonb;
        RETURN TRUE;
    EXCEPTION WHEN others THEN
        RETURN FALSE;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lab_platform_json_kind(value TEXT)
    RETURNS TEXT
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    AS $$
    BEGIN
        RETURN jsonb_typeof(value::jsonb);
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lab_platform_julianday(value TEXT)
    RETURNS DOUBLE PRECISION
    LANGUAGE plpgsql
    STABLE
    STRICT
    AS $$
    BEGIN
        IF lower(value) = 'now' THEN
            RETURN extract(epoch FROM CURRENT_TIMESTAMP) / 86400.0 + 2440587.5;
        END IF;
        RETURN extract(epoch FROM value::timestamptz) / 86400.0 + 2440587.5;
    END;
    $$
    """,
)

_RESERVATION_TRIGGER_SQL = (
    """
    CREATE OR REPLACE FUNCTION lab_platform_validate_reservation()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.starts_at IS NOT NULL AND NEW.ends_at IS NOT NULL THEN
            IF NEW.status NOT IN (
                'queued', 'scheduled', 'active', 'released', 'expired',
                'cancelled', 'rejected', 'expired_pending_operation'
            ) THEN
                RAISE EXCEPTION 'reservation_invalid_status' USING ERRCODE = '23514';
            END IF;
            IF NEW.ends_at <= NEW.starts_at THEN
                RAISE EXCEPTION 'reservation_invalid_time' USING ERRCODE = '23514';
            END IF;
            IF NEW.status IN ('scheduled', 'active', 'expired_pending_operation') AND EXISTS (
                SELECT 1 FROM reservations AS existing
                WHERE existing.id <> NEW.id
                  AND existing.bench_id = NEW.bench_id
                  AND existing.status IN (
                      'scheduled', 'active', 'expired_pending_operation'
                  )
                  AND (
                      existing.starts_at IS NULL OR existing.ends_at IS NULL
                      OR (
                          existing.starts_at < NEW.ends_at
                          AND existing.ends_at > NEW.starts_at
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'reservation_time_conflict' USING ERRCODE = '23505';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS reservations_validate_insert ON reservations",
    """
    CREATE TRIGGER reservations_validate_insert
    BEFORE INSERT ON reservations
    FOR EACH ROW EXECUTE FUNCTION lab_platform_validate_reservation()
    """,
    "DROP TRIGGER IF EXISTS reservations_validate_update ON reservations",
    """
    CREATE TRIGGER reservations_validate_update
    BEFORE UPDATE OF bench_id, starts_at, ends_at, status ON reservations
    FOR EACH ROW EXECUTE FUNCTION lab_platform_validate_reservation()
    """,
)

_QUEUE_TRIGGER_SQL = (
    """
    CREATE OR REPLACE FUNCTION lab_platform_validate_queue_transition()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF OLD.status <> NEW.status AND NOT (
            OLD.status = 'waiting'
            AND NEW.status IN ('promoted', 'cancelled', 'expired')
        ) THEN
            RAISE EXCEPTION 'queue_invalid_status_transition' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS reservation_queue_validate_status_update ON reservation_queue",
    """
    CREATE TRIGGER reservation_queue_validate_status_update
    BEFORE UPDATE OF status ON reservation_queue
    FOR EACH ROW EXECUTE FUNCTION lab_platform_validate_queue_transition()
    """,
)

_RESERVATION_TRIGGER_MARKER = "DROP TRIGGER IF EXISTS reservations_validate_insert;"
_QUEUE_TRIGGER_MARKER = "DROP TRIGGER IF EXISTS reservation_queue_validate_status_update;"
_SET_TRANSACTION_ISOLATION_SQL = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
_WRITER_LOCK_SQL = "SELECT pg_advisory_xact_lock(1279349840, 5)"


class PostgreSQLRow(Mapping[str, Any]):
    """A psycopg result row with the two access modes used by sqlite3.Row."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = tuple(names)
        self._values = tuple(values)
        self._positions = {name: index for index, name in enumerate(self._names)}

    @overload
    def __getitem__(self, key: str) -> Any: ...

    @overload
    def __getitem__(self, key: int) -> Any: ...

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._positions[key]]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


class _StaticCursor:
    def __init__(self, rows: Sequence[PostgreSQLRow]) -> None:
        self._rows = list(rows)
        self._position = 0
        self.rowcount = len(rows)
        self.lastrowid: int | None = None

    def fetchone(self) -> PostgreSQLRow | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[PostgreSQLRow]:
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[PostgreSQLRow]:
        return iter(self.fetchall())


class PostgreSQLCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._names = _column_names(cursor)

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @property
    def lastrowid(self) -> int | None:
        value = getattr(self._cursor, "lastrowid", None)
        return int(value) if value is not None else None

    def fetchone(self) -> PostgreSQLRow | None:
        row = self._cursor.fetchone()
        return _row(row, self._names)

    def fetchall(self) -> list[PostgreSQLRow]:
        return [_row_required(row, self._names) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[PostgreSQLRow]:
        for row in self._cursor:
            yield _row_required(row, self._names)


class PostgreSQLConnection:
    """Small DB-API compatibility surface consumed by the existing repositories."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._savepoint_counter = 0

    def execute(
        self,
        query: str,
        parameters: Sequence[object] = (),
    ) -> PostgreSQLCursor | _StaticCursor:
        normalized = query.strip()
        pragma_match = re.fullmatch(
            r"PRAGMA\s+table_info\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if pragma_match is not None:
            return self._table_info(pragma_match.group(1))
        if re.search(r"\bFROM\s+sqlite_master\b", normalized, flags=re.IGNORECASE):
            return self._table_definition("workflow_step_results")
        return self._execute_translated(query, parameters)

    def executescript(self, script: str) -> None:
        reservation_prefix, separator, _ = script.partition(_RESERVATION_TRIGGER_MARKER)
        if separator:
            self._execute_statements(reservation_prefix)
            self._execute_native_statements(_RESERVATION_TRIGGER_SQL)
            return
        if _QUEUE_TRIGGER_MARKER in script:
            self._execute_native_statements(_QUEUE_TRIGGER_SQL)
            return
        self._execute_statements(script)

    def _execute_statements(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def _execute_native_statements(self, statements: Sequence[str]) -> None:
        for statement in statements:
            self._execute_translated(statement, (), translate=False)

    def _execute_translated(
        self,
        query: str,
        parameters: Sequence[object],
        *,
        translate: bool = True,
    ) -> PostgreSQLCursor:
        statement = _translate_sql(query) if translate else query
        recoverable = _requires_recoverable_savepoint(statement)
        savepoint = ""
        if recoverable:
            self._savepoint_counter += 1
            savepoint = f"lab_platform_statement_{self._savepoint_counter}"
            self._connection.execute(f"SAVEPOINT {savepoint}")
        try:
            cursor = self._connection.execute(statement, tuple(parameters))
        except Exception as exc:
            if savepoint:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if _is_integrity_error(exc):
                raise _translate_integrity_error(exc) from exc
            raise
        if savepoint:
            self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return PostgreSQLCursor(cursor)

    def _table_info(self, table: str) -> _StaticCursor:
        cursor = self._connection.execute(
            "SELECT ordinal_position - 1 AS cid, column_name AS name, "
            "data_type AS type, CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
            "column_default AS dflt_value, 0 AS pk "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
        return _StaticCursor(
            [_row_required(row, _column_names(cursor)) for row in cursor.fetchall()]
        )

    def _table_definition(self, table: str) -> _StaticCursor:
        cursor = self._connection.execute(
            "SELECT COALESCE(string_agg(pg_get_constraintdef(constraint_row.oid), ' '), '') "
            "AS sql FROM pg_constraint AS constraint_row "
            "WHERE constraint_row.conrelid = to_regclass(current_schema() || '.' || %s)",
            (table,),
        )
        row = cursor.fetchone()
        return _StaticCursor([] if row is None else [_row_required(row, _column_names(cursor))])


class PostgreSQLDatabase(SQLiteDatabase):
    """Serialized PostgreSQL control-plane storage with SQLite repository compatibility.

    Phase 5 runs one control-plane process. The transaction-scoped advisory lock used for
    ``immediate`` units of work preserves the serialized check-and-write semantics on which
    those repositories rely, while PostgreSQL constraints remain the final fencing layer.
    """

    def __init__(
        self,
        url: str,
        *,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(Path("."))
        self.url = _normalize_postgresql_url(url)
        self._connect_factory = connect_factory
        self._pg_connection: Any | None = None
        self._compat_connection: PostgreSQLConnection | None = None

    def initialize(self) -> None:
        with self._lock:
            if self._pg_connection is not None:
                return
            factory = self._connect_factory or _connect_psycopg
            connection = factory(self.url)
            compat = PostgreSQLConnection(connection)
            try:
                # Psycopg starts the transaction before executing this statement.  SET
                # TRANSACTION must therefore be the first command so a server/DSN default
                # cannot weaken the serialized-writer contract below.
                connection.execute(_SET_TRANSACTION_ISOLATION_SQL)
                connection.execute(_WRITER_LOCK_SQL)
                for statement in _COMPATIBILITY_FUNCTIONS_SQL:
                    compat._execute_translated(statement, (), translate=False)
                compat.executescript(_BASE_SCHEMA_SQL)
                apply_migrations(cast(sqlite3.Connection, compat))
                row = compat.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
                version = int(row[0]) if row is not None and row[0] is not None else 0
                if version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"PostgreSQL schema migration stopped at {version}; "
                        f"expected {SCHEMA_VERSION}"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                connection.close()
                raise
            self._pg_connection = connection
            self._compat_connection = compat

    def close(self) -> None:
        with self._lock:
            if self._pg_connection is not None:
                self._pg_connection.close()
                self._pg_connection = None
                self._compat_connection = None

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[Any]:
        with self._lock:
            connection = self._require_postgresql_connection()
            raw = self._pg_connection
            if raw is None:  # pragma: no cover - kept adjacent to the invariant check
                raise RuntimeError("Database is not initialized")
            # This first execute lets Psycopg issue its implicit BEGIN, then pins the
            # transaction to the isolation level required by the compatibility layer.
            raw.execute(_SET_TRANSACTION_ISOLATION_SQL)
            if immediate:
                raw.execute(_WRITER_LOCK_SQL)
            try:
                yield connection
            except Exception:
                raw.rollback()
                raise
            else:
                raw.commit()

    def _require_postgresql_connection(self) -> PostgreSQLConnection:
        if self._compat_connection is None:
            raise RuntimeError("Database is not initialized")
        return self._compat_connection


def create_control_plane_database(url: str) -> SQLiteDatabase:
    """Select the explicit central persistence adapter from a validated URL."""

    if url.startswith("sqlite:///"):
        value = url.removeprefix("sqlite:///")
        if not value:
            raise ConfigurationError("SQLite database URL must include a path.")
        return SQLiteDatabase(Path(value).expanduser())
    if url.startswith(("postgresql://", "postgresql+psycopg://")):
        return PostgreSQLDatabase(url)
    raise ConfigurationError(
        "Control-plane database URL must use sqlite:///, postgresql://, or postgresql+psycopg://.",
        database_url_scheme=url.partition(":")[0],
    )


def _connect_psycopg(url: str) -> Any:
    global PSYCOPG_INTEGRITY_ERRORS

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - installed runtime dependency
        raise RuntimeError(
            "PostgreSQL control-plane storage requires the psycopg runtime dependency"
        ) from exc
    PSYCOPG_INTEGRITY_ERRORS = (psycopg.IntegrityError,)
    return psycopg.connect(url, autocommit=False, row_factory=dict_row)


def _normalize_postgresql_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgresql://"):
        return url
    raise ConfigurationError(
        "PostgreSQL database URL must use postgresql:// or postgresql+psycopg://.",
        database_url_scheme=url.partition(":")[0],
    )


def _translate_sql(query: str) -> str:
    translated = query
    insert_or_ignore = bool(
        re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", translated, flags=re.IGNORECASE)
    )
    translated = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT INTO",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s+COLLATE\s+NOCASE\b",
        r"LOWER(\1)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s+NOT\s+GLOB\s+'\*\[\^0-9a-f\]\*'",
        r"\1 !~ '[^0-9a-f]'",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"json_extract\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*,\s*'\$\.([A-Za-z0-9_.]+)'\s*\)",
        _postgresql_json_extract,
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bjson_valid\s*\(",
        "lab_platform_is_valid_json(",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bjson_type\s*\(",
        "lab_platform_json_kind(",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bjulianday\s*\(",
        "lab_platform_julianday(",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bMAX\(\s*0\s*,\s*COUNT\(\*\)\s*-\s*COUNT\(DISTINCT\s+agent_id\)\s*\)",
        "GREATEST(0, COUNT(*) - COUNT(DISTINCT agent_id))",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\s*\(\s*'now'\s*\)",
        "CURRENT_TIMESTAMP::text",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", translated, flags=re.IGNORECASE)
    if insert_or_ignore:
        translated = translated.rstrip().removesuffix(";") + " ON CONFLICT DO NOTHING"
    return _qmark_to_pyformat(translated)


def _postgresql_json_extract(match: re.Match[str]) -> str:
    column = match.group(1)
    path = ", ".join(repr(component) for component in match.group(2).split("."))
    return f"jsonb_extract_path_text({column}::jsonb, {path})"


def _qmark_to_pyformat(query: str) -> str:
    pieces: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(query):
        character = query[index]
        following = query[index + 1] if index + 1 < len(query) else ""
        if line_comment:
            pieces.append(character)
            if character in {"\n", "\r"}:
                line_comment = False
        elif block_comment:
            pieces.append(character)
            if character == "*" and following == "/":
                pieces.append(following)
                index += 1
                block_comment = False
        elif quote is not None:
            pieces.append(character)
            if character == quote:
                if following == quote:
                    pieces.append(following)
                    index += 1
                else:
                    quote = None
        elif character == "-" and following == "-":
            pieces.extend((character, following))
            index += 1
            line_comment = True
        elif character == "/" and following == "*":
            pieces.extend((character, following))
            index += 1
            block_comment = True
        elif character in {"'", '"'}:
            quote = character
            pieces.append(character)
        elif character == "?":
            pieces.append("?" if _is_postgresql_question_operator(query, index) else "%s")
        else:
            pieces.append(character)
        index += 1
    return "".join(pieces)


def _is_postgresql_question_operator(query: str, index: int) -> bool:
    following = query[index + 1] if index + 1 < len(query) else ""
    if following in {"|", "&"}:
        return True
    previous_non_space = next(
        (character for character in reversed(query[:index]) if not character.isspace()),
        "",
    )
    following_non_space = next(
        (character for character in query[index + 1 :] if not character.isspace()),
        "",
    )
    previous_is_operand = previous_non_space.isalnum() or previous_non_space in {"_", ")", "]"}
    following_is_json_key = following_non_space in {"'", '"', "$", "["}
    return previous_is_operand and following_is_json_key


def _requires_recoverable_savepoint(statement: str) -> bool:
    """Fence only inserts whose repositories recover after a uniqueness race.

    Other integrity failures leave the transaction aborted and immediately escape the
    repository's transaction context, which performs the ordinary full rollback. Keeping
    savepoints off bulk inventory and journal writes avoids two extra database round trips
    per statement.
    """

    normalized = " ".join(statement.split())
    return bool(
        re.match(
            r"^INSERT\s+INTO\s+(?:artifacts|ci_sessions)\s*(?:\(|$)",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _column_names(cursor: Any) -> tuple[str, ...]:
    description = getattr(cursor, "description", None) or ()
    names: list[str] = []
    for column in description:
        name = getattr(column, "name", None)
        if name is None and isinstance(column, Sequence) and column:
            name = column[0]
        names.append(str(name))
    return tuple(names)


def _row(row: Any, names: Sequence[str]) -> PostgreSQLRow | None:
    if row is None:
        return None
    return _row_required(row, names)


def _row_required(row: Any, names: Sequence[str]) -> PostgreSQLRow:
    if isinstance(row, PostgreSQLRow):
        return row
    if isinstance(row, Mapping):
        return PostgreSQLRow(tuple(str(key) for key in row), tuple(row.values()))
    values = tuple(row)
    resolved_names = tuple(names) or tuple(str(index) for index in range(len(values)))
    return PostgreSQLRow(resolved_names, values)


def _is_integrity_error(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    return (
        isinstance(exc, (sqlite3.IntegrityError, *PSYCOPG_INTEGRITY_ERRORS))
        or exc.__class__.__name__ in {"IntegrityError", "UniqueViolation", "ForeignKeyViolation"}
        or (isinstance(sqlstate, str) and sqlstate.startswith("23"))
    )


def _translate_integrity_error(exc: Exception) -> sqlite3.IntegrityError:
    diagnostic = getattr(exc, "diag", None)
    constraint_value = getattr(diagnostic, "constraint_name", None)
    constraint = constraint_value if isinstance(constraint_value, str) else None
    sqlite_constraint = (
        {
            "operations_one_active_per_bench": "operations.bench_id",
            "workflow_runs_one_active_per_bench": "workflow_runs.bench_id",
        }.get(constraint)
        if constraint is not None
        else None
    )
    if sqlite_constraint is not None:
        return sqlite3.IntegrityError(f"UNIQUE constraint failed: {sqlite_constraint}")
    context = f" [{constraint}]" if constraint else ""
    return sqlite3.IntegrityError(f"PostgreSQL integrity constraint failed{context}: {exc}")


__all__ = [
    "PostgreSQLConnection",
    "PostgreSQLCursor",
    "PostgreSQLDatabase",
    "PostgreSQLRow",
    "create_control_plane_database",
]
