from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from lab_platform.core import BenchNotFoundError, CapabilityNotSupportedError
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.real_backend import RealLabBackend


class FakePhysicalTarget:
    def __init__(self) -> None:
        self.version: str | None = None
        self.reset_count = 0

    @property
    def id(self) -> str:
        return "physical-01"

    @property
    def capabilities(self) -> set[str]:
        return {"firmware", "probe", "reset", "serial"}

    async def probe(self) -> TargetHealth:
        return TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type="FAKE",
            serial_port="fake://physical-01",
        )

    async def snapshot(self) -> BenchSnapshot:
        return BenchSnapshot(
            id=self.id,
            name="Fake Physical Target",
            status=BenchStatus.AVAILABLE,
            online=True,
            powered=None,
            firmware_version=self.version,
            capabilities=sorted(self.capabilities),
        )

    async def flash(self, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]:
        yield BackendProgress(percent=20, message="Connecting")
        self.version = firmware.version
        yield BackendProgress(percent=100, message="Complete")

    async def read_serial(self, request: SerialReadRequest) -> AsyncIterator[SerialLine]:
        yield SerialLine(text="READY")

    async def reset(self) -> None:
        self.reset_count += 1


@pytest.mark.backend_contract
def test_real_lab_backend_contract(tmp_path: Path) -> None:
    async def contract() -> None:
        target = FakePhysicalTarget()
        backend = RealLabBackend({target.id: target})
        await backend.start()
        await backend.start()
        try:
            benches = await backend.list_benches()
            assert [bench.id for bench in benches] == ["physical-01"]
            assert await backend.get_bench("physical-01") == benches[0]
            assert (await backend.probe("physical-01")).status is TargetHealthStatus.ONLINE
            with pytest.raises(BenchNotFoundError):
                await backend.get_bench("missing")

            for action in (backend.power_on, backend.power_off, backend.power_cycle):
                with pytest.raises(CapabilityNotSupportedError):
                    await action("physical-01")

            await backend.reset("physical-01")
            assert target.reset_count == 1
            firmware = FirmwareInput(
                filename="contract.bin",
                local_path=tmp_path / "contract.bin",
                sha256="e" * 64,
                size_bytes=8,
                version="9.0.0",
            )
            progress = [item async for item in backend.flash_firmware(target.id, firmware)]
            assert [item.percent for item in progress] == [20, 100]
            assert (await backend.get_bench(target.id)).firmware_version == "9.0.0"
            lines = [
                line.text async for line in backend.read_serial(target.id, SerialReadRequest())
            ]
            assert lines == ["READY"]
        finally:
            await backend.stop()
            await backend.stop()

    asyncio.run(contract())
