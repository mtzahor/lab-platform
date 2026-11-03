from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from lab_platform.core import (
    BackendTimeoutError,
    BenchMetadata,
    BenchNotFoundError,
    BenchOperationInProgressError,
    BenchRecord,
    CapabilityNotSupportedError,
    SimulationFailureError,
)
from lab_platform.models import (
    BackendProgress,
    EventRecord,
    FirmwareArtifact,
    FirmwareInput,
    HealthStatus,
    InvalidOperationTransition,
    Operation,
    OperationStatus,
    OperationType,
    Reservation,
    ReservationStatus,
)
from lab_platform.persistence import (
    SCHEMA_VERSION,
    SQLiteArtifactRepository,
    SQLiteCatalogRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationRepository,
    SQLiteReservationRepository,
)
from lab_platform.simlab import SimLab
from lab_platform.simlab_adapter import SimLabBackend, map_simlab_bench
from pydantic import ValidationError

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def test_operation_model_enforces_state_machine_and_firmware_metadata() -> None:
    pending = Operation.pending("bench-01", OperationType.FLASH_FIRMWARE, "michael", now=NOW)
    assert pending.status is OperationStatus.PENDING
    assert pending.progress == 0
    with pytest.raises(InvalidOperationTransition):
        pending.transition(OperationStatus.SUCCEEDED, now=NOW)

    running = pending.transition(OperationStatus.RUNNING, now=NOW + timedelta(seconds=1))
    requested = running.transition(
        OperationStatus.CANCEL_REQUESTED,
        now=NOW + timedelta(seconds=2),
    )
    succeeded = requested.transition(
        OperationStatus.SUCCEEDED,
        now=NOW + timedelta(seconds=3),
        message="completed before cancellation",
    )
    assert succeeded.progress == 100
    assert succeeded.completed_at == NOW + timedelta(seconds=3)

    running_again = pending.transition(OperationStatus.RUNNING, now=NOW)
    failed = running_again.transition(
        OperationStatus.FAILED,
        now=NOW,
        error_code="BACKEND_FAILURE",
        error_message="boom",
    )
    assert failed.error_code == "BACKEND_FAILURE"
    cancelled = pending.transition(OperationStatus.CANCELLED, now=NOW)
    assert cancelled.status is OperationStatus.CANCELLED

    with pytest.raises(ValidationError):
        FirmwareInput(
            filename="bad.bin",
            local_path=Path("bad.bin"),
            sha256="bad",
            size_bytes=1,
        )
    with pytest.raises(ValidationError):
        FirmwareInput(
            filename="empty.bin",
            local_path=Path("empty.bin"),
            sha256="0" * 64,
            size_bytes=0,
        )


