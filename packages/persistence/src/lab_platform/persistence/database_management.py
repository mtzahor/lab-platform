from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from lab_platform.persistence.migrations import SCHEMA_VERSION
from lab_platform.persistence.postgresql import create_control_plane_database

MINIMUM_SUPPORTED_SCHEMA_VERSION = 11
ROLLBACK_COMPATIBILITY = "restore_backup_required"

SchemaState = Literal[
    "empty",
    "current",
    "upgrade_required",
    "too_old",
    "newer",
    "inconsistent",
]


class SchemaCompatibilityError(RuntimeError):
    """The connected database cannot be used or migrated safely by this application."""


@dataclass(frozen=True)
class SchemaStatus:
    backend: Literal["sqlite", "postgresql"]
    current_version: int
    target_version: int
    minimum_supported_version: int
    applied_versions: tuple[int, ...]
    state: SchemaState
    rollback_compatibility: str = ROLLBACK_COMPATIBILITY

    @property
    def ready(self) -> bool:
        return self.state == "current"

    @property
    def migration_allowed(self) -> bool:
        return self.state in {"empty", "current", "upgrade_required"}

    @property
    def message(self) -> str:
        if self.state == "empty":
            return f"database is empty; schema {self.target_version} must be installed"
        if self.state == "current":
            return f"database schema {self.current_version} is current"
        if self.state == "upgrade_required":
            return (
                f"database schema {self.current_version} must be migrated to {self.target_version}"
            )
        if self.state == "too_old":
            return (
                f"database schema {self.current_version} is older than the minimum supported "
                f"schema {self.minimum_supported_version}"
            )
        if self.state == "newer":
            return (
                f"database schema {self.current_version} is newer than application schema "
                f"{self.target_version}"
            )
        return "database migration history is incomplete or inconsistent"

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "minimum_supported_version": self.minimum_supported_version,
            "applied_versions": list(self.applied_versions),
            "state": self.state,
            "ready": self.ready,
            "migration_allowed": self.migration_allowed,
            "rollback_compatibility": self.rollback_compatibility,
            "message": self.message,
        }


def inspect_database_schema(
    url: str,
    *,
    postgresql_connect_factory: Callable[[str], Any] | None = None,
) -> SchemaStatus:
    """Inspect connectivity and migration history without applying schema changes."""

    if url.startswith("sqlite:///"):
        return _inspect_sqlite(url)
    if url.startswith(("postgresql://", "postgresql+psycopg://")):
        return _inspect_postgresql(url, connect_factory=postgresql_connect_factory)
    raise ValueError("database URL must use sqlite:///, postgresql://, or postgresql+psycopg://")


def require_current_schema(status: SchemaStatus) -> SchemaStatus:
    if not status.ready:
        raise SchemaCompatibilityError(status.message)
    return status


def migrate_database(
    url: str,
    *,
    allow_unsupported_source: bool = False,
) -> SchemaStatus:
    """Apply the existing forward migrations after a non-mutating compatibility preflight."""

    before = inspect_database_schema(url)
    if before.state in {"newer", "inconsistent"}:
        raise SchemaCompatibilityError(before.message)
    if before.state == "too_old" and not allow_unsupported_source:
        raise SchemaCompatibilityError(before.message)

    database = create_control_plane_database(url)
    try:
        database.initialize()
    finally:
        database.close()

    if url == "sqlite:///:memory:":
        return _status_from_versions("sqlite", tuple(range(2, SCHEMA_VERSION + 1)))
    return require_current_schema(inspect_database_schema(url))


def _inspect_sqlite(url: str) -> SchemaStatus:
    configured_path = url.removeprefix("sqlite:///")
    if not configured_path:
        raise ValueError("SQLite database URL must include a path")
    if configured_path == ":memory:":
        return _status_from_versions("sqlite", ())
    path = Path(configured_path).expanduser()
    if not path.exists():
        return _status_from_versions("sqlite", ())
    if not path.is_file():
        raise OSError(f"SQLite database path is not a regular file: {path}")

    connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if row is None:
            return _status_from_versions("sqlite", ())
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return _status_from_versions("sqlite", tuple(int(item[0]) for item in rows))
    finally:
        connection.close()


def _inspect_postgresql(
    url: str,
    *,
    connect_factory: Callable[[str], Any] | None,
) -> SchemaStatus:
    normalized_url = (
        "postgresql://" + url.removeprefix("postgresql+psycopg://")
        if url.startswith("postgresql+psycopg://")
        else url
    )
    connection = (
        connect_factory(normalized_url)
        if connect_factory is not None
        else _connect_postgresql(normalized_url)
    )
    try:
        exists_cursor = connection.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'schema_migrations'"
            ") AS schema_migrations_exists"
        )
        exists_row = exists_cursor.fetchone()
        if exists_row is None or not bool(_row_value(exists_row, "schema_migrations_exists", 0)):
            return _status_from_versions("postgresql", ())
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        versions = tuple(_integer_row_value(item, "version", 0) for item in rows)
        return _status_from_versions("postgresql", versions)
    finally:
        connection.close()


def _connect_postgresql(url: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - required runtime dependency
        raise RuntimeError("PostgreSQL diagnostics require the psycopg runtime dependency") from exc
    return psycopg.connect(url, autocommit=True)


def _integer_row_value(row: object, name: str, position: int) -> int:
    value = _row_value(row, name, position)
    if not isinstance(value, (str, bytes, bytearray, int)):
        raise RuntimeError(f"Database column {name} did not contain an integer")
    return int(value)


def _status_from_versions(
    backend: Literal["sqlite", "postgresql"],
    versions: Sequence[int],
) -> SchemaStatus:
    applied = tuple(versions)
    current = max(applied, default=0)
    expected = tuple(range(2, current + 1)) if current else ()
    if any(version < 2 for version in applied) or len(set(applied)) != len(applied):
        state: SchemaState = "inconsistent"
    elif applied != expected:
        state = "inconsistent"
    elif current == 0:
        state = "empty"
    elif current > SCHEMA_VERSION:
        state = "newer"
    elif current == SCHEMA_VERSION:
        state = "current"
    elif current < MINIMUM_SUPPORTED_SCHEMA_VERSION:
        state = "too_old"
    else:
        state = "upgrade_required"
    return SchemaStatus(
        backend=backend,
        current_version=current,
        target_version=SCHEMA_VERSION,
        minimum_supported_version=MINIMUM_SUPPORTED_SCHEMA_VERSION,
        applied_versions=applied,
        state=state,
    )


def _row_value(row: object, name: str, index: int) -> object:
    if isinstance(row, Mapping):
        return row[name]
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[index]
    raise TypeError("database driver returned an unsupported row shape")


__all__ = [
    "MINIMUM_SUPPORTED_SCHEMA_VERSION",
    "ROLLBACK_COMPATIBILITY",
    "SchemaCompatibilityError",
    "SchemaState",
    "SchemaStatus",
    "inspect_database_schema",
    "migrate_database",
    "require_current_schema",
]
