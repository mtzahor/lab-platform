from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from lab_platform.persistence import DEFAULT_ORGANISATION_ID, SCHEMA_VERSION, SQLiteDatabase
from lab_platform.persistence.migrations import apply_migrations

IDENTITY_TABLES = {
    "organisations",
    "users",
    "password_credentials",
    "organisation_memberships",
    "teams",
    "team_memberships",
    "service_accounts",
    "user_sessions",
    "api_credentials",
    "role_assignments",
    "authorisation_snapshots",
    "bench_access_policies",
    "workflow_access_policies",
    "audit_events",
    "login_attempts",
}

CENTRAL_ORGANISATION_TABLES = {
    "reservations",
    "reservation_queue",
    "operations",
    "events",
    "firmware_artifacts",
    "operation_artifacts",
    "operation_locks",
    "workflows",
    "workflow_runs",
    "workflow_step_results",
    "api_tokens",
    "ci_sessions",
    "ci_cleanup_results",
    "artifacts",
    "backend_registrations",
    "bench_catalog",
    "agents",
    "agent_enrollment_tokens",
    "agent_credentials",
    "agent_connections",
    "global_benches",
    "bench_snapshots",
    "remote_commands",
    "remote_command_attempts",
    "distributed_operations",
    "reservation_leases",
    "coordinated_reservation_leases",
    "reservation_lease_mutations",
    "reconciliation_reports",
    "reconciliation_report_claims",
    "protocol_message_journal",
    "remote_artifacts",
    "artifact_transfers",
    "artifact_transfer_attempts",
    "agent_timelines",
    "distributed_ci_workflows",
}


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_schema_v10_installs_identity_tables_default_organisation_and_scope_columns(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "phase6.db")
    with database.transaction() as connection:
        assert SCHEMA_VERSION == 11
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables >= IDENTITY_TABLES
        for table in CENTRAL_ORGANISATION_TABLES:
            assert "organisation_id" in _columns(connection, table), table

        default = connection.execute(
            "SELECT id, slug, name, status FROM organisations WHERE id = ?",
            (DEFAULT_ORGANISATION_ID,),
        ).fetchone()
        assert default is not None
        assert tuple(default) == (
            DEFAULT_ORGANISATION_ID,
            "default",
            "Default Organisation",
            "ACTIVE",
        )
        assert {"owner_principal_id", "owner_principal_type"} <= _columns(
            connection,
            "reservations",
        )
        assert {
            "actor_context_json",
            "authorisation_snapshot_id",
        } <= _columns(connection, "remote_commands")
        assert {
            "cancel_actor_context_json",
            "cancel_authorisation_snapshot_id",
        } <= _columns(connection, "ci_sessions")
    database.close()


def test_existing_v10_database_repairs_ci_cancel_evidence_schema_unconditionally(
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing-v10-ci-cancel.db"
    database = _database(path)
    database.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE ci_sessions DROP COLUMN cancel_actor_context_json")
        connection.execute("ALTER TABLE ci_sessions DROP COLUMN cancel_authorisation_snapshot_id")
        connection.executescript(
            """
            ALTER TABLE authorisation_snapshots RENAME TO authorisation_snapshots_new_shape;
            CREATE TABLE authorisation_snapshots (
                id TEXT PRIMARY KEY,
                organisation_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                resource_type TEXT NOT NULL CHECK (
                    resource_type IN ('ORGANISATION', 'AGENT', 'BENCH', 'WORKFLOW')
                ),
                resource_id TEXT NOT NULL,
                granted_by_assignments_json TEXT NOT NULL DEFAULT '[]' CHECK (
                    json_valid(granted_by_assignments_json)
                    AND json_type(granted_by_assignments_json) = 'array'
                ),
                evaluated_at TEXT NOT NULL,
                FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                    ON UPDATE CASCADE ON DELETE RESTRICT
            );
            INSERT INTO authorisation_snapshots
            SELECT * FROM authorisation_snapshots_new_shape;
            DROP TABLE authorisation_snapshots_new_shape;
            """
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        old_definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'authorisation_snapshots'"
        ).fetchone()
        assert old_definition is not None
        assert "CI_SESSION" not in str(old_definition[0])

    repaired = _database(path)
    with repaired.transaction() as connection:
        assert {
            "cancel_actor_context_json",
            "cancel_authorisation_snapshot_id",
        } <= _columns(connection, "ci_sessions")
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'authorisation_snapshots'"
        ).fetchone()
        assert definition is not None
        assert "CI_SESSION" in str(definition[0])
    repaired.close()


