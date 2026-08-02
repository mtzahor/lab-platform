from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 8


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
        """
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
