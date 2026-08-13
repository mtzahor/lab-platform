from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.core.errors import ConfigurationError
from lab_platform.persistence.database import SCHEMA_VERSION, SQLiteDatabase
from lab_platform.persistence.postgresql import (
    PostgreSQLDatabase,
    create_control_plane_database,
)

POSTGRES_URL = "postgresql://lab:secret@database.example:5432/lab_platform"


@dataclass(frozen=True)
class ExecutedStatement:
    sql: str
    parameters: object | None


class FakeIntegrityError(Exception):
    """Psycopg-shaped SQLSTATE error used without a live PostgreSQL server."""

    sqlstate = "23505"


class FakeInFailedTransactionError(RuntimeError):
    pass


class FakePsycopgCursor:
    def __init__(self, connection: FakePsycopgConnection) -> None:
        self._connection = connection
        self._rows: list[object] = []
        self.rowcount = -1
        self.lastrowid: int | None = None
        self.closed = False

    def execute(
        self,
        query: object,
        parameters: object | None = None,
        **_kwargs: object,
    ) -> FakePsycopgCursor:
        sql = str(query)
        self._connection.before_execute(sql)
        self._connection.statements.append(ExecutedStatement(sql, parameters))
        self._rows = self._connection.rows_for(sql)
        self.rowcount = self._connection.rowcount_for(sql, self._rows)
        return self

    def executemany(
        self,
        query: object,
        parameters_seq: Sequence[object],
        **kwargs: object,
    ) -> FakePsycopgCursor:
        for parameters in parameters_seq:
            self.execute(query, parameters, **kwargs)
        return self

    def fetchone(self) -> object | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[object]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self.closed = True

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)

    def __enter__(self) -> FakePsycopgCursor:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class FakePsycopgConnection:
    def __init__(self) -> None:
        self.statements: list[ExecutedStatement] = []
        self.results: dict[str, list[object]] = {}
        self.rowcounts: dict[str, int] = {}
        self.failures: dict[str, Callable[[], BaseException]] = {}
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.aborted = False
        self.autocommit = False
        self.row_factory: object | None = None

    def cursor(self, *args: object, **kwargs: object) -> FakePsycopgCursor:
        if args:
            self.row_factory = args[0]
        elif "row_factory" in kwargs:
            self.row_factory = kwargs["row_factory"]
        return FakePsycopgCursor(self)

    def execute(
        self,
        query: object,
        parameters: object | None = None,
        **kwargs: object,
    ) -> FakePsycopgCursor:
        return self.cursor().execute(query, parameters, **kwargs)

    def commit(self) -> None:
        if self.aborted:
            raise FakeInFailedTransactionError("current transaction is aborted")
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1
        self.aborted = False

    def close(self) -> None:
        self.close_count += 1

    def before_execute(self, sql: str) -> None:
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("ROLLBACK TO"):
            self.aborted = False
            return
        if self.aborted:
            raise FakeInFailedTransactionError("current transaction is aborted")
        for fragment, failure in self.failures.items():
            if fragment in sql:
                self.aborted = True
                raise failure()

    def rows_for(self, sql: str) -> list[object]:
        for fragment, rows in self.results.items():
            if fragment in sql:
                return list(rows)
        return []

    def rowcount_for(self, sql: str, rows: Sequence[object]) -> int:
        for fragment, rowcount in self.rowcounts.items():
            if fragment in sql:
                return rowcount
        return len(rows) if sql.lstrip().upper().startswith("SELECT") else 1

    def clear_observations(self) -> None:
        self.statements.clear()
        self.commit_count = 0
        self.rollback_count = 0


