from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core.workflows import WorkflowRunNotFoundError
from lab_platform.models import (
    ArtifactOwnerType,
    ArtifactRecord,
    CiProvider,
    CiSession,
    CiSessionStatus,
    PrincipalType,
    QueueEntry,
    Reservation,
    ReservationStatus,
    WaitWorkflowStep,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRequirements,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence import (
    DEFAULT_ORGANISATION_ID,
    SCHEMA_VERSION,
    SQLiteCiSessionRepository,
    SQLiteGenericArtifactRepository,
    SQLiteQueueRepository,
    SQLiteTimedReservationRepository,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.migrations import apply_migrations

from tests.support.persistence import open_sqlite_database

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _definition(organisation_id: UUID) -> WorkflowDefinition:
    return WorkflowDefinition(
        organisation_id=organisation_id,
        name="shared-workflow",
        version=1,
        requirements=WorkflowRequirements(),
        steps=[WaitWorkflowStep(action="wait", seconds=1)],
    )


def _run(organisation_id: UUID, *, run_id: UUID | None = None) -> WorkflowRun:
    return WorkflowRun(
        id=run_id or uuid4(),
        organisation_id=organisation_id,
        workflow_name="shared-workflow",
        workflow_version=1,
        bench_id="shared-bench",
        owner=f"owner-{organisation_id}",
        reservation_id=uuid4(),
        created_at=NOW,
    )


def _session(organisation_id: UUID, *, session_id: UUID | None = None) -> CiSession:
    return CiSession(
        id=session_id or uuid4(),
        provider=CiProvider.LOCAL,
        external_run_id=f"run-{organisation_id}",
        requested_by=f"user-{organisation_id}",
        organisation_id=organisation_id,
        requested_by_principal_id=uuid4(),
        requested_by_principal_type=PrincipalType.USER,
        status=CiSessionStatus.RESERVED,
        created_at=NOW,
    )


def test_two_organisations_can_reuse_workflow_and_retry_keys_without_cross_tenant_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "tenant-keys.db")
        workflows = SQLiteWorkflowRepository(database, initialize_schema=False)
        ci_sessions = SQLiteCiSessionRepository(database)
        artifacts = SQLiteGenericArtifactRepository(database)
        reservations = SQLiteTimedReservationRepository(database)
        queue = SQLiteQueueRepository(database)
        try:
            definition_a = await workflows.save_definition(_definition(ORG_A))
            definition_b = await workflows.save_definition(_definition(ORG_B))
            assert definition_a.organisation_id == ORG_A
            assert definition_b.organisation_id == ORG_B
            assert (
                await workflows.get_definition(
                    definition_a.name,
                    definition_a.version,
                    organisation_id=ORG_B,
                )
                == definition_b
            )
            assert await workflows.list_definitions(organisation_id=ORG_A) == [definition_a]
            assert await workflows.list_definitions(organisation_id=ORG_B) == [definition_b]
            assert await workflows.get_definition(definition_a.name) is None
            assert await workflows.list_definitions() == []

            run_a = await workflows.create_run(_run(ORG_A))
            run_b = await workflows.create_run(_run(ORG_B))
            step_a = WorkflowStepResult(
                organisation_id=ORG_A,
                workflow_run_id=run_a.id,
                step_index=0,
                action=WorkflowAction.WAIT,
                status=WorkflowStepStatus.RUNNING,
                started_at=NOW,
            )
            step_b = step_a.model_copy(
                update={
                    "id": uuid4(),
                    "organisation_id": ORG_B,
                    "workflow_run_id": run_b.id,
                }
            )
            await workflows.create_step_result(step_a)
            await workflows.create_step_result(step_b)

            assert await workflows.get_run(run_a.id, organisation_id=ORG_B) is None
            assert (
                await workflows.list_step_results(
                    run_a.id,
                    organisation_id=ORG_B,
                )
                == []
            )
            spoofed = run_a.model_copy(
                update={
                    "organisation_id": ORG_B,
                    "status": WorkflowRunStatus.FAILED,
                    "completed_at": NOW,
                }
            )
            with pytest.raises(WorkflowRunNotFoundError):
                await workflows.update_run(spoofed)
            assert (await workflows.get_run(run_a.id, organisation_id=ORG_A)) == run_a

            # An omitted recovery scope is the legacy tenant, not a global sweep.
            assert await workflows.recover_interrupted(NOW + timedelta(minutes=1)) == []
            [recovered_a] = await workflows.recover_interrupted(
                NOW + timedelta(minutes=1),
                organisation_id=ORG_A,
            )
            assert recovered_a.status is WorkflowRunStatus.FAILED
            assert (await workflows.get_run(run_b.id, organisation_id=ORG_B)) == run_b
            assert (await workflows.list_step_results(run_b.id, organisation_id=ORG_B)) == [step_b]

            session_a = await ci_sessions.create(_session(ORG_A), idempotency_key="same-create")
            session_b = await ci_sessions.create(_session(ORG_B), idempotency_key="same-create")
            assert await ci_sessions.get_by_idempotency_key("same-create") is None
            assert (
                await ci_sessions.attach_workflow_run(
                    session_a.id,
                    run_b.id,
                    idempotency_key="cross-tenant-launch",
                    started_at=NOW,
                )
                is None
            )
            attached_a = await ci_sessions.attach_workflow_run(
                session_a.id,
                run_a.id,
                idempotency_key="same-launch",
                started_at=NOW,
            )
            attached_b = await ci_sessions.attach_workflow_run(
                session_b.id,
                run_b.id,
                idempotency_key="same-launch",
                started_at=NOW,
            )
            assert attached_a is not None and attached_a.id == session_a.id
            assert attached_b is not None and attached_b.id == session_b.id
            assert (
                await ci_sessions.get_by_workflow_launch_idempotency_key(
                    "same-launch", organisation_id=ORG_A
                )
            ) == attached_a
            assert (
                await ci_sessions.get_by_workflow_launch_idempotency_key(
                    "same-launch", organisation_id=ORG_B
                )
            ) == attached_b

            finalized_a = await ci_sessions.mark_finalized(
                attached_a.model_copy(
                    update={"status": CiSessionStatus.COMPLETED, "completed_at": NOW}
                ),
                idempotency_key="same-finalize",
            )
            finalized_b = await ci_sessions.mark_finalized(
                attached_b.model_copy(
                    update={"status": CiSessionStatus.COMPLETED, "completed_at": NOW}
                ),
                idempotency_key="same-finalize",
            )
            assert finalized_a is not None and finalized_a.id == session_a.id
            assert finalized_b is not None and finalized_b.id == session_b.id

            owner_id = uuid4()
            artifact_a = ArtifactRecord(
                organisation_id=ORG_A,
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="result.txt",
                artifact_type="text",
                path="org-a/result.txt",
                size_bytes=1,
                sha256="a" * 64,
                created_at=NOW,
            )
            artifact_b = artifact_a.model_copy(
                update={"id": uuid4(), "organisation_id": ORG_B, "path": "org-b/result.txt"}
            )
            assert await artifacts.save(artifact_a, idempotency_key="same-artifact") == artifact_a
            assert await artifacts.save(artifact_b, idempotency_key="same-artifact") == artifact_b
            assert await artifacts.get_by_idempotency_key(owner_id, "same-artifact") is None

            reservation_a = Reservation(
                id=uuid4(),
                organisation_id=ORG_A,
                bench_id="shared-bench",
                owner="owner-a",
                status=ReservationStatus.CANCELLED,
                created_at=NOW,
                idempotency_key="same-reservation",
            )
            reservation_b = reservation_a.model_copy(
                update={"id": uuid4(), "organisation_id": ORG_B, "owner": "owner-b"}
            )
            assert await reservations.create(reservation_a) == reservation_a
            assert await reservations.create(reservation_b) == reservation_b
            assert (
                await reservations.get_by_idempotency_key("shared-bench", "same-reservation")
                is None
            )
            assert (
                await reservations.get_by_idempotency_key(
                    "shared-bench", "same-reservation", organisation_id=ORG_A
                )
            ) == reservation_a
            assert (
                await reservations.get_by_idempotency_key(
                    "shared-bench", "same-reservation", organisation_id=ORG_B
                )
            ) == reservation_b

            entry_a = QueueEntry(
                organisation_id=ORG_A,
                bench_id="shared-bench",
                owner="owner-a",
                requested_duration_seconds=60,
                created_at=NOW,
                idempotency_key="same-queue",
            )
            entry_b = entry_a.model_copy(
                update={"id": uuid4(), "organisation_id": ORG_B, "owner": "owner-b"}
            )
            assert (await queue.create(entry_a)).id == entry_a.id
            assert (await queue.create(entry_b)).id == entry_b.id
            assert await queue.get_by_idempotency_key("shared-bench", "same-queue") is None
            replay_a = await queue.get_by_idempotency_key(
                "shared-bench", "same-queue", organisation_id=ORG_A
            )
            replay_b = await queue.get_by_idempotency_key(
                "shared-bench", "same-queue", organisation_id=ORG_B
            )
            assert replay_a is not None and replay_a.id == entry_a.id
            assert replay_b is not None and replay_b.id == entry_b.id
        finally:
            database.close()

    asyncio.run(scenario())


def test_v9_workflow_upgrade_preserves_rows_and_rebinds_run_and_step_tenant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade-v9-workflows.db"
    database = open_sqlite_database(path)
    run_id = uuid4()
    step_id = uuid4()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO organisations "
            "(id, slug, name, status, created_at, updated_at) "
            "VALUES (?, 'tenant-b', 'Tenant B', 'ACTIVE', ?, ?)",
            (str(ORG_B), NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO workflows "
            "(organisation_id, name, version, definition_json, created_at) "
            "VALUES (?, 'shared-workflow', 1, ?, ?)",
            (str(ORG_B), _definition(ORG_B).model_dump_json(), NOW.isoformat()),
        )
    database.close()

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            f"""
            CREATE TABLE workflows_v9 (
                name TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                PRIMARY KEY (name, version)
            );
            CREATE TABLE workflow_runs_v9 (
                id TEXT PRIMARY KEY,
                workflow_name TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                bench_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                error_message TEXT,
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                FOREIGN KEY (workflow_name, workflow_version)
                    REFERENCES workflows_v9(name, version)
            );
            CREATE TABLE workflow_step_results_v9 (
                id TEXT PRIMARY KEY,
                workflow_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                output_json TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                organisation_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANISATION_ID}',
                UNIQUE (workflow_run_id, step_index),
                FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs_v9(id) ON DELETE CASCADE
            );
            INSERT INTO workflows_v9 SELECT name, version, definition_json, created_at,
                organisation_id FROM workflows;
            INSERT INTO workflow_runs_v9 SELECT id, workflow_name, workflow_version, bench_id,
                owner, reservation_id, status, current_step, created_at, started_at,
                completed_at, error_code, error_message, organisation_id FROM workflow_runs;
            INSERT INTO workflow_step_results_v9 SELECT id, workflow_run_id, step_index, name,
                action, status, started_at, completed_at, output_json, error_code,
                error_message, artifact_ids_json, organisation_id FROM workflow_step_results;
            DROP TABLE workflow_step_results;
            DROP TABLE workflow_runs;
            DROP TABLE workflows;
            ALTER TABLE workflows_v9 RENAME TO workflows;
            ALTER TABLE workflow_runs_v9 RENAME TO workflow_runs;
            ALTER TABLE workflow_step_results_v9 RENAME TO workflow_step_results;
            CREATE UNIQUE INDEX workflow_runs_one_active_per_bench
                ON workflow_runs(bench_id)
                WHERE status IN ('pending', 'running', 'cancel_requested');
            CREATE INDEX workflow_runs_created ON workflow_runs(created_at DESC);
            CREATE INDEX workflow_step_results_run
                ON workflow_step_results(workflow_run_id, step_index);
            CREATE INDEX workflows_organisation
                ON workflows(organisation_id, name, version);
            DELETE FROM schema_migrations WHERE version = 10;
            """
        )
        connection.execute(
            "INSERT INTO workflow_runs "
            "(id, organisation_id, workflow_name, workflow_version, bench_id, owner, "
            "reservation_id, status, created_at) "
            "VALUES (?, ?, 'shared-workflow', 1, 'shared-bench', 'owner-b', ?, 'running', ?)",
            (str(run_id), DEFAULT_ORGANISATION_ID, str(uuid4()), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO workflow_step_results "
            "(id, organisation_id, workflow_run_id, step_index, name, action, status, "
            "started_at, output_json, artifact_ids_json) "
            "VALUES (?, ?, ?, 0, 'Wait', 'wait', 'running', ?, '{}', '[]')",
            (str(step_id), DEFAULT_ORGANISATION_ID, str(run_id), NOW.isoformat()),
        )
        connection.commit()

    upgraded = open_sqlite_database(path)
    try:
        with upgraded.transaction() as connection:
            assert SCHEMA_VERSION == 12
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            run_scope = connection.execute(
                "SELECT organisation_id FROM workflow_runs WHERE id = ?", (str(run_id),)
            ).fetchone()[0]
            step_scope = connection.execute(
                "SELECT organisation_id FROM workflow_step_results WHERE id = ?",
                (str(step_id),),
            ).fetchone()[0]
            assert run_scope == step_scope == str(ORG_B)
        repository = SQLiteWorkflowRepository(upgraded, initialize_schema=False)

        async def verify() -> None:
            run = await repository.get_run(run_id, organisation_id=ORG_B)
            assert run is not None and run.organisation_id == ORG_B
            [step] = await repository.list_step_results(run_id, organisation_id=ORG_B)
            assert step.id == step_id and step.organisation_id == ORG_B
            assert await repository.get_run(run_id, organisation_id=ORG_A) is None

        asyncio.run(verify())
    finally:
        upgraded.close()


def test_fresh_v10_schema_uses_tenant_primary_and_retry_keys(tmp_path: Path) -> None:
    database = open_sqlite_database(tmp_path / "fresh-v10.db")
    try:
        with database.transaction() as connection:
            workflow_pk = [
                row[1]
                for row in sorted(
                    connection.execute("PRAGMA table_info(workflows)"),
                    key=lambda row: row[5],
                )
                if row[5]
            ]
            assert workflow_pk == ["organisation_id", "name", "version"]
            mutation_pk = [
                row[1]
                for row in sorted(
                    connection.execute("PRAGMA table_info(reservation_lease_mutations)"),
                    key=lambda row: row[5],
                )
                if row[5]
            ]
            assert mutation_pk == ["organisation_id", "mutation_key"]
            expected_indexes = {
                "ci_sessions_idempotency": ["organisation_id", "idempotency_key"],
                "ci_sessions_workflow_launch_idempotency": [
                    "organisation_id",
                    "workflow_launch_idempotency_key",
                ],
                "ci_sessions_finalize_idempotency": [
                    "organisation_id",
                    "finalize_idempotency_key",
                ],
                "artifacts_idempotency": [
                    "organisation_id",
                    "owner_id",
                    "idempotency_key",
                ],
                "reservations_idempotency": [
                    "organisation_id",
                    "bench_id",
                    "idempotency_key",
                ],
                "queue_idempotency": [
                    "organisation_id",
                    "bench_id",
                    "idempotency_key",
                ],
                "distributed_ci_workflows_launch_idempotency": [
                    "organisation_id",
                    "launch_idempotency_key",
                ],
                "reservation_lease_mutations_reservation": [
                    "organisation_id",
                    "reservation_id",
                    "revision",
                    "created_at",
                ],
            }
            for index, columns in expected_indexes.items():
                assert [
                    row[2] for row in connection.execute(f"PRAGMA index_info({index})")
                ] == columns
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.close()


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


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_schema_v10_installs_identity_tables_default_organisation_and_scope_columns(
    tmp_path: Path,
) -> None:
    database = open_sqlite_database(tmp_path / "phase6.db")
    with database.transaction() as connection:
        assert SCHEMA_VERSION == 12
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
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
    database = open_sqlite_database(path)
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
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        old_definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'authorisation_snapshots'"
        ).fetchone()
        assert old_definition is not None
        assert "CI_SESSION" not in str(old_definition[0])

    repaired = open_sqlite_database(path)
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
    database = open_sqlite_database(tmp_path / "idempotent.db")
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
    database = open_sqlite_database(path)
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

    upgraded = open_sqlite_database(path)
    with upgraded.transaction() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
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
