from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab_platform.core import BenchMetadata, BenchRecord
from lab_platform.models import HealthStatus
from lab_platform.persistence import SCHEMA_VERSION, SQLiteCatalogRepository, SQLiteDatabase

NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


def _record(
    *,
    name: str = "Virtual bench",
    capabilities: set[str] | None = None,
    labels: dict[str, str] | None = None,
    online: bool = True,
    health: HealthStatus = HealthStatus.HEALTHY,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
    last_seen_at: datetime | None = NOW,
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
        ] == [2, 3, 4, 5, 6, 7, 8]
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
            (NOW.isoformat(), NOW.isoformat(), '{"recovered":1}'),
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
            == 8
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
            "virtual-lab", "simlab", {"bench_count": 1}, now=NOW
        )
        updated_backend = await repository.upsert_backend(
            "virtual-lab",
            "simlab",
            {"bench_count": 2, "clock_mode": "manual"},
            now=NOW + timedelta(minutes=1),
        )
        assert updated_backend.created_at == first_backend.created_at
        assert updated_backend.updated_at == NOW + timedelta(minutes=1)
        assert updated_backend.config["bench_count"] == 2
        assert len(await repository.list_backends()) == 1

        original = await repository.upsert_record(_record())
        refreshed_at = NOW + timedelta(minutes=2)
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
            updated_at=NOW + timedelta(minutes=3),
        )
        assert metadata == BenchMetadata(
            name="Platform display name",
            target_type="virtual-target",
            labels={"location": "simulation", "purpose": "smoke"},
        )

        latest_at = NOW + timedelta(minutes=4)
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

        offline_at = NOW + timedelta(minutes=5)
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
