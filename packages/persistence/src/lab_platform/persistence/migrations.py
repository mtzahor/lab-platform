from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 4


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Upgrade a Phase 1/2 database to the latest Phase 3 schema."""

    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, datetime('now'))"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    if 3 not in applied:
        _add_reservation_columns(connection)
        _add_event_columns(connection)
        _create_phase3_tables(connection)
        _create_phase3_indexes_and_triggers(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, datetime('now'))"
        )
    if 4 not in applied:
        _create_catalog_tables(connection)
        _create_catalog_indexes(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (4, datetime('now'))"
        )
    # Kept idempotent and unconditional so databases created by early Phase 3
    # builds also receive the workflow portion of the same schema version.
    from lab_platform.persistence.workflows import WORKFLOW_SCHEMA_SQL

    connection.executescript(WORKFLOW_SCHEMA_SQL)
    _create_queue_transition_trigger(connection)


def _create_catalog_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS backend_registrations (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bench_catalog (
            id TEXT PRIMARY KEY,
            backend_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_type TEXT,
            online INTEGER NOT NULL CHECK (online IN (0, 1)),
            health TEXT NOT NULL CHECK (health IN ('healthy', 'warning', 'unhealthy')),
            capabilities_json TEXT NOT NULL DEFAULT '[]'
                CHECK (
                    json_valid(capabilities_json)
                    AND json_type(capabilities_json) = 'array'
                ),
            labels_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(labels_json) AND json_type(labels_json) = 'object'),
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_name TEXT,
            metadata_target_type TEXT,
            FOREIGN KEY (backend_id) REFERENCES backend_registrations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );
        """
    )


def _create_catalog_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS bench_catalog_backend
            ON bench_catalog(backend_id, id);
        CREATE INDEX IF NOT EXISTS bench_catalog_availability
            ON bench_catalog(online, health, target_type, id);
        CREATE INDEX IF NOT EXISTS bench_catalog_updated
            ON bench_catalog(updated_at DESC);
        """
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _add_reservation_columns(connection: sqlite3.Connection) -> None:
    columns = {
        "requested_at": "TEXT",
        "starts_at": "TEXT",
        "ends_at": "TEXT",
        "activated_at": "TEXT",
        "expired_at": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'api'",
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
        "idempotency_key": "TEXT",
        "release_pending": "INTEGER NOT NULL DEFAULT 0 CHECK (release_pending IN (0, 1))",
    }
    for name, declaration in columns.items():
        _add_column(connection, "reservations", name, declaration)
    connection.execute(
        "UPDATE reservations SET requested_at = created_at, starts_at = created_at, "
        "activated_at = CASE WHEN status = 'active' THEN created_at ELSE activated_at END "
        "WHERE requested_at IS NULL OR starts_at IS NULL"
    )


def _add_event_columns(connection: sqlite3.Connection) -> None:
    _add_column(connection, "events", "reservation_id", "TEXT")
    _add_column(connection, "events", "deduplication_key", "TEXT")


def _create_phase3_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS reservation_queue (
            id TEXT PRIMARY KEY,
            bench_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            requested_duration_seconds INTEGER NOT NULL
                CHECK (requested_duration_seconds > 0),
            status TEXT NOT NULL
                CHECK (status IN ('waiting', 'promoted', 'cancelled', 'expired')),
            created_at TEXT NOT NULL,
            promoted_at TEXT,
            cancelled_at TEXT,
            idempotency_key TEXT
        );
        CREATE TABLE IF NOT EXISTS operation_locks (
            bench_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            acquired_at TEXT NOT NULL,
            expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS recovery_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            report TEXT NOT NULL DEFAULT '{}'
        );
        """
    )


def _create_phase3_indexes_and_triggers(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP INDEX IF EXISTS reservations_one_active_per_bench;
        CREATE UNIQUE INDEX IF NOT EXISTS reservations_one_active_per_bench
            ON reservations(bench_id)
            WHERE status IN ('active', 'expired_pending_operation');
        CREATE UNIQUE INDEX IF NOT EXISTS reservations_idempotency
            ON reservations(bench_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS reservations_schedule
            ON reservations(bench_id, starts_at, ends_at, status);

        CREATE UNIQUE INDEX IF NOT EXISTS queue_idempotency
            ON reservation_queue(bench_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS queue_waiting_fifo
            ON reservation_queue(bench_id, created_at, id)
            WHERE status = 'waiting';
        CREATE UNIQUE INDEX IF NOT EXISTS events_deduplication
            ON events(deduplication_key)
            WHERE deduplication_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS events_reservation
            ON events(reservation_id, timestamp DESC);

        DROP TRIGGER IF EXISTS reservations_validate_insert;
        CREATE TRIGGER reservations_validate_insert
        BEFORE INSERT ON reservations
        WHEN NEW.starts_at IS NOT NULL AND NEW.ends_at IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN NEW.status NOT IN (
                    'queued', 'scheduled', 'active', 'released', 'expired',
                    'cancelled', 'rejected', 'expired_pending_operation'
                )
                THEN RAISE(ABORT, 'reservation_invalid_status')
            END;
            SELECT CASE
                WHEN NEW.ends_at <= NEW.starts_at
                THEN RAISE(ABORT, 'reservation_invalid_time')
            END;
            SELECT CASE
                WHEN NEW.status IN ('scheduled', 'active', 'expired_pending_operation')
                 AND EXISTS (
                    SELECT 1 FROM reservations existing
                    WHERE existing.bench_id = NEW.bench_id
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
                 )
                THEN RAISE(ABORT, 'reservation_time_conflict')
            END;
        END;

        DROP TRIGGER IF EXISTS reservations_validate_update;
        CREATE TRIGGER reservations_validate_update
        BEFORE UPDATE OF bench_id, starts_at, ends_at, status ON reservations
        WHEN NEW.starts_at IS NOT NULL AND NEW.ends_at IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN NEW.status NOT IN (
                    'queued', 'scheduled', 'active', 'released', 'expired',
                    'cancelled', 'rejected', 'expired_pending_operation'
                )
                THEN RAISE(ABORT, 'reservation_invalid_status')
            END;
            SELECT CASE
                WHEN NEW.ends_at <= NEW.starts_at
                THEN RAISE(ABORT, 'reservation_invalid_time')
            END;
            SELECT CASE
                WHEN NEW.status IN ('scheduled', 'active', 'expired_pending_operation')
                 AND EXISTS (
                    SELECT 1 FROM reservations existing
                    WHERE existing.id != NEW.id
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
                 )
                THEN RAISE(ABORT, 'reservation_time_conflict')
            END;
        END;
        """
    )


def _create_queue_transition_trigger(connection: sqlite3.Connection) -> None:
    """Protect terminal queue entries even in already-migrated Phase 3 databases."""

    connection.executescript(
        """
        DROP TRIGGER IF EXISTS reservation_queue_validate_status_update;
        CREATE TRIGGER reservation_queue_validate_status_update
        BEFORE UPDATE OF status ON reservation_queue
        WHEN OLD.status != NEW.status
        BEGIN
            SELECT CASE
                WHEN NOT (
                    OLD.status = 'waiting'
                    AND NEW.status IN ('promoted', 'cancelled', 'expired')
                )
                THEN RAISE(ABORT, 'queue_invalid_status_transition')
            END;
        END;
        """
    )