def test_phase6_migration_is_idempotent_and_legacy_inserts_receive_default_scope(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "idempotent.db")
    with database.transaction(immediate=True) as connection:
        apply_migrations(connection)
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO reservations (id, bench_id, owner, created_at, status) "
            "VALUES ('legacy-reservation', 'legacy/bench', 'legacy-owner', "
            "'2026-08-02T12:00:00+00:00', 'active')"
        )
        scope = connection.execute(
            "SELECT organisation_id, owner_principal_id, owner_principal_type "
            "FROM reservations WHERE id = 'legacy-reservation'"
        ).fetchone()
        assert scope is not None
        assert tuple(scope) == (DEFAULT_ORGANISATION_ID, None, None)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM organisations WHERE id = ?",
                (DEFAULT_ORGANISATION_ID,),
            ).fetchone()[0]
            == 1
        )
    database.close()


def test_v8_upgrade_preserves_legacy_rows_and_repairs_interrupted_phase6_objects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade-v8.db"
    database = _database(path)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO api_tokens "
            "(id, name, token_hash, owner, scopes_json, created_at) "
            "VALUES ('legacy-token', 'legacy', ?, 'legacy-owner', '[\"agents:read\"]', ?)",
            ("a" * 64, "2026-08-02T12:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO reservations (id, bench_id, owner, created_at, status) "
            "VALUES ('legacy-reservation', 'legacy/bench', 'legacy-owner', "
            "'2026-08-02T12:00:00+00:00', 'active')"
        )
    database.close()

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP INDEX reservations_organisation;
            DROP INDEX reservations_idempotency;
            DROP INDEX remote_commands_organisation;
            ALTER TABLE reservations DROP COLUMN owner_principal_type;
            ALTER TABLE reservations DROP COLUMN owner_principal_id;
            ALTER TABLE reservations DROP COLUMN organisation_id;
            ALTER TABLE remote_commands DROP COLUMN authorisation_snapshot_id;
            ALTER TABLE remote_commands DROP COLUMN actor_context_json;
            ALTER TABLE remote_commands DROP COLUMN organisation_id;
            DROP TABLE team_memberships;
            DROP TABLE organisation_memberships;
            DROP TABLE password_credentials;
            DROP TABLE user_sessions;
            DROP TABLE api_credentials;
            DROP TABLE role_assignments;
            DROP TABLE authorisation_snapshots;
            DROP TABLE bench_access_policies;
            DROP TABLE workflow_access_policies;
            DROP TABLE audit_events;
            DROP TABLE login_attempts;
            DROP TABLE teams;
            DROP TABLE users;
            DROP TABLE service_accounts;
            DROP TABLE organisations;
            DELETE FROM schema_migrations WHERE version >= 9;
            """
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 8

    upgraded = _database(path)
    with upgraded.transaction() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        legacy_token = connection.execute(
            "SELECT name, owner, scopes_json FROM api_tokens WHERE id = 'legacy-token'"
        ).fetchone()
        assert legacy_token is not None
        assert tuple(legacy_token) == ("legacy", "legacy-owner", '["agents:read"]')
        legacy_reservation = connection.execute(
            "SELECT owner, organisation_id, owner_principal_id, owner_principal_type "
            "FROM reservations WHERE id = 'legacy-reservation'"
        ).fetchone()
        assert legacy_reservation is not None
        assert tuple(legacy_reservation) == (
            "legacy-owner",
            DEFAULT_ORGANISATION_ID,
            None,
            None,
        )
        assert {
            "actor_context_json",
            "authorisation_snapshot_id",
        } <= _columns(connection, "remote_commands")
    upgraded.close()