class FakeConnectFactory:
    def __init__(self, connection: FakePsycopgConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakePsycopgConnection:
        self.calls.append((args, kwargs))
        return self.connection


def initialized_database(
    connection: FakePsycopgConnection | None = None,
) -> tuple[PostgreSQLDatabase, FakePsycopgConnection, FakeConnectFactory]:
    raw = connection or FakePsycopgConnection()
    raw.results.setdefault(
        "SELECT MAX(version) AS version FROM schema_migrations",
        [{"version": SCHEMA_VERSION}],
    )
    factory = FakeConnectFactory(raw)
    database = PostgreSQLDatabase(POSTGRES_URL, connect_factory=factory)
    database.initialize()
    return database, raw, factory


def executed_sql(connection: FakePsycopgConnection) -> str:
    return "\n".join(statement.sql for statement in connection.statements)


def test_database_factory_selects_sqlite_and_postgresql_adapters(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "control-plane.db"
    sqlite_database = create_control_plane_database(f"sqlite:///{sqlite_path}")
    postgres_database = create_control_plane_database(POSTGRES_URL)
    sqlalchemy_spelling = create_control_plane_database(
        "postgresql+psycopg://lab@database.example/lab_platform?sslmode=require"
    )

    assert type(sqlite_database) is SQLiteDatabase
    assert sqlite_database.path == sqlite_path
    assert isinstance(postgres_database, PostgreSQLDatabase)
    assert isinstance(sqlalchemy_spelling, PostgreSQLDatabase)

    with pytest.raises(ConfigurationError, match="database URL"):
        create_control_plane_database("mysql://database.example/lab_platform")


def test_initialize_connects_with_the_url_and_applies_postgresql_migrations() -> None:
    database, raw, factory = initialized_database()

    assert len(factory.calls) == 1
    args, kwargs = factory.calls[0]
    assert args and args[0] == POSTGRES_URL or kwargs.get("conninfo") == POSTGRES_URL

    sql = executed_sql(raw)
    for table in (
        "schema_migrations",
        "agents",
        "agent_credentials",
        "agent_connections",
        "global_benches",
        "remote_commands",
        "distributed_operations",
        "reservation_leases",
        "protocol_message_journal",
        "artifact_transfers",
        "agent_timelines",
    ):
        assert table in sql
    assert str(SCHEMA_VERSION) in sql
    assert "pg_advisory_xact_lock" in sql

    forbidden_sqlite_syntax = (
        "PRAGMA ",
        "sqlite_master",
        "AUTOINCREMENT",
        "datetime('now')",
        "json_valid(",
        "json_type(",
        " GLOB ",
        "BEGIN IMMEDIATE",
    )
    assert not any(fragment.casefold() in sql.casefold() for fragment in forbidden_sqlite_syntax)

    database.close()
    assert raw.close_count == 1


def test_phase10_migration_drops_foreign_keys_before_parent_unique_constraints() -> None:
    database, raw, _factory = initialized_database()

    phase10 = next(statement.sql for statement in raw.statements if "DO $phase10$" in statement.sql)
    foreign_keys = phase10.index("AND contype = 'f'")
    unique_constraints = phase10.index("AND contype = 'u'")

    assert foreign_keys < unique_constraints
    assert "CASCADE" not in phase10
    database.close()


def test_transaction_translates_placeholders_and_preserves_literals_and_parameters() -> None:
    database, raw, _factory = initialized_database()
    raw.clear_observations()

    with database.transaction(immediate=True) as connection:
        connection.execute(
            "SELECT '? literal', payload_json FROM remote_commands "
            "WHERE id = ? AND payload_json::jsonb ? 'capability' -- ? comment\n",
            ["command-1"],
        )

    application_sql = executed_sql(raw)
    assert "id = %s" in application_sql
    assert "'? literal'" in application_sql
    assert "::jsonb ? 'capability'" in application_sql
    assert "-- ? comment" in application_sql
    assert raw.statements[-1].parameters == ("command-1",)
    assert "BEGIN IMMEDIATE" not in application_sql
    assert "SET TRANSACTION ISOLATION LEVEL READ COMMITTED" in application_sql
    assert "PG_ADVISORY_XACT_LOCK" in application_sql.upper()
    statements = [statement.sql for statement in raw.statements]
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert statements[1] == "SELECT pg_advisory_xact_lock(1279349840, 5)"
    assert raw.commit_count == 1
    assert raw.rollback_count == 0


def test_transaction_rolls_back_on_body_error_and_can_be_reused() -> None:
    database, raw, _factory = initialized_database()
    raw.clear_observations()

    with pytest.raises(RuntimeError, match="boom"), database.transaction() as connection:
        connection.execute("UPDATE agents SET status = ? WHERE id = ?", ("OFFLINE", "a-1"))
        raise RuntimeError("boom")

    assert raw.commit_count == 0
    assert raw.rollback_count == 1

    with database.transaction() as connection:
        connection.execute("SELECT id FROM agents WHERE id = ?", ("a-1",))
    assert raw.commit_count == 1


def test_bulk_mutations_do_not_pay_per_statement_savepoint_round_trips() -> None:
    database, raw, _factory = initialized_database()
    raw.clear_observations()

    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE global_benches SET status = ? WHERE agent_id = ?",
            ("OFFLINE", "agent-1"),
        )
        connection.execute(
            "INSERT INTO bench_snapshots "
            "(id, agent_id, boot_id, generated_at, received_at, bench_count, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("snapshot-1", "agent-1", "boot-1", "now", "now", 0, "[]"),
        )

    assert "SAVEPOINT" not in executed_sql(raw).upper()


def test_cursor_rows_support_mapping_and_positional_access_and_preserve_rowcount() -> None:
    raw = FakePsycopgConnection()
    raw.results["SELECT id, status FROM agents"] = [{"id": "agent-1", "status": "ONLINE"}]
    raw.results["SELECT COUNT(*) AS count FROM agents"] = [{"count": 3}]
    raw.rowcounts["UPDATE agents"] = 1
    database, raw, _factory = initialized_database(raw)
    raw.clear_observations()

    with database.transaction() as connection:
        row = connection.execute("SELECT id, status FROM agents").fetchone()
        scalar = connection.execute("SELECT COUNT(*) AS count FROM agents").fetchone()
        updated = connection.execute(
            "UPDATE agents SET status = ? WHERE id = ?", ["OFFLINE", "agent-1"]
        )

    assert row is not None
    assert cast(Mapping[str, object], row)["id"] == "agent-1"
    assert cast(Sequence[object], row)[0] == "agent-1"
    assert scalar is not None
    assert cast(Mapping[str, object], scalar)["count"] == 3
    assert cast(Sequence[object], scalar)[0] == 3
    assert updated.rowcount == 1


def test_sql_compatibility_rewrites_insert_ignore_and_json_extract() -> None:
    database, raw, _factory = initialized_database()
    raw.clear_observations()

    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO events (id, payload) VALUES (?, ?)",
            ("event-1", "{}"),
        )
        connection.execute(
            "SELECT json_extract(record_json, "
            "'$.reservation.metadata.reservation_lifecycle') AS lifecycle "
            "FROM coordinated_reservation_leases WHERE reservation_id = ?",
            ("reservation-1",),
        )
        connection.execute(
            "UPDATE remote_artifacts SET uploaded_at = ? WHERE id = ? AND uploaded_at IS ?",
            ("2026-08-01T10:00:00+00:00", "transfer-1", None),
        )

    sql = executed_sql(raw)
    assert "INSERT OR IGNORE" not in sql.upper()
    assert "ON CONFLICT DO NOTHING" in sql.upper()
    assert "json_extract" not in sql.casefold()
    assert "::jsonb" in sql
    assert "reservation_lifecycle" in sql
    assert "uploaded_at IS NOT DISTINCT FROM %s" in sql


