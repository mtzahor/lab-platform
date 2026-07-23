from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lab_platform.agent.runtime import create_backend_registry
from lab_platform.config import PlatformConfig
from lab_platform.core.backend import LabBackend
from lab_platform.core.backend_registry import (
    BackendRegistry,
    DuplicateBackendIdError,
    DuplicateBenchIdError,
)
from lab_platform.core.bench_catalog import BenchCatalog, BenchMetadata
from lab_platform.core.clock import FakeClock
from lab_platform.core.errors import BackendNotFoundError, BenchNotFoundError, ConfigurationError
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    HealthStatus,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.simlab_adapter import SimLabBackend


class FakeBackend:
    def __init__(self, *snapshots: BenchSnapshot) -> None:
        self.snapshots = list(snapshots)
        self.actions: list[tuple[str, str]] = []
        self.fail_inventory = False
        self.fail_get = False
        self.fail_start = False
        self.fail_stop = False
        self.started = False

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("start unavailable")
        self.started = True

    async def stop(self) -> None:
        if self.fail_stop:
            raise RuntimeError("stop unavailable")
        self.started = False

    async def list_benches(self) -> list[BenchSnapshot]:
        if self.fail_inventory:
            raise RuntimeError("inventory unavailable")
        return list(self.snapshots)

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        if self.fail_get:
            raise RuntimeError("backend unavailable")
        return next(snapshot for snapshot in self.snapshots if snapshot.id == bench_id)

    async def power_on(self, bench_id: str) -> None:
        self.actions.append(("power_on", bench_id))

    async def power_off(self, bench_id: str) -> None:
        self.actions.append(("power_off", bench_id))

    async def power_cycle(self, bench_id: str) -> None:
        self.actions.append(("power_cycle", bench_id))

    async def reset(self, bench_id: str) -> None:
        self.actions.append(("reset", bench_id))

    async def probe(self, bench_id: str) -> TargetHealth:
        self.actions.append(("probe", bench_id))
        return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.ONLINE)

    async def flash_firmware(
        self,
        bench_id: str,
        firmware: FirmwareInput,
    ) -> AsyncIterator[BackendProgress]:
        self.actions.append(("flash", bench_id))
        yield BackendProgress(percent=100, message=firmware.filename)

    async def read_serial(
        self,
        bench_id: str,
        request: SerialReadRequest,
    ) -> AsyncIterator[SerialLine]:
        self.actions.append(("serial", bench_id))
        yield SerialLine(text=f"timeout={request.timeout_seconds}")


class NaiveClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 20, 10)


def _snapshot(
    bench_id: str,
    *,
    name: str | None = None,
    online: bool = True,
    capabilities: tuple[str, ...] = ("Firmware", "Serial"),
) -> BenchSnapshot:
    return BenchSnapshot(
        id=bench_id,
        name=name or bench_id,
        status=BenchStatus.ONLINE if online else BenchStatus.OFFLINE,
        online=online,
        powered=True,
        capabilities=list(capabilities),
    )


def test_registry_routes_mixed_backends_through_one_lab_backend() -> None:
    async def exercise() -> None:
        simulated = FakeBackend(_snapshot("bench-01"), _snapshot("bench-02"))
        physical = FakeBackend(_snapshot("esp32-devkit-01"))
        registry = BackendRegistry({"virtual-lab": simulated, "local-hardware": physical})
        composite: LabBackend = registry

        await composite.start()
        assert [bench.id for bench in await composite.list_benches()] == [
            "bench-01",
            "bench-02",
            "esp32-devkit-01",
        ]
        assert await registry.get_backend_id_for_bench("bench-02") == "virtual-lab"
        assert await registry.get_backend_id_for_bench("esp32-devkit-01") == "local-hardware"

        await registry.power_on("bench-01")
        await registry.reset("esp32-devkit-01")
        assert simulated.actions == [("power_on", "bench-01")]
        assert physical.actions == [("reset", "esp32-devkit-01")]

        await composite.stop()
        assert not simulated.started
        assert not physical.started

    asyncio.run(exercise())


def test_multiple_simlab_instances_can_use_distinct_bench_prefixes() -> None:
    async def exercise() -> None:
        registry = BackendRegistry(
            {
                "team-a": SimLabBackend(bench_count=2, bench_prefix="team-a"),
                "team-b": SimLabBackend(bench_count=2, bench_prefix="team-b"),
            }
        )
        await registry.start()
        try:
            assert [bench.id for bench in await registry.list_benches()] == [
                "team-a-01",
                "team-a-02",
                "team-b-01",
                "team-b-02",
            ]
        finally:
            await registry.stop()

    asyncio.run(exercise())


