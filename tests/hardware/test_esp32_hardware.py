from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from lab_platform.config import load_config
from lab_platform.models import FirmwareInput, SerialReadRequest, TargetHealthStatus
from lab_platform.real_backend import RealLabBackend
from lab_platform.real_backend.errors import FirmwareVerificationFailedError


def _settings() -> tuple[RealLabBackend, str]:
    if os.environ.get("LAB_PLATFORM_ENABLE_HARDWARE_TESTS") != "1":
        pytest.skip("set LAB_PLATFORM_ENABLE_HARDWARE_TESTS=1 to enable physical flashing")
    bench_id = os.environ.get("LAB_PLATFORM_HARDWARE_BENCH")
    if not bench_id:
        pytest.skip("set LAB_PLATFORM_HARDWARE_BENCH to the one allowed physical bench")
    config_path = Path(os.environ.get("LAB_PLATFORM_HARDWARE_CONFIG", "examples/esp32-local.yaml"))
    config = load_config(config_path)
    configured = [bench.id for bench in config.hardware.benches]
    if configured != [bench_id]:
        pytest.fail(
            f"LAB_PLATFORM_HARDWARE_BENCH={bench_id!r} does not exactly match {configured!r}"
        )
    return RealLabBackend.from_config(config.hardware), bench_id


@pytest.mark.hardware
def test_esp32_discovery_and_probe() -> None:
    async def scenario() -> None:
        backend, bench_id = _settings()
        await backend.start()
        try:
            health = await backend.probe(bench_id)
            assert health.status is TargetHealthStatus.ONLINE
            assert health.serial_port
            assert health.chip_type and "ESP32" in health.chip_type.upper()
        finally:
            await backend.stop()

    asyncio.run(scenario())


@pytest.mark.hardware
def test_esp32_flash_boot_reset_and_serial() -> None:
    firmware_path = os.environ.get("LAB_PLATFORM_HARDWARE_FIRMWARE")
    if not firmware_path:
        pytest.skip("set LAB_PLATFORM_HARDWARE_FIRMWARE to a known-good raw ESP32 binary")
    path = Path(firmware_path)
    content = path.read_bytes()
    firmware = FirmwareInput(
        filename=path.name,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        version=os.environ.get("LAB_PLATFORM_HARDWARE_FIRMWARE_VERSION", "hardware-test"),
    )

    async def scenario() -> None:
        backend, bench_id = _settings()
        await backend.start()
        try:
            progress = [item async for item in backend.flash_firmware(bench_id, firmware)]
            assert progress[-1].percent == 100
            boot_lines = [line.text for item in progress for line in item.serial_lines]
            assert boot_lines[-1] == "READY"
            await backend.reset(bench_id)
            lines = [
                line.text
                async for line in backend.read_serial(
                    bench_id,
                    SerialReadRequest(timeout_seconds=20, until_pattern="^READY$"),
                )
            ]
            assert "READY" in lines

            invalid = firmware.model_copy(update={"sha256": "0" * 64})
            with pytest.raises(FirmwareVerificationFailedError):
                async for _ in backend.flash_firmware(bench_id, invalid):
                    pass
        finally:
            await backend.stop()

    asyncio.run(scenario())


@pytest.mark.hardware
def test_unplugged_esp32_is_offline() -> None:
    if os.environ.get("LAB_PLATFORM_EXPECT_UNPLUGGED") != "1":
        pytest.skip(
            "set LAB_PLATFORM_EXPECT_UNPLUGGED=1 only after unplugging the configured board"
        )

    async def scenario() -> None:
        backend, bench_id = _settings()
        await backend.start()
        try:
            assert (await backend.probe(bench_id)).status is TargetHealthStatus.OFFLINE
        finally:
            await backend.stop()

    asyncio.run(scenario())
