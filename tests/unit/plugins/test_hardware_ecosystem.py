from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

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
from lab_platform.core.resources import (
    ResourceCatalog,
    ResourceConflictError,
    ResourceLockManager,
    ResourceReservationCoordinator,
    ResourceUnavailableError,
)
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
from lab_platform.models.hardware import ResourceLockOwnerType
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
from pydantic import ValidationError


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


AGENT_ID = UUID("00000000-0000-0000-0000-000000000909")


def _resource(
    resource_id: str,
    *,
    capabilities: set[str],
    sharing: ResourceSharingMode = ResourceSharingMode.EXCLUSIVE,
    channels: set[str] | None = None,
    health: ResourceHealthStatus = ResourceHealthStatus.HEALTHY,
) -> HardwareResource:
    return HardwareResource(
        id=resource_id,
        agent_id=AGENT_ID,
        plugin="test-hardware",
        type="test",
        name=resource_id,
        health=health,
        capabilities=frozenset(capabilities),
        sharing=sharing,
        channels=frozenset(channels or set()),
    )


def _catalog() -> ResourceCatalog:
    catalog = ResourceCatalog()
    catalog.upsert_resource(_resource("target-1", capabilities={"probe", "flash", "serial"}))
    catalog.upsert_resource(
        _resource(
            "psu-1",
            capabilities={"power", "measure"},
            sharing=ResourceSharingMode.CHANNEL,
            channels={"1", "2"},
        )
    )
    catalog.upsert_resource(
        _resource(
            "can-1",
            capabilities={"can", "capture"},
            sharing=ResourceSharingMode.SHARED,
        )
    )
    return catalog


def _bench(bench_id: str, channel: str) -> BenchComposition:
    return BenchComposition(
        id=bench_id,
        name=bench_id,
        resources=(
            BenchResourceBinding(role=ResourceBindingRole.TARGET, resource_id="target-1"),
            BenchResourceBinding(
                role=ResourceBindingRole.POWER,
                resource_id="psu-1",
                channel=channel,
                capabilities=frozenset({"power"}),
            ),
            BenchResourceBinding(role=ResourceBindingRole.CAN, resource_id="can-1"),
        ),
    )


def test_catalog_validates_composition_and_reports_capabilities() -> None:
    catalog = _catalog()
    catalog.register_bench(_bench("validation-1", "1"))

    assert catalog.capabilities_for_bench("validation-1") == {
        "probe",
        "flash",
        "serial",
        "power",
        "can",
        "capture",
    }
    assert [item.id for item in catalog.list_resources(capability="POWER")] == ["psu-1"]

    with pytest.raises(ResourceConflictError, match="does not expose channel"):
        catalog.register_bench(_bench("invalid-channel", "9"))


def test_resource_model_requires_channels_only_for_channel_sharing() -> None:
    with pytest.raises(ValidationError, match="must declare at least one channel"):
        _resource(
            "missing-channels",
            capabilities={"power"},
            sharing=ResourceSharingMode.CHANNEL,
        )
    with pytest.raises(ValidationError, match="only channel-shared"):
        _resource("exclusive", capabilities={"debug"}, channels={"1"})


def test_required_unhealthy_resources_make_a_bench_unavailable() -> None:
    catalog = ResourceCatalog()
    catalog.upsert_resource(
        _resource(
            "target-offline",
            capabilities={"probe"},
            health=ResourceHealthStatus.OFFLINE,
        )
    )
    catalog.register_bench(
        BenchComposition(
            id="offline-bench",
            name="Offline bench",
            resources=(
                BenchResourceBinding(
                    role=ResourceBindingRole.TARGET,
                    resource_id="target-offline",
                ),
            ),
        )
    )

    with pytest.raises(ResourceUnavailableError, match="target-offline"):
        catalog.require_available("offline-bench")


def test_resource_locks_are_atomic_channel_aware_and_fenced() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 24, tzinfo=UTC)
        current = now
        catalog = _catalog()
        catalog.register_bench(_bench("bench-a", "1"))
        catalog.upsert_resource(_resource("target-2", capabilities={"probe", "flash"}))
        catalog.register_bench(
            BenchComposition(
                id="bench-b",
                name="Bench B",
                resources=(
                    BenchResourceBinding(
                        role=ResourceBindingRole.TARGET,
                        resource_id="target-2",
                    ),
                    BenchResourceBinding(
                        role=ResourceBindingRole.POWER,
                        resource_id="psu-1",
                        channel="2",
                    ),
                    BenchResourceBinding(role=ResourceBindingRole.CAN, resource_id="can-1"),
                ),
            )
        )
        locks = ResourceLockManager(catalog, clock=lambda: current)
        first_owner = uuid4()
        second_owner = uuid4()

        first = await locks.acquire_for_bench(
            "bench-a",
            ResourceLockOwnerType.RESERVATION,
            first_owner,
            expires_at=now + timedelta(minutes=10),
        )
        second = await locks.acquire_for_bench(
            "bench-b",
            ResourceLockOwnerType.RESERVATION,
            second_owner,
            expires_at=now + timedelta(minutes=10),
        )
        assert {(item.resource_id, item.channel) for item in first} == {
            ("target-1", None),
            ("psu-1", "1"),
        }
        assert {(item.resource_id, item.channel) for item in second} == {
            ("target-2", None),
            ("psu-1", "2"),
        }

        with pytest.raises(ResourceConflictError, match="already locked"):
            await locks.acquire_for_bench(
                "bench-a",
                ResourceLockOwnerType.WORKFLOW,
                uuid4(),
            )
        assert len(await locks.active_locks()) == 4

        released = await locks.release_owner(
            ResourceLockOwnerType.RESERVATION,
            first_owner,
        )
        assert len(released) == 2
        replacement = await locks.acquire_for_bench(
            "bench-a",
            ResourceLockOwnerType.WORKFLOW,
            uuid4(),
        )
        assert {item.fencing_token for item in replacement} == {2}

    asyncio.run(scenario())


def test_reservation_coordinator_renews_and_recovers_expired_locks() -> None:
    async def scenario() -> None:
        current = datetime(2026, 8, 24, tzinfo=UTC)
        catalog = _catalog()
        catalog.register_bench(_bench("bench-a", "1"))
        manager = ResourceLockManager(catalog, clock=lambda: current)
        coordinator = ResourceReservationCoordinator(manager)
        reservation_id = uuid4()

        acquired = await coordinator.acquire(
            "bench-a",
            reservation_id,
            expires_at=current + timedelta(minutes=5),
        )
        assert len(acquired) == 2
        renewed = await coordinator.renew(
            reservation_id,
            expires_at=current + timedelta(minutes=10),
        )
        assert all(item.expires_at == current + timedelta(minutes=10) for item in renewed)

        current += timedelta(minutes=11)
        expired = await manager.recover_expired()
        assert len(expired) == 2
        assert await manager.active_locks() == ()

    asyncio.run(scenario())