def test_metrics_sql_uses_postgresql_scalar_and_datetime_compatibility() -> None:
    database, raw, _factory = initialized_database()
    raw.clear_observations()

    with database.transaction() as connection:
        connection.execute(
            "SELECT MAX(0, COUNT(*) - COUNT(DISTINCT connection.agent_id)) "
            "FROM agent_connections AS connection JOIN agents AS agent "
            "ON agent.id = connection.agent_id"
        )
        connection.execute(
            "SELECT COALESCE(MAX((julianday('now') - julianday(last_heartbeat_at)) "
            "* 86400.0), 0) FROM agent_connections WHERE disconnected_at IS NULL"
        )

    sql = executed_sql(raw)
    assert "GREATEST(0, COUNT(*) - COUNT(DISTINCT connection.agent_id))" in sql
    assert "MAX(0, COUNT(*)" not in sql
    assert "lab_platform_julianday('now')" in sql
    assert "lab_platform_julianday(last_heartbeat_at)" in sql


def test_integrity_errors_are_translated_and_leave_transaction_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adapter intentionally exposes one backend-neutral sqlite3.IntegrityError to
    # the existing repositories. Patch its driver error tuple so this unit contract
    # does not need a running server or a driver-generated diagnostic object.
    import lab_platform.persistence.postgresql as postgresql

    patched = False
    for name in ("PSYCOPG_INTEGRITY_ERRORS", "_PSYCOPG_INTEGRITY_ERRORS"):
        if hasattr(postgresql, name):
            monkeypatch.setattr(postgresql, name, (FakeIntegrityError,))
            patched = True
    if hasattr(postgresql, "PsycopgIntegrityError"):
        monkeypatch.setattr(postgresql, "PsycopgIntegrityError", FakeIntegrityError)
        patched = True
    if hasattr(postgresql, "IntegrityError"):
        monkeypatch.setattr(postgresql, "IntegrityError", FakeIntegrityError)
        patched = True
    assert patched, "PostgreSQL adapter must expose its driver integrity error for translation"

    raw = FakePsycopgConnection()
    raw.failures["INSERT INTO artifacts"] = lambda: FakeIntegrityError("duplicate key")
    raw.results["SELECT id FROM artifacts"] = [{"id": "artifact-1"}]
    database, raw, _factory = initialized_database(raw)
    raw.clear_observations()

    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="duplicate key"):
            connection.execute(
                "INSERT INTO artifacts (id, owner_id) VALUES (?, ?)",
                ("artifact-2", "owner-1"),
            )
        row = connection.execute(
            "SELECT id FROM artifacts WHERE owner_id = ?", ("owner-1",)
        ).fetchone()

    assert row is not None
    assert cast(Mapping[str, object], row)["id"] == "artifact-1"
    assert raw.rollback_count == 0
    assert raw.commit_count == 1
    sql = executed_sql(raw).upper()
    assert "SAVEPOINT" in sql
    assert "ROLLBACK TO" in sql
    assert "RELEASE SAVEPOINT" in sql


