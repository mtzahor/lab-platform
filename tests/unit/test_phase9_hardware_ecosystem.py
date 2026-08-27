from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from lab_platform.config import (
    HardwareBenchSettings,
    HardwareSettings,
    JLinkSettings,
    Nrf52Settings,
    OpenOcdSettings,
    Rp2040Settings,
)
from lab_platform.core.errors import ConfigurationError
from lab_platform.core.resources import ResourceCatalog
from lab_platform.models import (
    BenchComposition,
    BenchResourceBinding,
    BenchStatus,
    FirmwareInput,
    HardwareResource,
    ResourceBindingRole,
    ResourceHealthStatus,
    ResourceSharingMode,
    TargetHealthStatus,
)
from lab_platform.plugin_sdk import (
    CanCapability,
    CanFilter,
    CanFrame,
    CaptureCapability,
    CaptureRequest,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    FirmwareArtifact,
    MeasureCapability,
    MeasureOptions,
)
from lab_platform.plugin_sdk.testing import FakeDeviceDriver, FakeFlashCapability
from lab_platform.plugins.can import MemoryCanTransport, SocketCanDriver
from lab_platform.plugins.composed_backend import ComposedHardwareBackend
from lab_platform.plugins.instruments import InstrumentDriver, MemoryInstrumentTransport
from lab_platform.plugins.power import MemoryRelayTransport, UsbRelayDriver
from lab_platform.real_backend import RealLabBackend
from lab_platform.real_backend.process_runner import (
    OutputCallback,
    ProcessResult,
    ProcessRunner,
)
from lab_platform.real_backend.targets.jlink import (
    build_jlink_args,
    build_jlink_flash_script,
)
from lab_platform.real_backend.targets.nrf52 import build_nrfjprog_flash_args
from lab_platform.real_backend.targets.openocd import (
    build_openocd_flash_args,
    build_openocd_probe_args,
)
from lab_platform.real_backend.targets.registry import (
    TargetDriverRegistry,
    default_target_registry,
)
from lab_platform.real_backend.targets.rp2040 import build_picotool_flash_args


class RecordingRunner(ProcessRunner):
    def __init__(self, results: Sequence[ProcessResult] = ()) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self._results = list(results)

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        self.calls.append((tuple(args), timeout_seconds))
        if self._results:
            return self._results.pop(0)
        return ProcessResult(0, ("target halted due to debug-request",), ())


class HealthReportingDriver(FakeDeviceDriver):
    def __init__(self, descriptor: DeviceDescriptor, status: DeviceHealthStatus) -> None:
        super().__init__(descriptor)
        self._status = status

    async def health(self) -> DeviceHealth:
        return DeviceHealth(status=self._status, message=self._status.value)


def _stm32_bench() -> HardwareBenchSettings:
    return HardwareBenchSettings(
        id="stm32-nucleo-01",
        name="STM32 Nucleo F446RE",
        target_type="stm32",
        openocd=OpenOcdSettings(
            executable="openocd",
            interface_config="interface/stlink.cfg",
            target_config="target/stm32f4x.cfg",
        ),
    )


def test_openocd_commands_use_argv_and_validate_embedded_paths(tmp_path: Path) -> None:
    settings = OpenOcdSettings()
    probe = build_openocd_probe_args(settings)

    assert probe[:5] == [
        "openocd",
        "-f",
        "interface/stlink.cfg",
        "-f",
        "target/stm32f4x.cfg",
    ]
    assert "shell" not in probe

    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"firmware")
    firmware = FirmwareInput(
        filename="firmware.bin",
        local_path=firmware_path,
        sha256=hashlib.sha256(b"firmware").hexdigest(),
        size_bytes=8,
        version="1.0.0",
    )
    flash = build_openocd_flash_args(settings, firmware)
    assert flash[-4:] == [
        "-c",
        f"program {{{firmware_path.resolve()}}} verify reset",
        "-c",
        "shutdown",
    ]


def test_stm32_runs_through_the_same_real_backend_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = RecordingRunner()
        backend = RealLabBackend.from_config(
            HardwareSettings(benches=[_stm32_bench()]),
            process_runner=runner,
        )
        await backend.start()
        health = await backend.probe("stm32-nucleo-01")
        assert health.status is TargetHealthStatus.ONLINE
        snapshot = await backend.get_bench("stm32-nucleo-01")
        assert {"probe", "reset", "flash", "debug", "serial"}.issubset(snapshot.capabilities)

        content = b"stm32 firmware"
        path = tmp_path / "stm32.bin"
        path.write_bytes(content)
        updates = [
            update
            async for update in backend.flash_firmware(
                "stm32-nucleo-01",
                FirmwareInput(
                    filename=path.name,
                    local_path=path,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    version="1.0.0",
                ),
            )
        ]
        assert updates[-1].percent == 100
        assert updates[-1].firmware_version == "1.0.0"
        assert all(
            isinstance(argument, str) for call, _timeout in runner.calls for argument in call
        )

    asyncio.run(scenario())