def test_sqlite_repositories_are_atomic_filterable_and_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "lab.db"
        database = SQLiteDatabase(path)
        database.initialize()
        database.initialize()
        reservations = SQLiteReservationRepository(database)
        operations = SQLiteOperationRepository(database)
        events = SQLiteEventRepository(database)
        artifacts = SQLiteArtifactRepository(database)

        first = Reservation(
            id=uuid4(),
            bench_id="bench-01",
            owner="alice",
            created_at=NOW,
        )
        assert await reservations.create(first) == first
        contender = first.model_copy(update={"id": uuid4(), "owner": "bob"})
        assert await reservations.create(contender) == first
        assert await reservations.get_active("bench-01") == first
        released = first.model_copy(
            update={"status": ReservationStatus.RELEASED, "released_at": NOW}
        )
        await reservations.release(released)
        assert await reservations.get_active("bench-01") is None

        power = Operation.pending("bench-01", OperationType.POWER_CYCLE, "alice", now=NOW)
        await operations.create(power)
        with pytest.raises(BenchOperationInProgressError):
            await operations.create(
                Operation.pending("bench-01", OperationType.POWER_ON, "alice", now=NOW)
            )
        running = power.transition(OperationStatus.RUNNING, now=NOW)
        completed = running.transition(OperationStatus.SUCCEEDED, now=NOW)
        await operations.update(running)
        await operations.update(completed)
        assert await operations.get(power.id) == completed
        assert await operations.list(
            bench_id="bench-01",
            status=OperationStatus.SUCCEEDED,
            operation_type=OperationType.POWER_CYCLE,
            limit=1,
        ) == [completed]

        interrupted = Operation.pending("bench-02", OperationType.POWER_OFF, "bob", now=NOW)
        await operations.create(interrupted)
        assert await operations.recover_incomplete(NOW + timedelta(minutes=1)) == 1
        recovered = await operations.get(interrupted.id)
        assert recovered is not None
        assert recovered.status is OperationStatus.FAILED
        assert recovered.error_code == "AGENT_RESTARTED"

        event = EventRecord(
            timestamp=NOW,
            type="POWER_CYCLE_COMPLETED",
            source="platform",
            bench_id="bench-01",
            operation_id=power.id,
            actor="alice",
            payload={"status": "succeeded"},
        )
        await events.create(event)
        assert await events.list(
            bench_id="bench-01",
            event_type="POWER_CYCLE_COMPLETED",
            after=NOW - timedelta(seconds=1),
            before=NOW + timedelta(seconds=1),
            limit=1,
        ) == [event]
        assert await events.list(event_type="missing") == []

        artifact = FirmwareArtifact(
            sha256="a" * 64,
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            size_bytes=4,
            version="1.0",
            created_at=NOW,
        )
        assert await artifacts.save(artifact) == artifact
        updated_artifact = await artifacts.save(artifact.model_copy(update={"version": "2.0"}))
        assert updated_artifact.version == "2.0"
        database.close()
        database.close()

        connection = sqlite3.connect(path)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        connection.close()

    asyncio.run(scenario())


def test_simlab_mapping_and_backend_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = SimLabBackend(
            bench_count=3,
            clock_mode="manual",
            flash_duration_seconds=5,
        )
        await backend.start()
        benches = await backend.list_benches()
        assert [bench.id for bench in benches] == ["bench-01", "bench-02", "bench-03"]
        assert benches[0].capabilities == [
            "power",
            "serial",
            "firmware",
            "flash",
            "probe",
            "reset",
        ]
        assert (await backend.get_bench("bench-02")).powered is False
        with pytest.raises(BenchNotFoundError):
            await backend.get_bench("missing")

        await backend.power_off("bench-01")
        assert (await backend.get_bench("bench-01")).powered is False
        await backend.power_on("bench-01")
        cycle = asyncio.create_task(backend.power_cycle("bench-01"))
        await asyncio.sleep(0)
        assert (await backend.get_bench("bench-01")).powered is False
        backend.simulator.tick(2)
        await cycle
        assert (await backend.get_bench("bench-01")).powered is True

        firmware = FirmwareInput(
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            sha256="b" * 64,
            size_bytes=3,
            version="2.1.0",
        )
        with pytest.raises(CapabilityNotSupportedError):
            async for _ in backend.flash_firmware("bench-02", firmware):
                pass

        backend.simulator.inject_failure("bench-01", "flash_failure")
        with pytest.raises(SimulationFailureError):
            progress = backend.flash_firmware("bench-01", firmware)
            assert (await anext(progress)).percent == 0
            for expected in (20, 40):
                next_progress: asyncio.Future[BackendProgress] = asyncio.ensure_future(
                    anext(progress)
                )
                await asyncio.sleep(0)
                backend.simulator.tick(1)
                assert (await next_progress).percent == expected
            failed_progress: asyncio.Future[BackendProgress] = asyncio.ensure_future(
                anext(progress)
            )
            await asyncio.sleep(0)
            backend.simulator.tick(1)
            await failed_progress

        backend.simulator.inject_failure("bench-01", "backend_timeout")
        with pytest.raises(BackendTimeoutError):
            await backend.power_off("bench-01")
        await backend.stop()

        raw = SimLab(bench_count=1)
        await raw.start()
        internal = raw.get_bench("bench-01")
        shared = map_simlab_bench(internal)
        assert shared.id == "bench-01"
        with pytest.raises(FrozenInstanceError):
            internal.powered = False  # type: ignore[misc]
        await raw.shutdown()

    asyncio.run(scenario())


