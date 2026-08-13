from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    ReservationSource,
)
from lab_platform.models.workflows import (
    WorkflowAction,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence import (
    SCHEMA_VERSION,
    SQLiteApiTokenRepository,
    SQLiteCiSessionRepository,
    SQLiteDatabase,
    SQLiteGenericArtifactRepository,
    SQLiteWorkflowRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _session(
    *,
    created_at: datetime = NOW,
    status: CiSessionStatus = CiSessionStatus.CREATED,
    external_run_id: str | None = None,
) -> CiSession:
    return CiSession(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id=external_run_id or str(uuid4()),
        repository="openai/lab-platform",
        ref="refs/pull/42/head",
        commit_sha="a" * 40,
        actor="octocat",
        requested_by="github-actions",
        status=status,
        created_at=created_at,
    )


def _artifact(owner_id: UUID, *, name: str = "serial.log") -> ArtifactRecord:
    return ArtifactRecord(
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=owner_id,
        name=name,
        artifact_type="serial-log",
        content_type="text/plain",
        path=f"objects/{uuid4()}/{name}",
        size_bytes=12,
        sha256="a" * 64,
        created_at=NOW,
        metadata={"encoding": "utf-8"},
    )


def _downgrade_workflow_steps_and_remove_phase4(path: Path) -> tuple[UUID, UUID]:
    run_id = uuid4()
    step_id = uuid4()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            f"""
            INSERT INTO workflows(name, version, definition_json)
            VALUES ('legacy', 1,
                    '{{"name":"legacy","version":1,"requirements":'
                    || '{{"capabilities":[],"labels":{{}}}},'
                    || '"steps":[{{"action":"wait","seconds":1}}]}}');
            INSERT INTO workflow_runs(
                id, workflow_name, workflow_version, bench_id, owner, reservation_id,
                status, current_step, created_at
            ) VALUES (
                '{run_id}', 'legacy', 1, 'legacy-bench', 'alice', '{uuid4()}',
                'running', 0, '{NOW.isoformat()}'
            );
            INSERT INTO workflow_step_results(
                id, workflow_run_id, step_index, name, action, status, started_at,
                completed_at, output_json, error_code, error_message, artifact_ids_json
            ) VALUES (
                '{step_id}', '{run_id}', 0, 'Legacy wait', 'wait', 'running',
                '{NOW.isoformat()}', NULL, '{{"legacy":true}}', NULL, NULL,
                '["{uuid4()}"]'
            );

            DROP INDEX workflow_step_results_run;
            ALTER TABLE workflow_step_results RENAME TO workflow_step_results_phase4_source;
            CREATE TABLE workflow_step_results (
                id TEXT PRIMARY KEY,
                workflow_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL CHECK (step_index >= 0),
                action TEXT NOT NULL CHECK (
                    action IN (
                        'flash', 'reset', 'read_serial', 'assert_serial', 'wait', 'probe'
                    )
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'succeeded', 'failed', 'cancelled')
                ),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                output_json TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                UNIQUE (workflow_run_id, step_index),
                FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
            );
            INSERT INTO workflow_step_results(
                id, workflow_run_id, step_index, action, status, started_at, completed_at,
                output_json, error_code, error_message
            )
            SELECT id, workflow_run_id, step_index, action, status, started_at, completed_at,
                   output_json, error_code, error_message
            FROM workflow_step_results_phase4_source;
            DROP TABLE workflow_step_results_phase4_source;
            CREATE INDEX workflow_step_results_run
                ON workflow_step_results(workflow_run_id, step_index);

            DROP TABLE ci_cleanup_results;
            DROP TABLE ci_sessions;
            DROP TABLE artifacts;
            DROP TABLE api_tokens;
            DELETE FROM schema_migrations WHERE version = 5;
            """
        )
    return run_id, step_id


def test_v4_upgrade_preserves_workflow_rows_and_installs_phase4_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.db"
    database = _database(path)
    database.close()
    run_id, step_id = _downgrade_workflow_steps_and_remove_phase4(path)

    database = _database(path)
    try:
        with database.transaction() as connection:
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                == SCHEMA_VERSION
                == 10
            )
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"api_tokens", "ci_sessions", "ci_cleanup_results", "artifacts"} <= tables
            columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info(workflow_step_results)")
            }
            assert {"name", "artifact_ids_json"} <= columns.keys()
            assert columns["started_at"][3] == 0

        async def verify() -> None:
            repository = SQLiteWorkflowRepository(database)
            [legacy] = await repository.list_step_results(run_id)
            assert legacy.id == step_id
            assert legacy.name == ""
            assert legacy.artifact_ids == []
            assert legacy.output == {"legacy": True}
            pending = WorkflowStepResult(
                workflow_run_id=run_id,
                step_index=1,
                name="Queued assertion",
                action=WorkflowAction.ASSERT_SERIAL,
                status=WorkflowStepStatus.PENDING,
                started_at=None,
                artifact_ids=[uuid4()],
            )
            await repository.create_step_result(pending)
            assert (await repository.list_step_results(run_id))[1] == pending

        asyncio.run(verify())
    finally:
        database.close()


