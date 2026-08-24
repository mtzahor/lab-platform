from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from lab_platform.persistence import SCHEMA_VERSION
from lab_platform.persistence.database_management import (
    MINIMUM_SUPPORTED_SCHEMA_VERSION,
    SchemaCompatibilityError,
    SchemaStatus,
    _integer_row_value,
    _row_value,
    _status_from_versions,
    inspect_database_schema,
    require_current_schema,
)


@pytest.mark.parametrize(
    ("state", "message_fragment", "ready", "migration_allowed"),
    [
        ("empty", "must be installed", False, True),
        ("current", "is current", True, True),
        ("upgrade_required", "must be migrated", False, True),
        ("too_old", "older than", False, False),
        ("newer", "newer than", False, False),
        ("inconsistent", "incomplete or inconsistent", False, False),
    ],
)
def test_schema_status_describes_each_operator_state(
    state: str,
    message_fragment: str,
    ready: bool,
    migration_allowed: bool,
) -> None:
    status = SchemaStatus(
        backend="sqlite",
        current_version=SCHEMA_VERSION,
        target_version=SCHEMA_VERSION,
        minimum_supported_version=MINIMUM_SUPPORTED_SCHEMA_VERSION,
        applied_versions=tuple(range(2, SCHEMA_VERSION + 1)),
        state=state,  # type: ignore[arg-type]
    )

    assert message_fragment in status.message
    assert status.ready is ready
    assert status.migration_allowed is migration_allowed
    assert status.as_dict()["rollback_compatibility"] == "restore_backup_required"
    if ready:
        assert require_current_schema(status) is status
    else:
        with pytest.raises(SchemaCompatibilityError, match=message_fragment):
            require_current_schema(status)


@pytest.mark.parametrize(
    ("versions", "state"),
    [
        ((), "empty"),
        ((1,), "inconsistent"),
        ((2, 2), "inconsistent"),
        ((2, 4), "inconsistent"),
        (tuple(range(2, MINIMUM_SUPPORTED_SCHEMA_VERSION)), "too_old"),
        (tuple(range(2, SCHEMA_VERSION)), "upgrade_required"),
        (tuple(range(2, SCHEMA_VERSION + 1)), "current"),
        (tuple(range(2, SCHEMA_VERSION + 2)), "newer"),
    ],
)
def test_schema_history_classification(versions: tuple[int, ...], state: str) -> None:
    assert _status_from_versions("sqlite", versions).state == state


def test_sqlite_schema_inspection_handles_empty_invalid_and_inconsistent_targets(
    tmp_path: Path,
) -> None:
    assert inspect_database_schema("sqlite:///:memory:").state == "empty"
    assert inspect_database_schema(f"sqlite:///{tmp_path / 'missing.db'}").state == "empty"
    with pytest.raises(ValueError, match="must include a path"):
        inspect_database_schema("sqlite:///")
    with pytest.raises(ValueError, match="database URL"):
        inspect_database_schema("mysql://database/lab")

    directory = tmp_path / "directory.db"
    directory.mkdir()
    with pytest.raises(OSError, match="not a regular file"):
        inspect_database_schema(f"sqlite:///{directory}")

    without_history = tmp_path / "without-history.db"
    sqlite3.connect(without_history).close()
    assert inspect_database_schema(f"sqlite:///{without_history}").state == "empty"

    partial = tmp_path / "partial.db"
    connection = sqlite3.connect(partial)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'now')",
            ((2,), (4,)),
        )
        connection.commit()
    finally:
        connection.close()
    inspected = inspect_database_schema(f"sqlite:///{partial}")
    assert inspected.state == "inconsistent"
    assert inspected.applied_versions == (2, 4)


class _Cursor:
    def __init__(self, *, one: object = None, all_rows: list[object] | None = None) -> None:
        self._one = one
        self._all = all_rows or []

    def fetchone(self) -> object:
        return self._one

    def fetchall(self) -> list[object]:
        return self._all


class _PostgresConnection:
    def __init__(self, exists: object, rows: list[object] | None = None) -> None:
        self.exists = exists
        self.rows = rows or []
        self.closed = False

    def execute(self, query: str) -> _Cursor:
        if "information_schema.tables" in query:
            return _Cursor(one=self.exists)
        return _Cursor(all_rows=self.rows)

    def close(self) -> None:
        self.closed = True


def test_postgresql_inspection_normalizes_driver_url_and_row_shapes() -> None:
    observed: list[str] = []
    connection = _PostgresConnection(
        {"schema_migrations_exists": True},
        [{"version": version} for version in range(2, SCHEMA_VERSION + 1)],
    )

    def connect(url: str) -> _PostgresConnection:
        observed.append(url)
        return connection

    status = inspect_database_schema(
        "postgresql+psycopg://lab@database/lab",
        postgresql_connect_factory=connect,
    )

    assert status.state == "current"
    assert observed == ["postgresql://lab@database/lab"]
    assert connection.closed is True

    missing = _PostgresConnection((False,))
    status = inspect_database_schema(
        "postgresql://lab@database/lab",
        postgresql_connect_factory=lambda _url: missing,
    )
    assert status.state == "empty"
    assert missing.closed is True

    no_result = _PostgresConnection(None)
    assert (
        inspect_database_schema(
            "postgresql://lab@database/lab",
            postgresql_connect_factory=lambda _url: no_result,
        ).state
        == "empty"
    )


def test_database_row_helpers_reject_unsupported_driver_results() -> None:
    assert _row_value({"version": "12"}, "version", 0) == "12"
    assert _row_value((12,), "version", 0) == 12
    assert _integer_row_value((b"12",), "version", 0) == 12
    with pytest.raises(RuntimeError, match="did not contain an integer"):
        _integer_row_value((object(),), "version", 0)
    with pytest.raises(TypeError, match="unsupported row shape"):
        _row_value(object(), "version", 0)


def test_postgresql_inspection_closes_connection_when_driver_rows_are_invalid() -> None:
    connection = _PostgresConnection((True,), [object()])
    with pytest.raises(TypeError, match="unsupported row shape"):
        inspect_database_schema(
            "postgresql://lab@database/lab",
            postgresql_connect_factory=lambda _url: connection,
        )
    assert connection.closed is True
