from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 10

DEFAULT_ORGANISATION_ID = "00000000-0000-0000-0000-000000000001"


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Upgrade an existing Lab Platform database to the latest schema."""

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
    from lab_platform.persistence.workflows import ensure_workflow_schema

    ensure_workflow_schema(connection)
    # Table/index creation is intentionally unconditional. This repairs databases
    # produced by interrupted or early Phase 4 builds even when version 5 was
    # already recorded.
    _create_phase4_tables(connection)
    _create_phase4_indexes(connection)
    if 5 not in applied:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (5, datetime('now'))"
        )
    # Phase 5 registry creation is intentionally unconditional so an interrupted
    # initialization can recreate missing tables and indexes even if the version
    # row committed. Existing incompatible tables require an explicit future
    # rebuild migration; CREATE TABLE IF NOT EXISTS cannot repair their shape.
    _create_phase5_agent_tables(connection)
    _create_phase5_agent_indexes(connection)
    if 6 not in applied:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (6, datetime('now'))"
        )
    _create_phase5_distributed_tables(connection)
    _create_phase5_distributed_indexes(connection)
    if 7 not in applied:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (7, datetime('now'))"
        )
    _add_distributed_operation_result_column(connection)
    if 8 not in applied:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (8, datetime('now'))"
        )
    # Phase 6 is an expand-only migration. Existing Phase 5 columns and uniqueness
    # constraints remain intact while identity-aware services move to tenant-scoped
    # repositories incrementally.
    _create_phase6_identity_tables(connection)
    _seed_default_organisation(connection)
    _add_phase6_central_columns(connection)
    _ensure_ci_cancellation_evidence_schema(connection)
    _create_phase6_indexes(connection)
    if 9 not in applied:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (9, datetime('now'))"
        )
    if 10 not in applied:
        _upgrade_phase10_tenant_storage_boundaries(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (10, datetime('now'))"
        )
    _create_queue_transition_trigger(connection)


def _create_phase5_agent_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'PENDING', 'ONLINE', 'DEGRADED', 'OFFLINE', 'DRAINING',
                    'DRAINED', 'REVOKED', 'INCOMPATIBLE'
                )
            ),
            version TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            location TEXT,
            labels_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(labels_json) AND json_type(labels_json) = 'object'),
            registered_at TEXT NOT NULL,
            last_connected_at TEXT,
            last_seen_at TEXT,
            disconnected_at TEXT,
            certificate_fingerprint TEXT,
            enrollment_status TEXT NOT NULL CHECK (
                enrollment_status IN ('PENDING', 'ENROLLED', 'REVOKED')
            ),
            revoked_at TEXT,
            CHECK (last_connected_at IS NULL OR last_connected_at >= registered_at),
            CHECK (revoked_at IS NULL OR revoked_at >= registered_at),
            CHECK (
                last_seen_at IS NULL OR (
                    last_connected_at IS NOT NULL AND last_seen_at >= last_connected_at
                )
            ),
            CHECK (
                disconnected_at IS NULL OR (
                    last_connected_at IS NOT NULL
                    AND disconnected_at >= last_connected_at
                    AND (last_seen_at IS NULL OR disconnected_at >= last_seen_at)
                )
            ),
            CHECK (
                (
                    status = 'REVOKED' AND enrollment_status = 'REVOKED'
                    AND revoked_at IS NOT NULL
                ) OR (
                    status != 'REVOKED' AND enrollment_status != 'REVOKED'
                    AND revoked_at IS NULL
                )
            )
        );

        CREATE TABLE IF NOT EXISTS agent_enrollment_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE CHECK (
                length(token_hash) = 64
                AND token_hash NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            revoked_at TEXT,
            allowed_labels_json TEXT NOT NULL DEFAULT '{}'
                CHECK (
                    json_valid(allowed_labels_json)
                    AND json_type(allowed_labels_json) = 'object'
                ),
            used_by_agent_id TEXT UNIQUE,
            enrollment_request_id TEXT UNIQUE,
            CHECK (
                (used_at IS NULL AND used_by_agent_id IS NULL
                    AND enrollment_request_id IS NULL)
                OR
                (used_at IS NOT NULL AND used_by_agent_id IS NOT NULL
                    AND enrollment_request_id IS NOT NULL)
            ),
            CHECK (expires_at > created_at),
            CHECK (used_at IS NULL OR (used_at >= created_at AND used_at < expires_at)),
            CHECK (revoked_at IS NULL OR revoked_at >= created_at),
            CHECK (used_at IS NULL OR revoked_at IS NULL OR revoked_at >= used_at),
            FOREIGN KEY (used_by_agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS agent_credentials (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind = 'OPAQUE_TOKEN'),
            credential_hash TEXT NOT NULL UNIQUE CHECK (
                length(credential_hash) = 64
                AND credential_hash NOT GLOB '*[^0-9a-f]*'
            ),
            version INTEGER NOT NULL CHECK (version > 0),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT,
            UNIQUE (agent_id, version),
            CHECK (expires_at IS NULL OR expires_at > created_at),
            CHECK (revoked_at IS NULL OR revoked_at >= created_at),
            CHECK (
                last_used_at IS NULL OR (
                    last_used_at >= created_at
                    AND (expires_at IS NULL OR last_used_at < expires_at)
                    AND (revoked_at IS NULL OR last_used_at <= revoked_at)
                )
            ),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );
        """
    )