NOW_CATALOG_PERSISTENCE = datetime(2026, 7, 20, 10, tzinfo=UTC)


def _record(
    *,
    name: str = "Virtual bench",
    capabilities: set[str] | None = None,
    labels: dict[str, str] | None = None,
    online: bool = True,
    health: HealthStatus = HealthStatus.HEALTHY,
    created_at: datetime = NOW_CATALOG_PERSISTENCE,
    updated_at: datetime = NOW_CATALOG_PERSISTENCE,
    last_seen_at: datetime | None = NOW_CATALOG_PERSISTENCE,
) -> BenchRecord:
    return BenchRecord(
        id="bench-01",
        backend_id="virtual-lab",
        name=name,
        target_type="virtual",
        online=online,
        health=health,
        capabilities=capabilities or {"firmware", "serial"},
        labels=labels or {"location": "simulation"},
        last_seen_at=last_seen_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_schema_v6_migrates_phase2_data_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "phase2.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations(version, applied_at)
            VALUES (2, '2026-07-01T00:00:00+00:00');
        CREATE TABLE reservations (
            id TEXT PRIMARY KEY,
            bench_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            created_at TEXT NOT NULL,
            released_at TEXT,
            status TEXT NOT NULL
        );
        INSERT INTO reservations(id, bench_id, owner, created_at, status)
            VALUES ('reservation-1', 'bench-legacy', 'alice',
                    '2026-07-01T08:00:00+00:00', 'active');
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            bench_id TEXT,
            operation_id TEXT,
            actor TEXT,
            payload TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    database = SQLiteDatabase(path)
    database.initialize()
    with database.transaction() as migrated:
        assert [
            row[0]
            for row in migrated.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == list(range(2, SCHEMA_VERSION + 1))
        legacy = migrated.execute(
            "SELECT bench_id, owner, status FROM reservations WHERE id = 'reservation-1'"
        ).fetchone()
        assert legacy is not None
        assert tuple(legacy) == ("bench-legacy", "alice", "active")
        assert {row[1] for row in migrated.execute("PRAGMA table_info(bench_catalog)")} >= {
            "id",
            "backend_id",
            "name",
            "target_type",
            "online",
            "health",
            "capabilities_json",
            "labels_json",
            "last_seen_at",
            "created_at",
            "updated_at",
        }
        assert {
            row[0]
            for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } >= {"backend_registrations", "bench_catalog"}
    database.close()


def test_schema_v6_upgrades_an_existing_phase3_database(tmp_path: Path) -> None:
    path = tmp_path / "phase3.db"
    initial = SQLiteDatabase(path)
    initial.initialize()
    with initial.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO recovery_records(started_at, completed_at, report) VALUES (?, ?, ?)",
            (
                NOW_CATALOG_PERSISTENCE.isoformat(),
                NOW_CATALOG_PERSISTENCE.isoformat(),
                '{"recovered":1}',
            ),
        )
    initial.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE bench_catalog")
    connection.execute("DROP TABLE backend_registrations")
    connection.execute("DELETE FROM schema_migrations WHERE version = 4")
    connection.commit()
    connection.close()

    upgraded = SQLiteDatabase(path)
    upgraded.initialize()
    with upgraded.transaction() as connection:
        assert (
            connection.execute("SELECT report FROM recovery_records").fetchone()[0]
            == '{"recovered":1}'
        )
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert connection.execute("SELECT COUNT(*) FROM backend_registrations").fetchone()[0] == 0
    upgraded.close()


def test_catalog_repository_survives_restart_and_preserves_metadata(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "catalog.db"
        database = SQLiteDatabase(path)
        database.initialize()
        repository = SQLiteCatalogRepository(database)

        first_backend = await repository.upsert_backend(
            "virtual-lab", "simlab", {"bench_count": 1}, now=NOW_CATALOG_PERSISTENCE
        )
        updated_backend = await repository.upsert_backend(
            "virtual-lab",
            "simlab",
            {"bench_count": 2, "clock_mode": "manual"},
            now=NOW_CATALOG_PERSISTENCE + timedelta(minutes=1),
        )
        assert updated_backend.created_at == first_backend.created_at
        assert updated_backend.updated_at == NOW_CATALOG_PERSISTENCE + timedelta(minutes=1)
        assert updated_backend.config["bench_count"] == 2
        assert len(await repository.list_backends()) == 1

        original = await repository.upsert_record(_record())
        refreshed_at = NOW_CATALOG_PERSISTENCE + timedelta(minutes=2)
        backend_refresh = _record(
            name="Name from refreshed backend",
            capabilities={"power", "probe"},
            labels={"location": "backend-must-not-replace-this"},
            created_at=refreshed_at,
            updated_at=refreshed_at,
            last_seen_at=refreshed_at,
        )
        refreshed = await repository.upsert_record(backend_refresh)
        assert refreshed.name == "Name from refreshed backend"
        assert refreshed.capabilities == {"power", "probe"}
        assert refreshed.labels == {"location": "simulation"}
        assert refreshed.created_at == original.created_at

        metadata = await repository.set_metadata(
            "bench-01",
            name="Platform display name",
            target_type="virtual-target",
            labels={"purpose": "smoke"},
            updated_at=NOW_CATALOG_PERSISTENCE + timedelta(minutes=3),
        )
        assert metadata == BenchMetadata(
            name="Platform display name",
            target_type="virtual-target",
            labels={"location": "simulation", "purpose": "smoke"},
        )

        latest_at = NOW_CATALOG_PERSISTENCE + timedelta(minutes=4)
        latest = await repository.upsert_record(
            _record(
                name="Another backend name",
                capabilities={"firmware", "probe"},
                labels={"owner": "backend"},
                created_at=latest_at,
                updated_at=latest_at,
                last_seen_at=latest_at,
            )
        )
        assert latest.name == "Platform display name"
        assert latest.target_type == "virtual-target"
        assert latest.labels == {"location": "simulation", "purpose": "smoke"}
        assert await repository.list_records(
            online=True,
            target_type="VIRTUAL-TARGET",
            capabilities=("FIRMWARE", "probe"),
            labels={"purpose": "smoke"},
        ) == [latest]

        offline_at = NOW_CATALOG_PERSISTENCE + timedelta(minutes=5)
        reconciled = await repository.reconcile(
            [], reconciled_backend_ids={"virtual-lab"}, observed_at=offline_at
        )
        assert len(reconciled) == 1
        assert not reconciled[0].online
        assert reconciled[0].health is HealthStatus.UNHEALTHY
        assert reconciled[0].last_seen_at == latest_at
        assert reconciled[0].updated_at == offline_at
        database.close()

        restarted_database = SQLiteDatabase(path)
        restarted_database.initialize()
        restarted = SQLiteCatalogRepository(restarted_database)
        assert (await restarted.load_records()) == reconciled
        assert await restarted.load_metadata() == {
            "bench-01": BenchMetadata(
                name="Platform display name",
                target_type="virtual-target",
                labels={"location": "simulation", "purpose": "smoke"},
            )
        }
        persisted_backend = await restarted.get_backend("virtual-lab")
        assert persisted_backend is not None
        assert persisted_backend.config == {"bench_count": 2, "clock_mode": "manual"}
        restarted_database.close()

    asyncio.run(exercise())