def test_simlab_auto_start_false_keeps_instance_out_of_inventory() -> None:
    async def exercise() -> None:
        config = PlatformConfig.model_validate(
            {
                "backends": [
                    {
                        "id": "manual-simlab",
                        "type": "simlab",
                        "config": {
                            "benches": 2,
                            "bench_prefix": "manual",
                            "auto_start": False,
                        },
                    }
                ]
            }
        )
        registry = create_backend_registry(config)

        await registry.start()
        try:
            assert await registry.list_benches() == []
        finally:
            await registry.stop()

    asyncio.run(exercise())


def test_registry_rejects_duplicate_backend_and_bench_ids() -> None:
    first = FakeBackend(_snapshot("bench-shared"))
    second = FakeBackend(_snapshot("bench-shared"))

    with pytest.raises(DuplicateBackendIdError):
        BackendRegistry([("duplicate", first), ("duplicate", second)])
    with pytest.raises(ConfigurationError, match="cannot be empty"):
        BackendRegistry({" ": first})

    async def discover() -> None:
        registry = BackendRegistry({"first": first, "second": second})
        with pytest.raises(DuplicateBenchIdError) as conflict:
            await registry.refresh()
        assert conflict.value.code == "BENCH_ID_CONFLICT"
        assert registry.ownership == {}

    asyncio.run(discover())


def test_registry_rejects_duplicates_within_one_backend_and_stale_ownership() -> None:
    async def exercise() -> None:
        repeated = BackendRegistry({"repeated": FakeBackend(_snapshot("same"), _snapshot("same"))})
        with pytest.raises(DuplicateBenchIdError, match="more than once"):
            await repeated.refresh()

        first = FakeBackend(_snapshot("moving-bench"))
        second = FakeBackend(_snapshot("other-bench"))
        registry = BackendRegistry({"first": first, "second": second})
        await registry.refresh()
        first.fail_inventory = True
        second.snapshots = [_snapshot("moving-bench")]
        with pytest.raises(DuplicateBenchIdError, match="exposed by both"):
            await registry.refresh()
        assert registry.ownership == {
            "moving-bench": "first",
            "other-bench": "second",
        }

    asyncio.run(exercise())


