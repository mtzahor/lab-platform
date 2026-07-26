from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

from lab_platform.agent import create_agent
from lab_platform.core import BenchRecord
from lab_platform.models import (
    ArtifactOwnerType,
    ArtifactRecord,
    BenchRequest,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupStatus,
    HealthStatus,
    Reservation,
    ReservationStatus,
)
from lab_platform.persistence import (
    SQLiteCatalogRepository,
    SQLiteCiSessionRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteGenericArtifactRepository,
    SQLiteOperationLockRepository,
    SQLiteTimedReservationRepository,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _waiting_session(index: int, request: BenchRequest) -> CiSession:
    return CiSession(
        id=UUID(int=index + 1),
        provider=CiProvider.LOCAL,
        external_run_id=f"scale-run-{index:04d}",
        repository="lab-platform/scale",
        requested_by=f"ci-worker-{index:04d}",
        status=CiSessionStatus.WAITING_FOR_BENCH,
        created_at=NOW + timedelta(microseconds=index),
        bench_request=request,
    )


def test_50_concurrent_ci_sessions_atomically_select_unique_benches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "atomic-ci-assignment.db"
    setup_database = _database(path)
    worker_databases: list[SQLiteDatabase] = []
    worker_count = 50
    request = BenchRequest(
        required_capabilities={"serial", "reset"},
        required_labels={"pool": "hardware-ci"},
        allow_physical=False,
        reservation_duration_seconds=300,
    )

    async def prepare() -> list[CiSession]:
        catalog = SQLiteCatalogRepository(setup_database)
        sessions = SQLiteCiSessionRepository(setup_database)
        await catalog.upsert_backend(
            "scale-simlab",
            "simlab",
            {"benches": 100},
            now=NOW,
        )
        await catalog.upsert_records(
            BenchRecord(
                id=f"sim-bench-{index + 1:03d}",
                backend_id="scale-simlab",
                name=f"Scale SimLab bench {index + 1:03d}",
                target_type="virtual",
                online=True,
                health=HealthStatus.HEALTHY,
                capabilities={"serial", "reset", "flash"},
                labels={"pool": "hardware-ci", "slot": f"{index + 1:03d}"},
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            for index in range(100)
        )
        created = [_waiting_session(index, request) for index in range(worker_count)]
        for session in created:
            await sessions.create(session)
        return created

    created_sessions = asyncio.run(prepare())
    barrier = Barrier(worker_count)

    try:
        worker_databases = [_database(path) for _ in range(worker_count)]
        repositories = [SQLiteCiSessionRepository(database) for database in worker_databases]

        def assign(index: int) -> tuple[CiSession, Reservation] | None:
            barrier.wait(timeout=10)
            return asyncio.run(
                repositories[index].assign_compatible_bench(
                    created_sessions[index].id,
                    request,
                    now=NOW + timedelta(seconds=1),
                )
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(assign, range(worker_count)))

        assignments = [result for result in results if result is not None]
        assert len(assignments) == worker_count
        assigned_sessions = [assignment[0] for assignment in assignments]
        reservations = [assignment[1] for assignment in assignments]
        assigned_bench_ids = [session.bench_id for session in assigned_sessions]
        assert len(set(assigned_bench_ids)) == worker_count
        assert {reservation.bench_id for reservation in reservations} == set(assigned_bench_ids)
        assert all(session.status is CiSessionStatus.RESERVED for session in assigned_sessions)

        async def verify_and_release() -> None:
            repository = SQLiteTimedReservationRepository(setup_database)
            active = await repository.list(status=ReservationStatus.ACTIVE, limit=worker_count + 1)
            assert len(active) == worker_count
            assert {reservation.id for reservation in active} == {
                reservation.id for reservation in reservations
            }
            assert await SQLiteOperationLockRepository(setup_database).list() == []

            released_at = NOW + timedelta(minutes=1)
            for reservation in active:
                await repository.update(
                    reservation.model_copy(
                        update={
                            "status": ReservationStatus.RELEASED,
                            "released_at": released_at,
                        }
                    ),
                    expected_status=ReservationStatus.ACTIVE,
                )
            assert (
                await repository.list(
                    status=ReservationStatus.ACTIVE,
                    limit=worker_count + 1,
                )
                == []
            )

        asyncio.run(verify_and_release())
    finally:
        for database in worker_databases:
            database.close()
        setup_database.close()


def test_100_bench_simlab_cleans_up_50_simultaneous_ci_sessions(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (tmp_path / "agent.yaml").write_text(
        """agent:
  log_level: ERROR
plugins: []
backends:
  - id: scale-simlab
    type: simlab
    config:
      benches: 100
      bench_prefix: sim-scale
      speed_multiplier: 1000
      flash_duration_seconds: 0
      labels:
        pool: hardware-ci
database:
  url: sqlite:///./scale-runtime.db
artifacts:
  directory: ./artifacts
ci:
  heartbeat_interval_seconds: 30
  heartbeat_timeout_seconds: 120
  cleanup_timeout_seconds: 5
  reaper_poll_interval_seconds: 3600
scheduler:
  poll_interval_seconds: 3600
workflows:
  definitions_directory: ./workflows
""",
        encoding="utf-8",
    )
    agent = create_agent(tmp_path)

    async def scenario() -> None:
        await agent.start()
        try:
            request = BenchRequest(
                required_labels={"pool": "hardware-ci"},
                allow_simulated=True,
                allow_physical=False,
                reservation_duration_seconds=300,
            )
            started_at = time.monotonic()
            sessions = await asyncio.gather(
                *(
                    agent.ci_session_service.create(
                        provider=CiProvider.LOCAL,
                        external_run_id=f"simlab-scale-{index:03d}",
                        requested_by=f"scale-worker-{index:03d}",
                        bench_request=request,
                    )
                    for index in range(50)
                )
            )
            assert {session.bench_id for session in sessions} == {
                f"sim-scale-{index:03d}" for index in range(1, 51)
            }

            completed = await asyncio.gather(
                *(agent.ci_session_service.cancel(session.id) for session in sessions)
            )
            assert time.monotonic() - started_at < 30
            assert all(session.status is CiSessionStatus.COMPLETED for session in completed)
            assert all(session.cleanup_status is CleanupStatus.SUCCEEDED for session in completed)
            assert (
                await agent.reservation_service.list(
                    status=ReservationStatus.ACTIVE,
                    limit=101,
                )
                == []
            )
            assert await agent._operation_locks.list() == []
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_500_waiting_ci_sessions_survive_a_database_restart(tmp_path: Path) -> None:
    path = tmp_path / "waiting-ci-sessions.db"
    request = BenchRequest(
        required_capabilities={"serial"},
        required_labels={"pool": "hardware-ci"},
    )
    expected = [_waiting_session(index, request) for index in range(500)]
    database = _database(path)
    try:
        repository = SQLiteCiSessionRepository(database)

        async def persist() -> None:
            for session in expected:
                await repository.create(session)

        asyncio.run(persist())
    finally:
        database.close()

    reopened = _database(path)
    try:
        persisted = asyncio.run(
            SQLiteCiSessionRepository(reopened).list(
                status=CiSessionStatus.WAITING_FOR_BENCH,
                limit=501,
            )
        )
        assert len(persisted) == 500
        assert {session.id for session in persisted} == {session.id for session in expected}
        assert all(session.bench_request == request for session in persisted)
        assert persisted[0].created_at > persisted[-1].created_at
    finally:
        reopened.close()


def test_1000_generic_artifact_metadata_rows_survive_a_database_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact-metadata.db"
    owner_id = UUID(int=50_000)
    database = _database(path)
    try:
        repository = SQLiteGenericArtifactRepository(database)

        async def persist() -> None:
            for index in range(1_000):
                await repository.save(
                    ArtifactRecord(
                        id=UUID(int=60_000 + index),
                        owner_type=ArtifactOwnerType.CI_SESSION,
                        owner_id=owner_id,
                        name=f"artifact-{index:04d}.log",
                        artifact_type="workflow-log",
                        content_type="text/plain",
                        path=f"objects/scale/artifact-{index:04d}.log",
                        size_bytes=index,
                        sha256=f"{index:064x}",
                        created_at=NOW + timedelta(microseconds=index),
                        metadata={"ordinal": str(index), "source": "scale-test"},
                    ),
                    idempotency_key=f"artifact-{index:04d}",
                )

        asyncio.run(persist())
    finally:
        database.close()

    reopened = _database(path)
    try:
        artifacts = asyncio.run(
            SQLiteGenericArtifactRepository(reopened).list_for_owner(
                ArtifactOwnerType.CI_SESSION,
                owner_id,
            )
        )
        assert len(artifacts) == 1_000
        assert [artifact.metadata["ordinal"] for artifact in artifacts] == [
            str(index) for index in range(1_000)
        ]
        assert sum(artifact.size_bytes for artifact in artifacts) == sum(range(1_000))
    finally:
        reopened.close()


def test_10000_workflow_events_are_persisted_and_queryable(tmp_path: Path) -> None:
    path = tmp_path / "workflow-events.db"
    database = _database(path)
    event_count = 10_000
    try:
        rows = []
        for index in range(event_count):
            workflow_run_id = UUID(int=100_000 + index // 10)
            rows.append(
                (
                    str(UUID(int=200_000 + index)),
                    (NOW + timedelta(microseconds=index)).isoformat(),
                    "WORKFLOW_STEP_SUCCEEDED",
                    "workflow",
                    f"sim-bench-{index % 100 + 1:03d}",
                    None,
                    None,
                    "ci-scale",
                    json.dumps(
                        {
                            "workflow_run_id": str(workflow_run_id),
                            "step_index": index % 10,
                            "ordinal": index,
                        },
                        sort_keys=True,
                    ),
                    f"workflow-event:{index}",
                )
            )
        with database.transaction(immediate=True) as connection:
            connection.executemany(
                "INSERT INTO events "
                "(id, timestamp, type, source, bench_id, reservation_id, operation_id, "
                "actor, payload, deduplication_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        database.close()

    reopened = _database(path)
    try:
        with reopened.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE source = 'workflow'"
                ).fetchone()[0]
                == event_count
            )

        events = asyncio.run(
            SQLiteEventRepository(reopened).list(
                bench_id="sim-bench-043",
                event_type="WORKFLOW_STEP_SUCCEEDED",
                limit=101,
            )
        )
        expected_ordinals = list(range(9_942, 41, -100))
        assert len(events) == 100
        assert [event.payload["ordinal"] for event in events] == expected_ordinals
        assert all(event.source == "workflow" for event in events)
    finally:
        reopened.close()