def test_api_tokens_round_trip_update_revoke_and_monotonic_last_use(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "tokens.db")
        repository = SQLiteApiTokenRepository(database)
        token = ApiToken(
            name="github",
            token_hash="b" * 64,
            owner="github-actions",
            scopes={ApiTokenScope.CI_SESSIONS, ApiTokenScope.ARTIFACTS_WRITE},
            created_at=NOW,
        )
        try:
            assert await repository.create(token) == token
            assert await repository.get_by_hash(token.token_hash) == token
            assert await repository.lookup(token.token_hash) == token
            renamed = token.model_copy(update={"name": "github-main"})
            assert await repository.update(renamed) == renamed
            used = await repository.update_last_used(token.id, NOW + timedelta(minutes=2))
            assert used is not None and used.last_used_at == NOW + timedelta(minutes=2)
            used = await repository.update_last_used(token.id, NOW + timedelta(minutes=1))
            assert used is not None and used.last_used_at == NOW + timedelta(minutes=2)
            revoked = await repository.revoke(token.id, NOW + timedelta(minutes=3))
            assert revoked is not None and revoked.revoked_at == NOW + timedelta(minutes=3)
            assert await repository.update_last_used(token.id, NOW + timedelta(minutes=4)) is None
            assert await repository.revoke(token.id, NOW + timedelta(minutes=4)) == revoked
            assert await repository.list(owner="github-actions", include_revoked=False) == []
            assert await repository.list(owner="github-actions") == [revoked]
            with pytest.raises(LookupError, match="does not exist"):
                await repository.update(token.model_copy(update={"id": uuid4()}))
            assert await repository.delete(token.id)
            assert not await repository.delete(token.id)
        finally:
            database.close()

    asyncio.run(scenario())


def test_generic_artifacts_are_idempotent_and_listed_by_typed_owner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "artifacts.db")
        repository = SQLiteGenericArtifactRepository(database)
        owner_id = uuid4()
        first = _artifact(owner_id)
        retry = _artifact(owner_id, name="retry.log")
        try:
            assert await repository.save(first, idempotency_key="serial") == first
            assert await repository.save(retry, idempotency_key="serial") == first
            assert await repository.get(first.id) == first
            assert await repository.get_by_idempotency_key(owner_id, "serial") == first
            assert await repository.list_for_owner(ArtifactOwnerType.CI_SESSION, owner_id) == [
                first
            ]
            assert await repository.list_for_owner(ArtifactOwnerType.WORKFLOW_RUN, owner_id) == []
            assert await repository.delete(first.id)
            assert await repository.get(first.id) is None
        finally:
            database.close()

    asyncio.run(scenario())