@pytest.mark.skipif(
    not os.environ.get("LAB_PLATFORM_TEST_POSTGRESQL_URL"),
    reason="requires an isolated PostgreSQL test database",
)
def test_postgresql_optional_live_smoke(tmp_path: Path) -> None:
    url = os.environ["LAB_PLATFORM_TEST_POSTGRESQL_URL"]

    async def scenario() -> None:
        runtime = ControlPlaneRuntime(
            ControlPlaneConfig.model_validate(
                {
                    "control_plane": {
                        "host": "127.0.0.1",
                        "port": 8443,
                        "public_url": "http://127.0.0.1:8443",
                    },
                    "database": {"url": url},
                    "artifacts": {"directory": tmp_path / "artifacts"},
                    "development": {"allow_insecure_agent_transport": True},
                }
            )
        )
        assert isinstance(runtime.database, PostgreSQLDatabase)
        await runtime.start()
        try:
            issued = await runtime.enrollment.issue_token(
                name=f"postgresql-smoke-{uuid4().hex}",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                allowed_labels={"suite": "postgresql"},
                allow_internal_authorisation=True,
            )
            enrolled = await runtime.enrollment.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=uuid4(),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
                location="postgresql-ci",
            )
            authenticated = await runtime.enrollment.authenticate(
                enrolled.agent.id,
                enrolled.plaintext.get_secret_value(),
            )
            assert authenticated.agent.id == enrolled.agent.id
            assert enrolled.agent in await runtime.enrollment.list_agents(
                allow_internal_authorisation=True
            )

            metrics = await runtime.metrics(allow_internal_authorisation=True)
            assert {
                "agents_online",
                "agents_offline",
                "agent_reconnects_total",
                "agent_heartbeat_lag_seconds",
                "bench_inventory_total",
                "ci_sessions_running",
            } <= metrics.keys()
            assert all(isinstance(value, (int, float)) for value in metrics.values())
            assert metrics["agents_offline"] >= 1
            with runtime.database.transaction() as connection:
                row = connection.execute("SELECT 1 AS value").fetchone()
                version_row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
            assert row is not None
            assert cast(Sequence[object], row)[0] == 1
            assert version_row is not None
            assert cast(Mapping[str, Any], version_row)["version"] == SCHEMA_VERSION
        finally:
            await runtime.stop()

    asyncio.run(scenario())