def _create_phase5_agent_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS agents_status_seen
            ON agents(status, last_seen_at, id);
        CREATE UNIQUE INDEX IF NOT EXISTS agents_certificate_fingerprint
            ON agents(certificate_fingerprint)
            WHERE certificate_fingerprint IS NOT NULL;

        CREATE INDEX IF NOT EXISTS agent_enrollment_tokens_active_expiry
            ON agent_enrollment_tokens(expires_at, id)
            WHERE used_at IS NULL AND revoked_at IS NULL;
        CREATE INDEX IF NOT EXISTS agent_enrollment_tokens_created
            ON agent_enrollment_tokens(created_at DESC, id);

        CREATE UNIQUE INDEX IF NOT EXISTS agent_credentials_one_active
            ON agent_credentials(agent_id)
            WHERE revoked_at IS NULL;
        CREATE INDEX IF NOT EXISTS agent_credentials_agent_version
            ON agent_credentials(agent_id, version DESC);
        """,
    )


def _create_phase5_distributed_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_connections (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            protocol_version TEXT NOT NULL,
            connected_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            disconnected_at TEXT,
            last_sequence_number INTEGER NOT NULL DEFAULT 0
                CHECK (last_sequence_number >= 0),
            observed_clock_offset_seconds REAL NOT NULL DEFAULT 0
                CHECK (observed_clock_offset_seconds BETWEEN -86400 AND 86400),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (last_heartbeat_at >= connected_at),
            CHECK (
                disconnected_at IS NULL OR disconnected_at >= last_heartbeat_at
            )
        );

        CREATE TABLE IF NOT EXISTS global_benches (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            agent_slug TEXT NOT NULL,
            local_bench_id TEXT NOT NULL,
            name TEXT NOT NULL,
            backend_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('SIMULATED', 'PHYSICAL')),
            target_type TEXT,
            status TEXT NOT NULL CHECK (status IN ('ONLINE', 'OFFLINE', 'DEGRADED')),
            health TEXT NOT NULL,
            capabilities_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(capabilities_json) AND json_type(capabilities_json) = 'array'),
            labels_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(labels_json) AND json_type(labels_json) = 'object'),
            firmware_version TEXT,
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (agent_id, local_bench_id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (updated_at >= created_at),
            CHECK (last_seen_at IS NULL OR last_seen_at <= updated_at)
        );

        CREATE TABLE IF NOT EXISTS bench_snapshots (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            bench_count INTEGER NOT NULL CHECK (bench_count >= 0),
            snapshot_json TEXT NOT NULL
                CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'array'),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS remote_commands (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            command_type TEXT NOT NULL CHECK (
                command_type IN (
                    'PROBE', 'FLASH', 'RESET', 'READ_SERIAL', 'RUN_WORKFLOW',
                    'CANCEL_OPERATION', 'REFRESH_INVENTORY'
                )
            ),
            payload_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
            status TEXT NOT NULL CHECK (
                status IN (
                    'CREATED', 'QUEUED', 'DISPATCHED', 'ACCEPTED', 'RUNNING',
                    'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED', 'UNKNOWN'
                )
            ),
            created_at TEXT NOT NULL,
            dispatched_at TEXT,
            acknowledged_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            expires_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            operation_id TEXT,
            reservation_id TEXT,
            lease_version INTEGER CHECK (lease_version IS NULL OR lease_version > 0),
            error_code TEXT,
            error_message TEXT,
            UNIQUE (agent_id, idempotency_key),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (bench_id) REFERENCES global_benches(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (expires_at > created_at),
            CHECK ((reservation_id IS NULL) = (lease_version IS NULL))
        );

        CREATE TABLE IF NOT EXISTS remote_command_attempts (
            id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            connection_id TEXT,
            sequence_number INTEGER CHECK (sequence_number IS NULL OR sequence_number > 0),
            dispatched_at TEXT NOT NULL,
            acknowledged_at TEXT,
            failed_at TEXT,
            error_code TEXT,
            UNIQUE (command_id, attempt_number),
            FOREIGN KEY (command_id) REFERENCES remote_commands(id) ON DELETE CASCADE,
            FOREIGN KEY (connection_id) REFERENCES agent_connections(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS distributed_operations (
            id TEXT PRIMARY KEY,
            remote_command_id TEXT NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            reservation_id TEXT,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'CREATED', 'DISPATCHED', 'ACCEPTED', 'RUNNING', 'SUCCEEDED',
                    'FAILED', 'CANCELLED', 'UNKNOWN', 'RECONCILING'
                )
            ),
            progress INTEGER CHECK (progress IS NULL OR progress BETWEEN 0 AND 100),
            message TEXT,
            result_json TEXT CHECK (
                result_json IS NULL OR (
                    json_valid(result_json) AND json_type(result_json) = 'object'
                )
            ),
            created_at TEXT NOT NULL,
            dispatched_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            last_agent_update_at TEXT,
            reconciliation_deadline TEXT,
            error_code TEXT,
            error_message TEXT,
            FOREIGN KEY (remote_command_id) REFERENCES remote_commands(id)
                ON DELETE RESTRICT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (bench_id) REFERENCES global_benches(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS reservation_leases (
            reservation_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            lease_version INTEGER NOT NULL CHECK (lease_version > 0),
            released_at TEXT,
            PRIMARY KEY (reservation_id, lease_version),
            FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE RESTRICT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (bench_id) REFERENCES global_benches(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (valid_until > valid_from),
            CHECK (released_at IS NULL OR released_at >= valid_from)
        );

        CREATE TABLE IF NOT EXISTS coordinated_reservation_leases (
            reservation_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'ACTIVATING', 'ACTIVE', 'RENEWING', 'UNKNOWN',
                    'RELEASED', 'EXPIRED', 'REVOKED'
                )
            ),
            revision INTEGER NOT NULL CHECK (revision > 0),
            lease_version INTEGER NOT NULL CHECK (lease_version > 0),
            record_json TEXT NOT NULL
                CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE RESTRICT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (bench_id) REFERENCES global_benches(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS reservation_lease_mutations (
            mutation_key TEXT PRIMARY KEY,
            request_fingerprint TEXT NOT NULL CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            reservation_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            result_json TEXT NOT NULL
                CHECK (json_valid(result_json) AND json_type(result_json) = 'object'),
            created_at TEXT NOT NULL,
            FOREIGN KEY (reservation_id) REFERENCES coordinated_reservation_leases(reservation_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS reconciliation_reports (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            report_json TEXT NOT NULL
                CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS reconciliation_report_claims (
            report_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'COMPLETE')),
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            UNIQUE (agent_id, content_digest, report_id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS protocol_message_journal (
            message_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            connection_id TEXT,
            direction TEXT NOT NULL CHECK (
                direction IN ('agent_to_control_plane', 'control_plane_to_agent')
            ),
            sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
            message_type TEXT NOT NULL,
            correlation_id TEXT,
            payload_sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            handled_at TEXT,
            outcome TEXT NOT NULL CHECK (
                outcome IN ('RECEIVED', 'SENT', 'HANDLED', 'REJECTED', 'DUPLICATE')
            ),
            PRIMARY KEY (agent_id, direction, message_id),
            UNIQUE (connection_id, direction, sequence_number),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (connection_id) REFERENCES agent_connections(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS remote_artifacts (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            local_artifact_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            operation_id TEXT,
            name TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            uploaded_at TEXT,
            UNIQUE (agent_id, local_artifact_id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (command_id) REFERENCES remote_commands(id) ON DELETE RESTRICT,
            FOREIGN KEY (operation_id) REFERENCES distributed_operations(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS artifact_transfers (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (
                direction IN ('CONTROL_PLANE_TO_AGENT', 'AGENT_TO_CONTROL_PLANE')
            ),
            status TEXT NOT NULL CHECK (
                status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'EXPIRED')
            ),
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            completed_at TEXT,
            expected_sha256 TEXT NOT NULL,
            expected_size_bytes INTEGER NOT NULL CHECK (expected_size_bytes >= 0),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            error_code TEXT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (expires_at > created_at)
        );

        CREATE TABLE IF NOT EXISTS artifact_transfer_attempts (
            id TEXT PRIMARY KEY,
            transfer_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            bytes_transferred INTEGER NOT NULL DEFAULT 0 CHECK (bytes_transferred >= 0),
            sha256 TEXT,
            error_code TEXT,
            error_message TEXT,
            UNIQUE (transfer_id, attempt_number),
            FOREIGN KEY (transfer_id) REFERENCES artifact_transfers(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_timelines (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),
            message TEXT NOT NULL,
            correlation_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
            deduplication_key TEXT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS distributed_ci_workflows (
            ci_session_id TEXT PRIMARY KEY,
            remote_command_id TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            reservation_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            workflow_version INTEGER NOT NULL CHECK (workflow_version > 0),
            launch_idempotency_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            FOREIGN KEY (ci_session_id) REFERENCES ci_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (remote_command_id) REFERENCES remote_commands(id) ON DELETE RESTRICT,
            FOREIGN KEY (operation_id) REFERENCES distributed_operations(id) ON DELETE RESTRICT,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (bench_id) REFERENCES global_benches(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS agent_command_journal (
            command_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            command_fingerprint TEXT NOT NULL,
            command_type TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            status TEXT NOT NULL,
            received_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            CHECK (result_json IS NULL OR json_valid(result_json))
        );

        CREATE TABLE IF NOT EXISTS agent_local_reservation_leases (
            reservation_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            lease_version INTEGER NOT NULL CHECK (lease_version > 0),
            released_at TEXT,
            PRIMARY KEY (bench_id, lease_version),
            UNIQUE (reservation_id, lease_version)
        );

        CREATE TABLE IF NOT EXISTS agent_event_buffer (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL
                CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
            priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
            created_at TEXT NOT NULL,
            UNIQUE (agent_id, sequence_number)
        );

        CREATE TABLE IF NOT EXISTS agent_runtime_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _create_phase5_distributed_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS agent_connections_one_active
            ON agent_connections(agent_id) WHERE disconnected_at IS NULL;
        CREATE INDEX IF NOT EXISTS agent_connections_heartbeat
            ON agent_connections(disconnected_at, last_heartbeat_at, agent_id);
        CREATE INDEX IF NOT EXISTS global_benches_inventory
            ON global_benches(status, health, kind, agent_id, id);
        CREATE INDEX IF NOT EXISTS global_benches_labels
            ON global_benches(agent_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS bench_snapshots_agent_time
            ON bench_snapshots(agent_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS remote_commands_dispatch
            ON remote_commands(agent_id, status, created_at, id);
        CREATE INDEX IF NOT EXISTS remote_commands_expiry
            ON remote_commands(status, expires_at, id);
        CREATE INDEX IF NOT EXISTS distributed_operations_active
            ON distributed_operations(agent_id, status, created_at, id);
        CREATE INDEX IF NOT EXISTS distributed_operations_bench
            ON distributed_operations(bench_id, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS reservation_leases_current_active
            ON reservation_leases(bench_id) WHERE released_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS coordinated_reservation_leases_current_bench
            ON coordinated_reservation_leases(bench_id)
            WHERE state NOT IN ('RELEASED', 'EXPIRED', 'REVOKED');
        CREATE INDEX IF NOT EXISTS coordinated_reservation_leases_agent_state
            ON coordinated_reservation_leases(agent_id, state, updated_at, reservation_id);
        CREATE INDEX IF NOT EXISTS reservation_lease_mutations_reservation
            ON reservation_lease_mutations(reservation_id, revision, created_at);
        CREATE INDEX IF NOT EXISTS reconciliation_reports_agent_time
            ON reconciliation_reports(agent_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS reconciliation_report_claims_digest
            ON reconciliation_report_claims(agent_id, content_digest, status);
        CREATE INDEX IF NOT EXISTS protocol_message_journal_observed
            ON protocol_message_journal(agent_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS remote_artifacts_command
            ON remote_artifacts(command_id, created_at, id);
        CREATE INDEX IF NOT EXISTS artifact_transfers_pending
            ON artifact_transfers(status, expires_at, id);
        CREATE UNIQUE INDEX IF NOT EXISTS agent_timelines_deduplication
            ON agent_timelines(deduplication_key)
            WHERE deduplication_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS agent_timelines_agent_time
            ON agent_timelines(agent_id, timestamp DESC, id);
        CREATE INDEX IF NOT EXISTS distributed_ci_workflows_route
            ON distributed_ci_workflows(agent_id, bench_id, created_at, ci_session_id);
        CREATE INDEX IF NOT EXISTS agent_command_journal_status
            ON agent_command_journal(status, received_at, command_id);
        CREATE INDEX IF NOT EXISTS agent_local_reservation_current
            ON agent_local_reservation_leases(bench_id, released_at, lease_version DESC);
        CREATE INDEX IF NOT EXISTS agent_event_buffer_order
            ON agent_event_buffer(agent_id, sequence_number, id);
        CREATE INDEX IF NOT EXISTS agent_event_buffer_overflow
            ON agent_event_buffer(priority, created_at, id);
        """
    )


