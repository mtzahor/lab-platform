from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from lab_platform.core import (
    BenchNotFoundError,
    CapabilityNotSupportedError,
    LabBackend,
    SimulationFailureError,
)
from lab_platform.models import FirmwareInput
from lab_platform.simlab_adapter import SimLabBackend


@dataclass(frozen=True)
class BackendHarness:
    backend: LabBackend
    inject_failure: Callable[[str, str], None]


@pytest.fixture
def backend_harness() -> BackendHarness:
    backend = SimLabBackend(
        bench_count=2,
        speed_multiplier=1000,
        flash_duration_seconds=1,
    )
    return BackendHarness(backend, backend.simulator.inject_failure)


@pytest.mark.backend_contract
def test_lab_backend_contract(backend_harness: BackendHarness, tmp_path: Path) -> None:
    async def contract() -> None:
        backend = backend_harness.backend
        await backend.start()
        try:
            benches = await backend.list_benches()
            assert [bench.id for bench in benches] == ["bench-01", "bench-02"]
            assert await backend.get_bench("bench-01") == benches[0]
            with pytest.raises(BenchNotFoundError):
                await backend.get_bench("missing")

            await backend.power_off("bench-01")
            assert (await backend.get_bench("bench-01")).powered is False
            await backend.power_on("bench-01")
            await backend.power_cycle("bench-01")
            assert (await backend.get_bench("bench-01")).powered is True

            firmware = FirmwareInput(
                filename="contract.bin",
                local_path=tmp_path / "contract.bin",
                sha256="e" * 64,
                size_bytes=8,
                version="9.0.0",
            )
            progress = [item async for item in backend.flash_firmware("bench-01", firmware)]
            assert [item.percent for item in progress] == [0, 20, 40, 60, 80, 100]
            assert (await backend.get_bench("bench-01")).firmware_version == "9.0.0"
            with pytest.raises(CapabilityNotSupportedError):
                async for _ in backend.flash_firmware("bench-02", firmware):
                    pass

            backend_harness.inject_failure("bench-01", "flash_failure")
            with pytest.raises(SimulationFailureError):
                async for _ in backend.flash_firmware("bench-01", firmware):
                    pass
        finally:
            await backend.stop()

    asyncio.run(contract())
