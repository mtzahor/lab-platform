from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab_platform.core.backend_registry import BackendRegistry
from lab_platform.core.bench_catalog import BenchCatalog, BenchMetadata, BenchRecord
from lab_platform.core.clock import FakeClock
from lab_platform.core.errors import BenchNotFoundError
from lab_platform.models import HealthStatus, TargetHealthStatus
from lab_platform.persistence.catalog import (
    BackendRegistration,
    SQLiteCatalogRepository,
    _coerce_backend_registration,
)
from lab_platform.persistence.database import SQLiteDatabase
from pydantic import ValidationError

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _backend(
    backend_id: str,
    *,
    backend_type: str = "simlab",
    updated_at: datetime = NOW,
) -> BackendRegistration:
    return BackendRegistration(
        id=backend_id,
        type=backend_type,
        config={"benches": 2},
        created_at=NOW,
        updated_at=updated_at,
    )


def _record(
    bench_id: str,
    backend_id: str,
    *,
    online: bool = True,
    health: HealthStatus = HealthStatus.HEALTHY,
    target_type: str | None = "esp32",
    capabilities: set[str] | None = None,
    labels: dict[str, str] | None = None,
    updated_at: datetime = NOW,
) -> BenchRecord:
    return BenchRecord(
        id=bench_id,
        backend_id=backend_id,
        name=f"Bench {bench_id}",
        target_type=target_type,
        online=online,
        health=health,
        capabilities=capabilities if capabilities is not None else {"Firmware", "Probe"},
        labels=labels if labels is not None else {"site": "north", "team": "platform"},
        last_seen_at=updated_at if online else None,
        created_at=NOW,
        updated_at=updated_at,
    )


def test_backend_registration_batches_and_argument_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "backend-batches.db")
        database.initialize()
        repository = SQLiteCatalogRepository(database)
        try:
            registrations = [_backend("z-backend"), _backend("a-backend")]
            assert [item.id for item in await repository.upsert_backends(registrations)] == [
                "a-backend",
                "z-backend",
            ]
            assert await repository.upsert_backends([]) == await repository.list_backends()
            assert await repository.get_backend("missing") is None

            updated = _backend(
                "a-backend", backend_type="real", updated_at=NOW + timedelta(minutes=1)
            )
            persisted = await repository.upsert_backend(updated)
            assert persisted.type == "real"
            assert persisted.created_at == NOW
            assert persisted.updated_at == NOW + timedelta(minutes=1)

            with pytest.raises(TypeError, match="cannot be combined"):
                _coerce_backend_registration(updated, "real", None, None)
            with pytest.raises(TypeError, match="backend_type is required"):
                _coerce_backend_registration("missing-type", None, None, None)
            with pytest.raises(ValidationError, match="timezone-aware"):
                _backend("naive", updated_at=datetime(2026, 7, 20, 12))
        finally:
            database.close()

    asyncio.run(scenario())