def test_ci_session_cas_staleness_cleanup_and_launch_finalize_idempotency(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "ci-sessions.db")
        repository = SQLiteCiSessionRepository(database)
        session = _session()
        duplicate = _session(external_run_id="different")
        try:
            assert await repository.create(session, idempotency_key="create:42") == session
            assert await repository.create(duplicate, idempotency_key="create:42") == session
            waiting = session.model_copy(
                update={
                    "status": CiSessionStatus.WAITING_FOR_BENCH,
                    "heartbeat_at": NOW + timedelta(seconds=30),
                }
            )
            assert (
                await repository.compare_and_set(
                    waiting, expected_statuses=[CiSessionStatus.CREATED]
                )
                == waiting
            )
            assert (
                await repository.compare_and_set(
                    session, expected_statuses=[CiSessionStatus.CREATED]
                )
                is None
            )

            stale = _session(created_at=NOW - timedelta(minutes=10))
            await repository.create(stale)
            assert await repository.list_stale(
                heartbeat_before=NOW - timedelta(minutes=2), now=NOW
            ) == [stale]
            assert await repository.append_error(session.id, "transient backend failure")
            assert await repository.errors(session.id) == ["transient backend failure"]

            reserved = waiting.model_copy(update={"status": CiSessionStatus.RESERVED})
            assert await repository.update(reserved) == reserved
            workflow_run_id = uuid4()
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO workflows(name, version, definition_json) VALUES (?, ?, ?)",
                    (
                        "ci-smoke",
                        1,
                        '{"name":"ci-smoke","version":1,"requirements":'
                        '{"capabilities":[],"labels":{}},'
                        '"steps":[{"action":"wait","seconds":1}]}',
                    ),
                )
                connection.execute(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_name, workflow_version, bench_id, owner, reservation_id, "
                    "status, current_step, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(workflow_run_id),
                        "ci-smoke",
                        1,
                        "bench-ci",
                        "github-actions",
                        str(uuid4()),
                        "pending",
                        None,
                        NOW.isoformat(),
                    ),
                )
            running = await repository.attach_workflow_run(
                session.id,
                workflow_run_id,
                idempotency_key="launch:42",
                started_at=NOW,
            )
            assert running is not None
            assert running.status is CiSessionStatus.RUNNING
            assert running.workflow_run_id == workflow_run_id
            assert (
                await repository.attach_workflow_run(
                    session.id,
                    uuid4(),
                    idempotency_key="launch:42",
                    started_at=NOW + timedelta(minutes=1),
                )
                == running
            )

            final = running.model_copy(
                update={
                    "status": CiSessionStatus.COMPLETED,
                    "outcome": CiOutcome.SUCCEEDED,
                    "cleanup_status": CleanupStatus.SUCCEEDED,
                    "completed_at": NOW + timedelta(minutes=2),
                }
            )
            assert await repository.mark_finalized(final, idempotency_key="finalize:42") == final
            assert await repository.mark_finalized(running, idempotency_key="finalize:42") == final
            cleanup = CleanupResult(
                reservation_released=True,
                workflow_stopped=True,
                locks_released=True,
                serial_closed=True,
                artifacts_finalized=False,
                errors=["artifact upload unavailable"],
            )
            assert await repository.save_cleanup(session.id, cleanup) == cleanup
            assert await repository.get_cleanup(session.id) == cleanup
        finally:
            database.close()

    asyncio.run(scenario())


def _insert_catalog_bench(
    database: SQLiteDatabase,
    bench_id: str,
    *,
    backend_id: str,
    backend_type: str,
    labels: str,
) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO backend_registrations "
            "(id, type, config_json, created_at, updated_at) VALUES (?, ?, '{}', ?, ?)",
            (backend_id, backend_type, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO bench_catalog "
            "(id, backend_id, name, target_type, online, health, capabilities_json, "
            "labels_json, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'esp32', 1, 'healthy', ?, ?, ?, ?, ?)",
            (
                bench_id,
                backend_id,
                bench_id,
                '["firmware","serial","reset"]',
                labels,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _insert_history(database: SQLiteDatabase, bench_id: str, activated_at: datetime) -> None:
    ended_at = activated_at + timedelta(minutes=10)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO reservations "
            "(id, bench_id, owner, created_at, released_at, status, requested_at, "
            "starts_at, ends_at, activated_at, expired_at, source, metadata, "
            "idempotency_key, release_pending) "
            "VALUES (?, ?, 'history', ?, ?, 'released', ?, ?, ?, ?, NULL, 'system', "
            "'{}', NULL, 0)",
            (
                str(uuid4()),
                bench_id,
                activated_at.isoformat(),
                ended_at.isoformat(),
                activated_at.isoformat(),
                activated_at.isoformat(),
                ended_at.isoformat(),
                activated_at.isoformat(),
            ),
        )


def _release_assignment(database: SQLiteDatabase, reservation_id: UUID) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE reservations SET status = 'released', released_at = ? WHERE id = ?",
            (NOW.isoformat(), str(reservation_id)),
        )