def test_registry_routes_all_protocol_operations_and_reports_lifecycle_failures(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        backend = FakeBackend(_snapshot("bench-01"))
        registry = BackendRegistry({"sim": backend})
        assert registry.backend_ids == ("sim",)
        assert registry.get_backend("sim") is backend
        with pytest.raises(BackendNotFoundError, match="not registered") as missing:
            registry.get_backend("missing")
        assert missing.value.code == "BACKEND_NOT_FOUND"

        await registry.refresh()
        assert registry.last_refresh.refreshed_backend_ids == {"sim"}
        await registry.power_off("bench-01")
        assert (await registry.probe("bench-01")).status is TargetHealthStatus.ONLINE
        firmware = FirmwareInput(
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            sha256="a" * 64,
            size_bytes=1,
        )
        assert [item.percent async for item in registry.flash_firmware("bench-01", firmware)] == [
            100
        ]
        assert [
            item.text
            async for item in registry.read_serial("bench-01", SerialReadRequest(timeout_seconds=2))
        ] == ["timeout=2.0"]
        with pytest.raises(BenchNotFoundError):
            await registry.get_backend_for_bench("missing")
        assert backend.actions == [
            ("power_off", "bench-01"),
            ("probe", "bench-01"),
            ("flash", "bench-01"),
            ("serial", "bench-01"),
        ]

        failing = FakeBackend(_snapshot("unavailable"))
        failing.fail_start = True
        failing.fail_inventory = True
        isolated = BackendRegistry({"good": backend, "failing": failing})
        await isolated.start()
        assert [(item.backend_id, item.stage) for item in isolated.lifecycle_failures] == [
            ("failing", "start"),
            ("failing", "refresh"),
        ]
        failing.fail_stop = True
        await isolated.stop()
        assert [(item.backend_id, item.stage) for item in isolated.lifecycle_failures] == [
            ("failing", "stop")
        ]

    asyncio.run(exercise())


def test_one_backend_inventory_failure_does_not_break_other_backends() -> None:
    async def exercise() -> None:
        simulated = FakeBackend(_snapshot("bench-01"))
        physical = FakeBackend(_snapshot("esp32-devkit-01"))
        registry = BackendRegistry({"sim": simulated, "physical": physical})
        await registry.refresh()

        physical.fail_inventory = True
        refresh = await registry.refresh()
        assert [item.snapshot.id for item in refresh.benches] == ["bench-01"]
        assert refresh.failed_backend_ids == {"physical"}
        assert registry.ownership == {
            "bench-01": "sim",
            "esp32-devkit-01": "physical",
        }

        await registry.power_cycle("bench-01")
        assert simulated.actions == [("power_cycle", "bench-01")]
        physical.fail_get = True
        with pytest.raises(RuntimeError, match="backend unavailable"):
            await registry.get_bench("esp32-devkit-01")
        assert (await registry.get_bench("bench-01")).online

    asyncio.run(exercise())


def test_catalog_filters_and_preserves_platform_metadata_across_refreshes() -> None:
    async def exercise() -> None:
        clock = FakeClock(datetime(2026, 7, 20, 10, tzinfo=UTC))
        simulated = FakeBackend(
            _snapshot("bench-01", name="Backend name", capabilities=("Firmware", "Serial"))
        )
        physical = FakeBackend(_snapshot("esp32-devkit-01", capabilities=("Firmware", "Probe")))
        registry = BackendRegistry({"virtual": simulated, "physical": physical})
        catalog = BenchCatalog(
            registry,
            clock=clock,
            metadata={
                "bench-01": BenchMetadata(
                    name="Team virtual bench",
                    target_type="virtual",
                    labels={"location": "simulation", "purpose": "testing"},
                ),
                "esp32-devkit-01": BenchMetadata(
                    target_type="esp32",
                    labels={"location": "home-lab", "board": "esp32"},
                ),
            },
        )

        initial = await catalog.refresh()
        assert initial.discovered_bench_ids == {"bench-01", "esp32-devkit-01"}
        assert [record.id for record in catalog.list(capability="firmware")] == [
            "bench-01",
            "esp32-devkit-01",
        ]
        assert [record.id for record in catalog.list(labels={"board": "esp32"})] == [
            "esp32-devkit-01"
        ]
        assert [record.id for record in catalog.list(target_type="VIRTUAL")] == ["bench-01"]
        assert [record.id for record in catalog.list(online=True, backend_id="virtual")] == [
            "bench-01"
        ]
        assert [
            record.id
            for record in catalog.list(
                health=HealthStatus.HEALTHY,
                capabilities=("firmware", "probe"),
            )
        ] == ["esp32-devkit-01"]
        assert catalog.list(online=False) == []
        assert catalog.list(backend_id="missing") == []
        assert catalog.list(target_type="missing") == []
        assert catalog.list(capability="power") == []
        assert catalog.list(labels={"board": "missing"}) == []
        assert catalog.is_online("bench-01")
        assert not catalog.is_online("unknown")
        with pytest.raises(BenchNotFoundError):
            catalog.get("unknown")

        updated_metadata = catalog.set_metadata(
            "bench-01",
            name="Renamed by platform",
            target_type="virtual-target",
            labels={"purpose": "smoke"},
            replace_labels=True,
        )
        assert updated_metadata.labels == {"purpose": "smoke"}
        assert catalog.metadata_for("bench-01") == updated_metadata
        assert catalog.metadata_for("unknown") == BenchMetadata()

        created_at = catalog.get("bench-01").created_at
        clock.advance(minutes=1)
        simulated.snapshots = [
            _snapshot("bench-01", name="Renamed by backend", capabilities=("Power",))
        ]
        await catalog.refresh()
        refreshed = catalog.get("bench-01")
        assert refreshed.name == "Renamed by platform"
        assert refreshed.target_type == "virtual-target"
        assert refreshed.labels == {"purpose": "smoke"}
        assert refreshed.capabilities == {"Power"}
        assert refreshed.created_at == created_at
        assert refreshed.updated_at == clock.now()

    asyncio.run(exercise())


def test_catalog_marks_failed_or_missing_backend_benches_offline() -> None:
    async def exercise() -> None:
        clock = FakeClock(datetime(2026, 7, 20, 10, tzinfo=UTC))
        simulated = FakeBackend(_snapshot("bench-01"))
        physical = FakeBackend(_snapshot("esp32-devkit-01"))
        catalog = BenchCatalog(
            BackendRegistry({"virtual": simulated, "physical": physical}),
            clock=clock,
        )
        await catalog.refresh()
        physical_last_seen = catalog.get("esp32-devkit-01").last_seen_at

        clock.advance(minutes=1)
        physical.fail_inventory = True
        failed = await catalog.refresh()
        assert failed.offline_bench_ids == {"esp32-devkit-01"}
        assert failed.backend_failures[0].backend_id == "physical"
        offline = catalog.get("esp32-devkit-01")
        assert not offline.online
        assert offline.health is HealthStatus.UNHEALTHY
        assert offline.last_seen_at == physical_last_seen
        assert catalog.get("bench-01").online

        clock.advance(minutes=1)
        simulated.snapshots = []
        missing = await catalog.refresh()
        assert "bench-01" in missing.offline_bench_ids
        assert not catalog.is_online("bench-01")

    asyncio.run(exercise())


def test_catalog_rejects_a_naive_clock() -> None:
    async def exercise() -> None:
        catalog = BenchCatalog(
            BackendRegistry({"sim": FakeBackend(_snapshot("bench-01"))}),
            clock=NaiveClock(),
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            await catalog.refresh()

    asyncio.run(exercise())