def test_target_registry_rejects_unknown_target_cleanly() -> None:
    registry = TargetDriverRegistry()
    bench = HardwareBenchSettings(id="unknown", name="Unknown", target_type="mystery")

    with pytest.raises(ConfigurationError, match="Unsupported physical target type"):
        # Dependencies are not inspected before registry resolution.
        registry.create(bench, None)  # type: ignore[arg-type]


def test_starter_target_registry_and_tool_commands_are_explicit_argv(tmp_path: Path) -> None:
    registry = default_target_registry()
    assert {
        "esp32",
        "stm32",
        "openocd",
        "rp2040",
        "pico",
        "nrf52",
        "jlink",
    }.issubset(registry.target_types)

    content = b"portable firmware"
    path = tmp_path / "portable firmware.hex"
    path.write_bytes(content)
    firmware = FirmwareInput(
        filename=path.name,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    assert build_picotool_flash_args(Rp2040Settings(), firmware) == [
        "picotool",
        "load",
        str(path.resolve()),
        "-f",
    ]
    assert build_nrfjprog_flash_args(Nrf52Settings(), firmware)[1:3] == [
        "--program",
        str(path.resolve()),
    ]
    script = build_jlink_flash_script(firmware)
    assert script[0] == f'loadfile "{path.resolve()}"'
    args = build_jlink_args(JLinkSettings(), tmp_path / "commands.jlink")
    assert args[0] == "JLinkExe"
    assert args[-2] == "-CommanderScript"


def test_rp2040_and_nrf52_share_the_real_backend_contract() -> None:
    async def scenario() -> None:
        runner = RecordingRunner()
        backend = RealLabBackend.from_config(
            HardwareSettings(
                benches=[
                    HardwareBenchSettings(
                        id="pico-01",
                        name="Raspberry Pi Pico",
                        target_type="rp2040",
                    ),
                    HardwareBenchSettings(
                        id="nrf52-01",
                        name="nRF52840 DK",
                        target_type="nrf52",
                    ),
                ]
            ),
            process_runner=runner,
        )
        await backend.start()
        snapshots = await backend.list_benches()
        assert {item.id for item in snapshots} == {"pico-01", "nrf52-01"}
        assert all(
            {"probe", "flash", "reset", "serial"}.issubset(item.capabilities) for item in snapshots
        )
        assert {call[0][0] for call in runner.calls} == {"picotool", "nrfjprog"}

    asyncio.run(scenario())


def test_socketcan_and_instrument_contract_paths() -> None:
    async def scenario() -> None:
        matching = CanFrame(arbitration_id=0x123, data=b"\x01\x00\xff")
        can_transport = MemoryCanTransport(
            [matching, CanFrame(arbitration_id=0x456, data=b"other")]
        )
        can_driver = SocketCanDriver(interface="vcan0", transport=can_transport)
        can = cast(CanCapability, await can_driver.get_capability("can"))
        received = [frame async for frame in can.receive(CanFilter(arbitration_id=0x123))]
        assert received == [matching]
        await can.send(matching)
        assert can_transport.sent == [matching]
        capture = cast(CaptureCapability, await can_driver.get_capability("capture"))
        artifact = await capture.capture(
            CaptureRequest(duration_seconds=0.01, channels=["vcan0"], format="json")
        )
        assert artifact.metadata["frame_count"] == 2

        instrument_transport = MemoryInstrumentTransport({"voltage": (3.3, "V")})
        instrument = InstrumentDriver(
            device_id="scope-01",
            name="Reference scope",
            category="oscilloscope",
            transport=instrument_transport,
        )
        measure = cast(MeasureCapability, await instrument.get_capability("measure"))
        reading = await measure.measure("voltage", MeasureOptions(channel="CH1"))
        assert reading.value == 3.3
        assert reading.unit == "V"

    asyncio.run(scenario())


def test_composed_bench_routes_target_and_independent_power_resource(tmp_path: Path) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    async def scenario() -> None:
        agent_id = uuid4()
        flash_capability = FakeFlashCapability()
        target = FakeDeviceDriver(
            DeviceDescriptor(
                id="esp32-target-01",
                name="ESP32 target",
                type="esp32",
                capabilities={"flash"},
            ),
            {"flash": flash_capability},
        )
        relay_transport = MemoryRelayTransport()
        relay = UsbRelayDriver(
            device_id="usb-relay-003",
            name="USB relay channel 2",
            channel="2",
            transport=relay_transport,
            cycle_delay_seconds=0,
            sleep=no_sleep,
        )
        catalog = ResourceCatalog()
        catalog.upsert_resource(
            HardwareResource(
                id="esp32-target-01",
                agent_id=agent_id,
                plugin="esp32",
                type="target",
                name="ESP32 target",
                health=ResourceHealthStatus.HEALTHY,
                capabilities=frozenset({"flash"}),
            )
        )
        catalog.upsert_resource(
            HardwareResource(
                id="usb-relay-003",
                agent_id=agent_id,
                plugin="usb-relay",
                type="power",
                name="USB relay",
                health=ResourceHealthStatus.HEALTHY,
                capabilities=frozenset({"power"}),
                sharing=ResourceSharingMode.CHANNEL,
                channels=frozenset({"1", "2"}),
            )
        )
        catalog.register_bench(
            BenchComposition(
                id="esp32-validation-01",
                name="ESP32 validation",
                resources=(
                    BenchResourceBinding(
                        role=ResourceBindingRole.TARGET,
                        resource_id="esp32-target-01",
                    ),
                    BenchResourceBinding(
                        role=ResourceBindingRole.POWER,
                        resource_id="usb-relay-003",
                        channel="2",
                    ),
                ),
            )
        )
        backend = ComposedHardwareBackend(
            catalog,
            {"esp32-target-01": target, "usb-relay-003": relay},
        )
        await backend.start()
        await backend.power_cycle("esp32-validation-01")
        assert relay_transport.states == {"2": True}

        content = b"firmware"
        path = tmp_path / "firmware.bin"
        path.write_bytes(content)
        updates = [
            update
            async for update in backend.flash_firmware(
                "esp32-validation-01",
                FirmwareInput(
                    filename=path.name,
                    local_path=path,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    version="1.2.3",
                ),
            )
        ]
        assert updates[-1].percent == 100
        assert len(flash_capability.calls) == 1
        assert isinstance(flash_capability.calls[0][0], FirmwareArtifact)
        snapshot = await backend.get_bench("esp32-validation-01")
        assert snapshot.powered is True
        assert snapshot.firmware_version == "1.2.3"

    asyncio.run(scenario())


def test_composed_bench_availability_is_gated_only_by_required_resources() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        catalog = ResourceCatalog()
        for resource_id, capabilities in (
            ("required-healthy", {"probe"}),
            ("required-unhealthy", {"probe"}),
            ("optional-unhealthy", {"measure"}),
            ("optional-missing", {"power"}),
        ):
            catalog.upsert_resource(
                HardwareResource(
                    id=resource_id,
                    agent_id=agent_id,
                    plugin="test",
                    type="test",
                    name=resource_id,
                    health=ResourceHealthStatus.HEALTHY,
                    capabilities=frozenset(capabilities),
                )
            )

        optional_bindings = (
            BenchResourceBinding(
                role=ResourceBindingRole.INSTRUMENT,
                resource_id="optional-unhealthy",
                required=False,
            ),
            BenchResourceBinding(
                role=ResourceBindingRole.POWER,
                resource_id="optional-missing",
                required=False,
            ),
        )
        catalog.register_bench(
            BenchComposition(
                id="available-with-optional-failures",
                name="Available with optional failures",
                resources=(
                    BenchResourceBinding(
                        role=ResourceBindingRole.TARGET,
                        resource_id="required-healthy",
                    ),
                    *optional_bindings,
                ),
            )
        )
        catalog.register_bench(
            BenchComposition(
                id="offline-required-resource",
                name="Offline required resource",
                resources=(
                    BenchResourceBinding(
                        role=ResourceBindingRole.TARGET,
                        resource_id="required-unhealthy",
                    ),
                    *optional_bindings,
                ),
            )
        )

        def driver(resource_id: str, status: DeviceHealthStatus) -> HealthReportingDriver:
            return HealthReportingDriver(
                DeviceDescriptor(
                    id=resource_id,
                    name=resource_id,
                    type="test",
                    capabilities={"probe"} if resource_id.startswith("required") else {"measure"},
                ),
                status,
            )

        backend = ComposedHardwareBackend(
            catalog,
            {
                "required-healthy": driver("required-healthy", DeviceHealthStatus.HEALTHY),
                "required-unhealthy": driver("required-unhealthy", DeviceHealthStatus.UNHEALTHY),
                "optional-unhealthy": driver("optional-unhealthy", DeviceHealthStatus.UNHEALTHY),
            },
        )
        await backend.start()

        available = await backend.get_bench("available-with-optional-failures")
        assert available.online is True
        assert available.status is BenchStatus.AVAILABLE

        snapshots = {item.id: item for item in await backend.list_benches()}
        assert snapshots["available-with-optional-failures"].online is True
        assert snapshots["offline-required-resource"].online is False
        assert snapshots["offline-required-resource"].status is BenchStatus.OFFLINE

    asyncio.run(scenario())