def _create_phase6_identity_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS organisations (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (updated_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            email TEXT,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED', 'LOCKED', 'DELETED')),
            authentication_source TEXT NOT NULL CHECK (authentication_source IN ('LOCAL', 'OIDC')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (updated_at >= created_at),
            CHECK (last_login_at IS NULL OR last_login_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS password_credentials (
            user_id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL CHECK (length(password_hash) >= 32),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE CASCADE ON DELETE CASCADE,
            CHECK (updated_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS organisation_memberships (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('OWNER', 'ADMIN', 'MEMBER', 'VIEWER')),
            created_at TEXT NOT NULL,
            UNIQUE (organisation_id, user_id),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE CASCADE ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (organisation_id, slug),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (updated_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS team_memberships (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('MANAGER', 'MEMBER', 'VIEWER')),
            created_at TEXT NOT NULL,
            UNIQUE (team_id, user_id),
            FOREIGN KEY (team_id) REFERENCES teams(id) ON UPDATE CASCADE ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE CASCADE ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS service_accounts (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED', 'REVOKED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT,
            UNIQUE (organisation_id, name),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (updated_at >= created_at),
            CHECK (last_used_at IS NULL OR last_used_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS user_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            organisation_id TEXT NOT NULL,
            secret_hash TEXT NOT NULL UNIQUE CHECK (
                length(secret_hash) = 64 AND secret_hash NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            maximum_expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT,
            user_agent TEXT,
            ip_address TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE CASCADE ON DELETE CASCADE,
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (expires_at > created_at),
            CHECK (maximum_expires_at >= expires_at),
            CHECK (last_seen_at >= created_at),
            CHECK (revoked_at IS NULL OR revoked_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS api_credentials (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_type TEXT NOT NULL CHECK (principal_type IN ('USER', 'SERVICE_ACCOUNT')),
            name TEXT NOT NULL,
            secret_hash TEXT NOT NULL UNIQUE CHECK (
                length(secret_hash) = 64 AND secret_hash NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT,
            allowed_ip_ranges_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(allowed_ip_ranges_json)
                AND json_type(allowed_ip_ranges_json) = 'array'
            ),
            permission_restrictions_json TEXT CHECK (
                permission_restrictions_json IS NULL OR (
                    json_valid(permission_restrictions_json)
                    AND json_type(permission_restrictions_json) = 'array'
                )
            ),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (expires_at IS NULL OR expires_at > created_at),
            CHECK (revoked_at IS NULL OR revoked_at >= created_at),
            CHECK (last_used_at IS NULL OR last_used_at >= created_at)
        );

        CREATE TABLE IF NOT EXISTS role_assignments (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            subject_type TEXT NOT NULL CHECK (
                subject_type IN ('USER', 'SERVICE_ACCOUNT', 'TEAM')
            ),
            subject_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (
                role IN (
                    'ORGANISATION_OWNER', 'ORGANISATION_ADMIN', 'LAB_ADMIN', 'OPERATOR',
                    'WORKFLOW_RUNNER', 'RESERVER', 'VIEWER', 'AUDITOR'
                )
            ),
            resource_type TEXT NOT NULL CHECK (
                resource_type IN ('ORGANISATION', 'AGENT', 'BENCH', 'WORKFLOW')
            ),
            resource_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            UNIQUE (
                organisation_id, subject_type, subject_id, role, resource_type, resource_id
            ),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK (expires_at IS NULL OR expires_at > created_at)
        );

        CREATE TABLE IF NOT EXISTS authorisation_snapshots (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            permission TEXT NOT NULL,
            resource_type TEXT NOT NULL CHECK (
                resource_type IN ('ORGANISATION', 'AGENT', 'BENCH', 'WORKFLOW', 'CI_SESSION')
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

        CREATE TABLE IF NOT EXISTS bench_access_policies (
            organisation_id TEXT NOT NULL,
            bench_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (
                visibility IN ('PRIVATE', 'ORGANISATION', 'RESTRICTED')
            ),
            reservation_role TEXT CHECK (
                reservation_role IS NULL OR reservation_role IN (
                    'ORGANISATION_OWNER', 'ORGANISATION_ADMIN', 'LAB_ADMIN', 'OPERATOR',
                    'WORKFLOW_RUNNER', 'RESERVER', 'VIEWER', 'AUDITOR'
                )
            ),
            operation_role TEXT CHECK (
                operation_role IS NULL OR operation_role IN (
                    'ORGANISATION_OWNER', 'ORGANISATION_ADMIN', 'LAB_ADMIN', 'OPERATOR',
                    'WORKFLOW_RUNNER', 'RESERVER', 'VIEWER', 'AUDITOR'
                )
            ),
            allowed_team_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(allowed_team_ids_json)
                AND json_type(allowed_team_ids_json) = 'array'
            ),
            PRIMARY KEY (organisation_id, bench_id),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS workflow_access_policies (
            organisation_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (
                visibility IN ('ORGANISATION', 'RESTRICTED', 'ADMIN_ONLY')
            ),
            PRIMARY KEY (organisation_id, workflow_id),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor_type TEXT CHECK (actor_type IS NULL OR actor_type IN ('USER', 'SERVICE_ACCOUNT')),
            actor_id TEXT,
            actor_display_name TEXT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            outcome TEXT NOT NULL CHECK (outcome IN ('SUCCEEDED', 'FAILED', 'DENIED')),
            request_id TEXT,
            source_ip TEXT,
            user_agent TEXT,
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(metadata_json) AND json_type(metadata_json) = 'object'
            ),
            FOREIGN KEY (organisation_id) REFERENCES organisations(id)
                ON UPDATE CASCADE ON DELETE RESTRICT,
            CHECK ((actor_type IS NULL) = (actor_id IS NULL))
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id TEXT PRIMARY KEY,
            organisation_slug TEXT NOT NULL,
            username TEXT NOT NULL,
            ip_address TEXT,
            attempted_at TEXT NOT NULL,
            succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1))
        );
        """
    )


def _seed_default_organisation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO organisations "
        "(id, slug, name, status, created_at, updated_at) "
        "VALUES (?, 'default', 'Default Organisation', 'ACTIVE', datetime('now'), datetime('now'))",
        (DEFAULT_ORGANISATION_ID,),
    )


def _add_phase6_central_columns(connection: sqlite3.Connection) -> None:
    # These are control-plane-owned records. Agent-local execution journals are
    # intentionally excluded because Agent authentication remains a separate trust domain.
    organisation_tables = (
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
    )
    declaration = f"TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}'"
    for table in organisation_tables:
        _add_column(connection, table, "organisation_id", declaration)

    _add_column(connection, "reservations", "owner_principal_id", "TEXT")
    _add_column(
        connection,
        "reservations",
        "owner_principal_type",
        "TEXT CHECK (owner_principal_type IS NULL OR "
        "owner_principal_type IN ('USER', 'SERVICE_ACCOUNT'))",
    )
    _add_column(
        connection,
        "remote_commands",
        "actor_context_json",
        "TEXT CHECK (actor_context_json IS NULL OR "
        "(json_valid(actor_context_json) AND json_type(actor_context_json) = 'object'))",
    )
    _add_column(connection, "remote_commands", "authorisation_snapshot_id", "TEXT")
    _add_column(connection, "ci_sessions", "requested_by_principal_id", "TEXT")
    _add_column(
        connection,
        "ci_sessions",
        "requested_by_principal_type",
        "TEXT CHECK (requested_by_principal_type IS NULL OR "
        "requested_by_principal_type IN ('USER', 'SERVICE_ACCOUNT'))",
    )


def _ensure_ci_cancellation_evidence_schema(connection: sqlite3.Connection) -> None:
    """Repair already-versioned Phase 6 databases for durable CI cancel evidence."""

    _add_column(
        connection,
        "ci_sessions",
        "cancel_actor_context_json",
        "TEXT CHECK (cancel_actor_context_json IS NULL OR "
        "(json_valid(cancel_actor_context_json) "
        "AND json_type(cancel_actor_context_json) = 'object'))",
    )
    _add_column(connection, "ci_sessions", "cancel_authorisation_snapshot_id", "TEXT")

    if connection.__class__.__module__ != "sqlite3":
        connection.execute(
            "ALTER TABLE authorisation_snapshots DROP CONSTRAINT IF EXISTS "
            "authorisation_snapshots_resource_type_check"
        )
        connection.execute(
            "ALTER TABLE authorisation_snapshots ADD CONSTRAINT "
            "authorisation_snapshots_resource_type_check CHECK "
            "(resource_type IN ('ORGANISATION', 'AGENT', 'BENCH', 'WORKFLOW', 'CI_SESSION'))"
        )
        return

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'authorisation_snapshots'"
    ).fetchone()
    if row is None or "CI_SESSION" in str(row[0]):
        return
    _execute_transactional_script(
        connection,
        """
        DROP TABLE IF EXISTS authorisation_snapshots_ci_cancel;
        CREATE TABLE authorisation_snapshots_ci_cancel (
            id TEXT PRIMARY KEY,
            organisation_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            permission TEXT NOT NULL,
            resource_type TEXT NOT NULL CHECK (
                resource_type IN (
                    'ORGANISATION', 'AGENT', 'BENCH', 'WORKFLOW', 'CI_SESSION'
                )
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
        INSERT INTO authorisation_snapshots_ci_cancel
            (id, organisation_id, principal_id, permission, resource_type, resource_id,
             granted_by_assignments_json, evaluated_at)
        SELECT id, organisation_id, principal_id, permission, resource_type, resource_id,
               granted_by_assignments_json, evaluated_at
        FROM authorisation_snapshots;
        DROP TABLE authorisation_snapshots;
        ALTER TABLE authorisation_snapshots_ci_cancel RENAME TO authorisation_snapshots;
        """,
    )


def _create_phase6_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_organisation_username
            ON users(organisation_id, username COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS users_organisation_status
            ON users(organisation_id, status, username, id);
        CREATE INDEX IF NOT EXISTS organisation_memberships_user
            ON organisation_memberships(organisation_id, user_id);
        CREATE UNIQUE INDEX IF NOT EXISTS teams_organisation_slug_nocase
            ON teams(organisation_id, slug COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS team_memberships_user
            ON team_memberships(user_id, team_id);
        CREATE INDEX IF NOT EXISTS service_accounts_organisation_status
            ON service_accounts(organisation_id, status, name, id);
        CREATE INDEX IF NOT EXISTS user_sessions_active
            ON user_sessions(organisation_id, user_id, revoked_at, expires_at);
        CREATE INDEX IF NOT EXISTS api_credentials_principal
            ON api_credentials(organisation_id, principal_type, principal_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS api_credentials_active
            ON api_credentials(organisation_id, revoked_at, expires_at);
        CREATE INDEX IF NOT EXISTS role_assignments_subject
            ON role_assignments(organisation_id, subject_type, subject_id, expires_at);
        CREATE INDEX IF NOT EXISTS role_assignments_resource
            ON role_assignments(organisation_id, resource_type, resource_id, expires_at);
        CREATE INDEX IF NOT EXISTS authorisation_snapshots_principal_time
            ON authorisation_snapshots(organisation_id, principal_id, evaluated_at DESC);
        CREATE INDEX IF NOT EXISTS audit_events_organisation_time
            ON audit_events(organisation_id, timestamp DESC, id);
        CREATE INDEX IF NOT EXISTS audit_events_organisation_action
            ON audit_events(organisation_id, action, timestamp DESC, id);
        CREATE INDEX IF NOT EXISTS audit_events_retention_time
            ON audit_events(timestamp, id);
        CREATE INDEX IF NOT EXISTS login_attempts_subject_time
            ON login_attempts(organisation_slug, username, attempted_at DESC, id);
        CREATE INDEX IF NOT EXISTS login_attempts_ip_time
            ON login_attempts(ip_address, attempted_at DESC, id)
            WHERE ip_address IS NOT NULL;
        CREATE INDEX IF NOT EXISTS login_attempts_retention_time
            ON login_attempts(attempted_at, id);

        CREATE INDEX IF NOT EXISTS reservations_organisation
            ON reservations(organisation_id, created_at DESC, id);
        CREATE INDEX IF NOT EXISTS operations_organisation
            ON operations(organisation_id, created_at DESC, id);
        CREATE INDEX IF NOT EXISTS workflows_organisation
            ON workflows(organisation_id, name, version);
        CREATE INDEX IF NOT EXISTS ci_sessions_organisation
            ON ci_sessions(organisation_id, created_at DESC, id);
        CREATE INDEX IF NOT EXISTS artifacts_organisation
            ON artifacts(organisation_id, created_at DESC, id);
        CREATE INDEX IF NOT EXISTS agents_organisation
            ON agents(organisation_id, status, id);
        CREATE INDEX IF NOT EXISTS global_benches_organisation
            ON global_benches(organisation_id, agent_id, status, id);
        CREATE INDEX IF NOT EXISTS remote_commands_organisation
            ON remote_commands(organisation_id, agent_id, created_at, id);
        CREATE INDEX IF NOT EXISTS distributed_operations_organisation
            ON distributed_operations(organisation_id, created_at DESC, id);
        """
    )


def _upgrade_phase10_tenant_storage_boundaries(connection: sqlite3.Connection) -> None:
    """Replace the remaining global user-key constraints with tenant-safe storage."""

    if connection.__class__.__module__ == "sqlite3":
        _rebuild_phase10_sqlite_tenant_tables(connection)
    else:
        _upgrade_phase10_postgresql_workflow_constraints(connection)
    _create_phase10_scoped_idempotency_indexes(connection)


def _rebuild_phase10_sqlite_tenant_tables(connection: sqlite3.Connection) -> None:
    # ``ci_sessions`` has a foreign key to ``workflow_runs`` and the mutation journal needs
    # a composite primary key. SQLite cannot replace these tables in place, so commit the
    # expand-only v9 work, rebuild the closed set atomically, and validate every relationship
    # before returning to the normal migration transaction.
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _execute_transactional_script(
            connection,
            f"""
            DROP TABLE IF EXISTS workflow_step_results_phase10;
            DROP TABLE IF EXISTS workflow_runs_phase10;
            DROP TABLE IF EXISTS workflows_phase10;
            DROP TABLE IF EXISTS distributed_ci_workflows_phase10;
            DROP TABLE IF EXISTS reservation_lease_mutations_phase10;

            CREATE TABLE workflows_phase10 (
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                name TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (organisation_id, name, version)
            );
            CREATE TABLE workflow_runs_phase10 (
                id TEXT PRIMARY KEY,
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                workflow_name TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                bench_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'running', 'succeeded', 'failed',
                        'cancel_requested', 'cancelled'
                    )
                ),
                current_step INTEGER CHECK (current_step IS NULL OR current_step >= 0),
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                error_message TEXT,
                UNIQUE (organisation_id, id),
                FOREIGN KEY (organisation_id, workflow_name, workflow_version)
                    REFERENCES workflows_phase10(organisation_id, name, version)
            );
            CREATE TABLE workflow_step_results_phase10 (
                id TEXT PRIMARY KEY,
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                workflow_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL CHECK (step_index >= 0),
                name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL CHECK (
                    action IN ('flash', 'reset', 'read_serial', 'assert_serial', 'wait', 'probe')
                ),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled'
                    )
                ),
                started_at TEXT,
                completed_at TEXT,
                output_json TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
                    json_valid(artifact_ids_json)
                    AND json_type(artifact_ids_json) = 'array'
                ),
                UNIQUE (organisation_id, workflow_run_id, step_index),
                FOREIGN KEY (organisation_id, workflow_run_id)
                    REFERENCES workflow_runs_phase10(organisation_id, id) ON DELETE CASCADE
            );
            CREATE TABLE distributed_ci_workflows_phase10 (
                ci_session_id TEXT PRIMARY KEY,
                remote_command_id TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                bench_id TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                workflow_version INTEGER NOT NULL CHECK (workflow_version > 0),
                launch_idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                FOREIGN KEY (ci_session_id) REFERENCES ci_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (remote_command_id)
                    REFERENCES remote_commands(id) ON DELETE RESTRICT,
                FOREIGN KEY (operation_id)
                    REFERENCES distributed_operations(id) ON DELETE RESTRICT,
                FOREIGN KEY (agent_id)
                    REFERENCES agents(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                FOREIGN KEY (bench_id)
                    REFERENCES global_benches(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                FOREIGN KEY (reservation_id)
                    REFERENCES reservations(id) ON DELETE RESTRICT
            );
            CREATE TABLE reservation_lease_mutations_phase10 (
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                mutation_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL CHECK (
                    length(request_fingerprint) = 64
                    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                reservation_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                result_json TEXT NOT NULL CHECK (
                    json_valid(result_json) AND json_type(result_json) = 'object'
                ),
                created_at TEXT NOT NULL,
                PRIMARY KEY (organisation_id, mutation_key),
                FOREIGN KEY (reservation_id)
                    REFERENCES coordinated_reservation_leases(reservation_id)
                    ON DELETE RESTRICT
            );
            """,
        )
        connection.execute(
            "INSERT INTO workflows_phase10 "
            "(organisation_id, name, version, definition_json, created_at) "
            "SELECT organisation_id, name, version, definition_json, created_at FROM workflows"
        )
        connection.execute(
            "INSERT INTO workflow_runs_phase10 "
            "(id, organisation_id, workflow_name, workflow_version, bench_id, owner, "
            "reservation_id, status, current_step, created_at, started_at, completed_at, "
            "error_code, error_message) "
            "SELECT run.id, COALESCE(definition.organisation_id, run.organisation_id, ?), "
            "run.workflow_name, run.workflow_version, run.bench_id, run.owner, "
            "run.reservation_id, run.status, "
            "run.current_step, run.created_at, run.started_at, run.completed_at, "
            "run.error_code, run.error_message FROM workflow_runs run "
            "LEFT JOIN workflows definition ON definition.name = run.workflow_name "
            "AND definition.version = run.workflow_version",
            (DEFAULT_ORGANISATION_ID,),
        )
        connection.execute(
            "INSERT INTO workflow_step_results_phase10 "
            "(id, organisation_id, workflow_run_id, step_index, name, action, status, "
            "started_at, completed_at, output_json, error_code, error_message, "
            "artifact_ids_json) "
            "SELECT result.id, COALESCE(run.organisation_id, result.organisation_id, ?), "
            "result.workflow_run_id, result.step_index, result.name, result.action, "
            "result.status, result.started_at, "
            "result.completed_at, result.output_json, result.error_code, "
            "result.error_message, result.artifact_ids_json "
            "FROM workflow_step_results result "
            "LEFT JOIN workflow_runs_phase10 run ON run.id = result.workflow_run_id",
            (DEFAULT_ORGANISATION_ID,),
        )
        connection.execute(
            "INSERT INTO distributed_ci_workflows_phase10 "
            "(ci_session_id, remote_command_id, operation_id, agent_id, bench_id, "
            "reservation_id, workflow_name, workflow_version, launch_idempotency_key, "
            "request_fingerprint, created_at, organisation_id) "
            "SELECT ci_session_id, remote_command_id, operation_id, agent_id, bench_id, "
            "reservation_id, workflow_name, workflow_version, launch_idempotency_key, "
            "request_fingerprint, created_at, organisation_id FROM distributed_ci_workflows"
        )
        connection.execute(
            "INSERT INTO reservation_lease_mutations_phase10 "
            "(organisation_id, mutation_key, request_fingerprint, reservation_id, revision, "
            "result_json, created_at) "
            "SELECT organisation_id, mutation_key, request_fingerprint, reservation_id, "
            "revision, result_json, created_at FROM reservation_lease_mutations"
        )
        _execute_transactional_script(
            connection,
            """
            DROP TABLE workflow_step_results;
            DROP TABLE workflow_runs;
            DROP TABLE workflows;
            DROP TABLE distributed_ci_workflows;
            DROP TABLE reservation_lease_mutations;
            ALTER TABLE workflows_phase10 RENAME TO workflows;
            ALTER TABLE workflow_runs_phase10 RENAME TO workflow_runs;
            ALTER TABLE workflow_step_results_phase10 RENAME TO workflow_step_results;
            ALTER TABLE distributed_ci_workflows_phase10 RENAME TO distributed_ci_workflows;
            ALTER TABLE reservation_lease_mutations_phase10
                RENAME TO reservation_lease_mutations;

            CREATE UNIQUE INDEX workflow_runs_one_active_per_bench
                ON workflow_runs(organisation_id, bench_id)
                WHERE status IN ('pending', 'running', 'cancel_requested');
            CREATE INDEX workflow_runs_created
                ON workflow_runs(organisation_id, created_at DESC);
            CREATE INDEX workflow_step_results_run
                ON workflow_step_results(organisation_id, workflow_run_id, step_index);
            CREATE INDEX workflows_organisation
                ON workflows(organisation_id, name, version);
            CREATE INDEX distributed_ci_workflows_route
                ON distributed_ci_workflows(
                    organisation_id, agent_id, bench_id, created_at, ci_session_id
                );
            CREATE INDEX reservation_lease_mutations_reservation
                ON reservation_lease_mutations(
                    organisation_id, reservation_id, revision, created_at
                );
            """,
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Phase 10 migration produced foreign-key violations: {violations!r}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _execute_transactional_script(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    """Execute simple migration DDL without sqlite3.executescript's implicit commit."""

    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _upgrade_phase10_postgresql_workflow_constraints(
    connection: sqlite3.Connection,
) -> None:
    # PostgreSQL can replace these constraints directly, preserving dependent table OIDs.
    connection.execute(
        """
        DO $phase10$
        DECLARE constraint_row record;
        BEGIN
            -- Foreign keys must be removed before the unique constraints whose
            -- backing indexes they reference. PostgreSQL otherwise rejects the
            -- parent-constraint drop with DependentObjectsStillExist.
            FOR constraint_row IN
                SELECT conrelid::regclass AS table_name, conname
                FROM pg_constraint
                WHERE conrelid IN (
                    'workflow_runs'::regclass,
                    'workflow_step_results'::regclass
                )
                AND contype = 'f'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %%I DROP CONSTRAINT %%I',
                    constraint_row.table_name,
                    constraint_row.conname
                );
            END LOOP;
            FOR constraint_row IN
                SELECT conrelid::regclass AS table_name, conname
                FROM pg_constraint
                WHERE conrelid IN (
                    'workflow_runs'::regclass,
                    'workflow_step_results'::regclass
                )
                AND contype = 'u'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %%I DROP CONSTRAINT %%I',
                    constraint_row.table_name,
                    constraint_row.conname
                );
            END LOOP;
        END
        $phase10$
        """
    )
    connection.execute(
        "UPDATE workflow_runs AS run SET organisation_id = definition.organisation_id "
        "FROM workflows AS definition WHERE definition.name = run.workflow_name "
        "AND definition.version = run.workflow_version"
    )
    connection.execute(
        "UPDATE workflow_step_results AS result SET organisation_id = run.organisation_id "
        "FROM workflow_runs AS run WHERE run.id = result.workflow_run_id"
    )
    connection.execute("ALTER TABLE workflows DROP CONSTRAINT IF EXISTS workflows_pkey")
    connection.execute(
        "ALTER TABLE workflows ADD CONSTRAINT workflows_pkey "
        "PRIMARY KEY (organisation_id, name, version)"
    )
    connection.execute(
        "ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_tenant_identity "
        "UNIQUE (organisation_id, id)"
    )
    connection.execute(
        "ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_definition_tenant_fk "
        "FOREIGN KEY (organisation_id, workflow_name, workflow_version) "
        "REFERENCES workflows(organisation_id, name, version)"
    )
    connection.execute(
        "ALTER TABLE workflow_step_results ADD CONSTRAINT workflow_step_results_tenant_step_key "
        "UNIQUE (organisation_id, workflow_run_id, step_index)"
    )
    connection.execute(
        "ALTER TABLE workflow_step_results ADD CONSTRAINT workflow_step_results_run_tenant_fk "
        "FOREIGN KEY (organisation_id, workflow_run_id) "
        "REFERENCES workflow_runs(organisation_id, id) ON DELETE CASCADE"
    )
    connection.execute(
        "ALTER TABLE distributed_ci_workflows "
        "DROP CONSTRAINT IF EXISTS distributed_ci_workflows_launch_idempotency_key_key"
    )
    connection.execute(
        "ALTER TABLE reservation_lease_mutations "
        "DROP CONSTRAINT IF EXISTS reservation_lease_mutations_pkey"
    )
    connection.execute(
        "ALTER TABLE reservation_lease_mutations "
        "ADD CONSTRAINT reservation_lease_mutations_pkey "
        "PRIMARY KEY (organisation_id, mutation_key)"
    )
    connection.executescript(
        """
        DROP INDEX IF EXISTS workflow_runs_one_active_per_bench;
        CREATE UNIQUE INDEX workflow_runs_one_active_per_bench
            ON workflow_runs(organisation_id, bench_id)
            WHERE status IN ('pending', 'running', 'cancel_requested');
        DROP INDEX IF EXISTS workflow_runs_created;
        CREATE INDEX workflow_runs_created
            ON workflow_runs(organisation_id, created_at DESC);
        DROP INDEX IF EXISTS workflow_step_results_run;
        CREATE INDEX workflow_step_results_run
            ON workflow_step_results(organisation_id, workflow_run_id, step_index);
        """
    )


def _create_phase10_scoped_idempotency_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP INDEX IF EXISTS ci_sessions_idempotency;
        CREATE UNIQUE INDEX ci_sessions_idempotency
            ON ci_sessions(organisation_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS ci_sessions_workflow_launch_idempotency;
        CREATE UNIQUE INDEX ci_sessions_workflow_launch_idempotency
            ON ci_sessions(organisation_id, workflow_launch_idempotency_key)
            WHERE workflow_launch_idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS ci_sessions_finalize_idempotency;
        CREATE UNIQUE INDEX ci_sessions_finalize_idempotency
            ON ci_sessions(organisation_id, finalize_idempotency_key)
            WHERE finalize_idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS artifacts_idempotency;
        CREATE UNIQUE INDEX artifacts_idempotency
            ON artifacts(organisation_id, owner_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS reservations_idempotency;
        CREATE UNIQUE INDEX reservations_idempotency
            ON reservations(organisation_id, bench_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS queue_idempotency;
        CREATE UNIQUE INDEX queue_idempotency
            ON reservation_queue(organisation_id, bench_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        DROP INDEX IF EXISTS distributed_ci_workflows_launch_idempotency;
        CREATE UNIQUE INDEX distributed_ci_workflows_launch_idempotency
            ON distributed_ci_workflows(organisation_id, launch_idempotency_key);
        """
    )


def _create_phase4_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL,
            scopes_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(scopes_json) AND json_type(scopes_json) = 'array'),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ci_sessions (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL CHECK (
                provider IN ('github_actions', 'gitlab_ci', 'jenkins', 'local', 'unknown')
            ),
            external_run_id TEXT NOT NULL,
            repository TEXT,
            ref TEXT,
            commit_sha TEXT,
            actor TEXT,
            requested_by TEXT NOT NULL,
            cancel_actor_context_json TEXT CHECK (
                cancel_actor_context_json IS NULL OR (
                    json_valid(cancel_actor_context_json)
                    AND json_type(cancel_actor_context_json) = 'object'
                )
            ),
            cancel_authorisation_snapshot_id TEXT,
            bench_id TEXT,
            reservation_id TEXT,
            workflow_run_id TEXT,
            status TEXT NOT NULL CHECK (
                status IN (
                    'created', 'waiting_for_bench', 'reserved', 'running',
                    'succeeded', 'failed', 'cancel_requested', 'cancelled',
                    'timed_out', 'cleanup_pending', 'completed'
                )
            ),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            heartbeat_at TEXT,
            timeout_at TEXT,
            cleanup_status TEXT NOT NULL DEFAULT 'not_started' CHECK (
                cleanup_status IN ('not_started', 'pending', 'running', 'succeeded', 'failed')
            ),
            bench_request_json TEXT CHECK (
                bench_request_json IS NULL OR (
                    json_valid(bench_request_json) AND json_type(bench_request_json) = 'object'
                )
            ),
            idempotency_key TEXT,
            outcome TEXT NOT NULL DEFAULT 'pending' CHECK (
                outcome IN (
                    'pending', 'succeeded', 'failed', 'cancelled', 'timed_out',
                    'infrastructure_error'
                )
            ),
            errors_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(errors_json) AND json_type(errors_json) = 'array'),
            workflow_launch_idempotency_key TEXT,
            finalize_idempotency_key TEXT,
            FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE SET NULL,
            FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS ci_cleanup_results (
            ci_session_id TEXT PRIMARY KEY,
            reservation_released INTEGER NOT NULL CHECK (reservation_released IN (0, 1)),
            workflow_stopped INTEGER NOT NULL CHECK (workflow_stopped IN (0, 1)),
            locks_released INTEGER NOT NULL CHECK (locks_released IN (0, 1)),
            serial_closed INTEGER NOT NULL CHECK (serial_closed IN (0, 1)),
            artifacts_finalized INTEGER NOT NULL CHECK (artifacts_finalized IN (0, 1)),
            errors_json TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(errors_json) AND json_type(errors_json) = 'array'),
            recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (ci_session_id) REFERENCES ci_sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            owner_type TEXT NOT NULL CHECK (
                owner_type IN ('ci_session', 'workflow_run', 'workflow_step', 'operation')
            ),
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            content_type TEXT,
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK (
                length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            expires_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
            idempotency_key TEXT
        );
        """
    )


def _create_phase4_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS api_tokens_owner
            ON api_tokens(owner, created_at DESC);
        CREATE INDEX IF NOT EXISTS api_tokens_active
            ON api_tokens(revoked_at, expires_at);

        CREATE UNIQUE INDEX IF NOT EXISTS ci_sessions_idempotency
            ON ci_sessions(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ci_sessions_workflow_launch_idempotency
            ON ci_sessions(workflow_launch_idempotency_key)
            WHERE workflow_launch_idempotency_key IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ci_sessions_finalize_idempotency
            ON ci_sessions(finalize_idempotency_key)
            WHERE finalize_idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ci_sessions_status_created
            ON ci_sessions(status, created_at, id);
        CREATE INDEX IF NOT EXISTS ci_sessions_heartbeat
            ON ci_sessions(status, heartbeat_at, timeout_at);
        CREATE INDEX IF NOT EXISTS ci_sessions_reservation
            ON ci_sessions(reservation_id)
            WHERE reservation_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ci_sessions_workflow_run
            ON ci_sessions(workflow_run_id)
            WHERE workflow_run_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS artifacts_idempotency
            ON artifacts(owner_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS artifacts_owner
            ON artifacts(owner_type, owner_id, created_at, id);
        CREATE INDEX IF NOT EXISTS artifacts_sha256
            ON artifacts(sha256);
        CREATE INDEX IF NOT EXISTS artifacts_expiry
            ON artifacts(expires_at)
            WHERE expires_at IS NOT NULL;
        """
    )


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


def _add_distributed_operation_result_column(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "distributed_operations",
        "result_json",
        "TEXT CHECK (result_json IS NULL OR "
        "(json_valid(result_json) AND json_type(result_json) = 'object'))",
    )


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