def test_atomic_selection_uses_preference_then_lru_and_explicit_bench(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "selection-order.db")
        repository = SQLiteCiSessionRepository(database)
        _insert_catalog_bench(
            database,
            "bench-a",
            backend_id="physical",
            backend_type="real",
            labels='{"board":"esp32","tier":"other"}',
        )
        _insert_catalog_bench(
            database,
            "bench-b",
            backend_id="simulated",
            backend_type="simlab",
            labels='{"board":"esp32","tier":"preferred"}',
        )
        _insert_history(database, "bench-a", NOW - timedelta(hours=3))
        _insert_history(database, "bench-b", NOW - timedelta(hours=1))
        first = _session(external_run_id="selection-1")
        second = _session(external_run_id="selection-2")
        third = _session(external_run_id="selection-3")
        try:
            for item in (first, second, third):
                await repository.create(item)
            preferred = BenchRequest(
                required_capabilities={"firmware", "serial"},
                required_labels={"board": "esp32"},
                preferred_labels={"tier": "preferred"},
                allow_physical=True,
            )
            first_assignment = await repository.assign_compatible_bench(
                first.id, preferred, now=NOW
            )
            assert first_assignment is not None
            first_session, first_reservation = first_assignment
            assert first_session.bench_id == first_reservation.bench_id == "bench-b"
            assert first_reservation.source is ReservationSource.CI
            _release_assignment(database, first_reservation.id)

            second_assignment = await repository.assign_compatible_bench(
                second.id,
                BenchRequest(
                    required_capabilities={"firmware", "serial"},
                    required_labels={"board": "esp32"},
                    allow_physical=True,
                ),
                now=NOW + timedelta(minutes=1),
            )
            assert second_assignment is not None
            second_session, second_reservation = second_assignment
            assert second_session.bench_id == second_reservation.bench_id == "bench-a"
            _release_assignment(database, second_reservation.id)

            explicit_assignment = await repository.assign_compatible_bench(
                third.id,
                BenchRequest(
                    explicit_bench_id="bench-b",
                    required_capabilities={"serial"},
                    required_labels={"board": "esp32"},
                ),
                now=NOW + timedelta(minutes=2),
            )
            assert explicit_assignment is not None
            assert explicit_assignment[0].bench_id == "bench-b"
        finally:
            database.close()

    asyncio.run(scenario())


def test_concurrent_atomic_selection_never_double_assigns_a_bench(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selection-race.db"
    database = _database(path)
    _insert_catalog_bench(
        database,
        "bench-a",
        backend_id="simulated",
        backend_type="simlab",
        labels='{"board":"esp32"}',
    )
    _insert_catalog_bench(
        database,
        "bench-b",
        backend_id="simulated",
        backend_type="simlab",
        labels='{"board":"esp32"}',
    )
    sessions = (_session(external_run_id="race-1"), _session(external_run_id="race-2"))

    async def seed() -> None:
        repository = SQLiteCiSessionRepository(database)
        for session in sessions:
            await repository.create(session)

    asyncio.run(seed())
    database.close()
    barrier = Barrier(2)

    def assign(session_id: UUID) -> str | None:
        worker_database = _database(path)
        try:
            repository = SQLiteCiSessionRepository(worker_database)
            barrier.wait()
            result = asyncio.run(
                repository.assign_compatible_bench(
                    session_id,
                    BenchRequest(
                        required_capabilities={"serial"},
                        required_labels={"board": "esp32"},
                    ),
                    now=NOW,
                )
            )
            return result[0].bench_id if result is not None else None
        finally:
            worker_database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        selected = list(executor.map(assign, (sessions[0].id, sessions[1].id)))

    assert all(item is not None for item in selected)
    assert sorted(item for item in selected if item is not None) == ["bench-a", "bench-b"]
    database = _database(path)
    try:
        with database.transaction() as connection:
            active = connection.execute(
                "SELECT bench_id, COUNT(*) FROM reservations WHERE status = 'active' "
                "GROUP BY bench_id ORDER BY bench_id"
            ).fetchall()
        assert [(row[0], row[1]) for row in active] == [("bench-a", 1), ("bench-b", 1)]
    finally:
        database.close()