def test_catalog_batches_filters_reconcile_and_metadata_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "catalog-edges.db")
        database.initialize()
        repository = SQLiteCatalogRepository(database)
        try:
            await repository.upsert_backends(
                [_backend("sim"), _backend("physical", backend_type="real")]
            )
            alpha = _record("alpha", "sim")
            beta = _record(
                "beta",
                "physical",
                online=False,
                health=HealthStatus.WARNING,
                target_type=None,
                capabilities={"Serial"},
                labels={"site": "south"},
            )
            assert await repository.upsert_records([beta, alpha]) == [alpha, beta]
            assert await repository.upsert_records([]) == [alpha, beta]
            assert await repository.get_record("missing") is None
            assert await repository.load_records() == [alpha, beta]

            assert await repository.list_records(
                online=True,
                health=HealthStatus.HEALTHY,
                backend_id="sim",
                target_type="ESP32",
                capability="firmware",
                capabilities=("PROBE",),
                labels={"team": "platform"},
            ) == [alpha]
            assert await repository.list_records(online=False) == [beta]
            assert await repository.list_records(health=HealthStatus.UNHEALTHY) == []
            assert await repository.list_records(backend_id="missing") == []
            assert await repository.list_records(target_type="esp32", online=False) == []
            assert await repository.list_records(capability="power") == []
            assert await repository.list_records(labels={"site": "west"}) == []

            observed = NOW + timedelta(minutes=2)
            refreshed_alpha = alpha.model_copy(update={"updated_at": observed})
            reconciled = await repository.reconcile(
                [refreshed_alpha],
                reconciled_backend_ids={"sim", "physical"},
                observed_at=observed,
            )
            assert reconciled[0] == refreshed_alpha
            assert reconciled[1].health is HealthStatus.UNHEALTHY
            assert reconciled[1].updated_at == observed
            assert await repository.reconcile([], reconciled_backend_ids=()) == reconciled

            with pytest.raises(BenchNotFoundError):
                await repository.save_metadata("missing", BenchMetadata())
            with pytest.raises(BenchNotFoundError):
                await repository.set_metadata("missing", labels={"team": "nobody"})

            replaced = await repository.set_metadata(
                "alpha",
                name="Primary bench",
                labels={"purpose": "smoke"},
                replace_labels=True,
                updated_at=observed + timedelta(minutes=1),
            )
            assert replaced == BenchMetadata(
                name="Primary bench",
                target_type="esp32",
                labels={"purpose": "smoke"},
            )
            merged = await repository.set_metadata(
                "alpha", labels={"owner": "qa"}, updated_at=observed + timedelta(minutes=2)
            )
            assert merged.labels == {"purpose": "smoke", "owner": "qa"}
            assert (await repository.get_record("alpha")).name == "Primary bench"  # type: ignore[union-attr]

            naive = alpha.model_copy(
                update={
                    "id": "naive",
                    "created_at": datetime(2026, 7, 20, 12),
                    "updated_at": datetime(2026, 7, 20, 12),
                }
            )
            with pytest.raises(ValueError, match="timezone-aware"):
                await repository.upsert_record(naive)
        finally:
            database.close()

    asyncio.run(scenario())


def test_catalog_reports_corrupted_persisted_labels(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "catalog-corrupt.db")
        database.initialize()
        repository = SQLiteCatalogRepository(database)
        try:
            await repository.upsert_backend(_backend("sim"))
            await repository.upsert_record(_record("alpha", "sim"))
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE bench_catalog SET labels_json = ? WHERE id = ?",
                    ('{"priority": 1}', "alpha"),
                )
            with pytest.raises(ValueError, match="map strings to strings"):
                await repository.load_records()
            with pytest.raises(ValueError, match="map strings to strings"):
                await repository.load_metadata()
        finally:
            database.close()

    asyncio.run(scenario())


def test_bench_catalog_restore_merges_persisted_and_configured_metadata() -> None:
    clock = FakeClock(NOW)
    catalog = BenchCatalog(
        BackendRegistry({}),
        clock=clock,
        metadata={
            "alpha": BenchMetadata(
                target_type="configured-target",
                labels={"team": "configured", "purpose": "testing"},
            ),
            "configured-only": BenchMetadata(name="Configured only"),
        },
    )
    record = _record("alpha", "sim", health=HealthStatus.WARNING)
    catalog.restore(
        [record],
        metadata={
            "alpha": BenchMetadata(
                name="Persisted name",
                target_type="persisted-target",
                labels={"team": "persisted", "site": "north"},
            )
        },
    )

    assert catalog.get("alpha") == record
    assert catalog.metadata_for("alpha") == BenchMetadata(
        name="Persisted name",
        target_type="configured-target",
        labels={"team": "configured", "site": "north", "purpose": "testing"},
    )
    assert catalog.metadata_for("configured-only") == BenchMetadata(name="Configured only")
    assert catalog.list(health=HealthStatus.HEALTHY) == []

    updated = catalog.set_metadata("alpha", labels=None)
    assert updated.labels == {"team": "configured", "site": "north", "purpose": "testing"}
    assert catalog.get("alpha").updated_at == NOW

    offline = catalog.record_probe_result("alpha", TargetHealthStatus.OFFLINE)
    assert not offline.online
    assert offline.health is HealthStatus.UNHEALTHY
    assert catalog.requires_probe("alpha")

    degraded = catalog.record_probe_result("alpha", TargetHealthStatus.DEGRADED)
    assert not degraded.online
    assert degraded.health is HealthStatus.WARNING
    assert catalog.requires_probe("alpha")

    online = catalog.record_probe_result("alpha", TargetHealthStatus.ONLINE)
    assert online.online
    assert online.health is HealthStatus.HEALTHY
    assert online.last_seen_at == NOW
    assert not catalog.requires_probe("alpha")
