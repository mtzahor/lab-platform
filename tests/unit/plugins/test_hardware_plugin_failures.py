from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from lab_platform.config import (
    HardwareBenchSettings,
    JLinkSettings,
    Nrf52Settings,
    OpenOcdSettings,
    Rp2040Settings,
)
from lab_platform.models import (
    BackendProgress,
    FirmwareInput,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.models import (
    SerialLine as BackendSerialLine,
)
from lab_platform.plugin_sdk import (
    ArtifactReference,
    CanFilter,
    CanFrame,
    CaptureRequest,
    DebugStatus,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    DeviceUnavailableError,
    DiagnosticCheck,
    DiagnosticStatus,
    FirmwareArtifact,
    FlashOptions,
    MeasureOptions,
    PluginHealthStatus,
    PluginMetadata,
    PluginOperationError,
    SerialOptions,
    TriggerDefinition,
    UnsupportedCapabilityError,
)
from lab_platform.plugins.can import (
    CanCaptureCapability,
    MemoryCanTransport,
    SocketCanDriver,
    SocketCanPlugin,
    SocketCanTransport,
    frame_matches,
)
from lab_platform.plugins.esp32 import Esp32Plugin
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.instruments import (
    InstrumentDriver,
    InstrumentPlugin,
    MemoryInstrumentTransport,
    PowerSupplyCapability,
    ProgrammablePowerSupplyDriver,
)
from lab_platform.plugins.jlink import JLinkPlugin
from lab_platform.plugins.nrf52 import Nrf52Plugin
from lab_platform.plugins.openocd import OpenOcdPlugin
from lab_platform.plugins.power import (
    CommandRelayTransport,
    MemoryRelayTransport,
    NetworkRelayPlugin,
    NetworkRelayTransport,
    UsbRelayDriver,
    UsbRelayPlugin,
    UsbRelayPowerCapability,
)
from lab_platform.plugins.rp2040 import Rp2040Plugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.errors import (
    ExternalToolCommandFailedError,
    ExternalToolNotAvailableError,
    FirmwareVerificationFailedError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import (
    OutputCallback,
    ProcessLine,
    ProcessResult,
    ProcessRunner,
)
from lab_platform.real_backend.targets.openocd import build_openocd_args
from lab_platform.real_backend.targets.rp2040 import Rp2040Target
from lab_platform.real_backend.targets.tooling import last_output, safe_tool_path, verify_firmware


class _OutcomeRunner(ProcessRunner):
    def __init__(self, outcomes: Sequence[ProcessResult | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        self.calls.append((tuple(args), timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if on_output is not None:
            for line in outcome.stdout:
                await on_output(ProcessLine("stdout", line))
        return outcome


class _HealthDriver:
    def __init__(self, device_id: str, status: DeviceHealthStatus) -> None:
        self._descriptor = DeviceDescriptor(
            id=device_id,
            name=device_id,
            type="test",
            capabilities=set(),
        )
        self.status = status

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return set()

    async def health(self) -> DeviceHealth:
        return DeviceHealth(status=self.status, message=self.status.value)

    async def get_capability(self, name: str) -> Any:
        raise UnsupportedCapabilityError("unsupported", capability=name)


class _UnhealthyRelayTransport(MemoryRelayTransport):
    async def health(self) -> tuple[bool, str]:
        return False, "relay disconnected"


class _UnhealthyInstrumentTransport(MemoryInstrumentTransport):
    async def health(self) -> tuple[bool, str]:
        return False, "instrument disconnected"


class _ArtifactSink:
    def __init__(self) -> None:
        self.content = b""
        self.metadata: dict[str, Any] = {}

    async def store(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference:
        self.content = content
        self.metadata = dict(metadata or {})
        return ArtifactReference(
            id=name,
            media_type=media_type,
            metadata=self.metadata,
        )


class _FakeCanSocket:
    def __init__(self, outcomes: Sequence[bytes | BaseException] = ()) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False

    def __enter__(self) -> _FakeCanSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, _size: int) -> bytes:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


class _FakePhysicalTarget:
    def __init__(self, *, debug_value: object | None = None) -> None:
        self._id = "physical-01"
        self._capabilities = {"probe", "reset", "firmware", "serial", "debug"}
        self.target_health = TargetHealth(
            bench_id=self._id,
            status=TargetHealthStatus.ONLINE,
            chip_type="fixture-mcu",
            mac_address="00:11:22:33:44:55",
            serial_port="/dev/fixture",
            details={"message": "ready"},
        )
        self.reset_calls = 0
        self.flash_calls: list[FirmwareInput] = []
        self.serial_requests: list[SerialReadRequest] = []
        self.debug_value = debug_value

    @property
    def id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> set[str]:
        return set(self._capabilities)

    async def probe(self) -> TargetHealth:
        return self.target_health

    async def snapshot(self) -> Any:
        raise NotImplementedError

    async def flash(self, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]:
        self.flash_calls.append(firmware)
        yield BackendProgress(
            percent=100,
            message="flashed",
            serial_lines=[BackendSerialLine(text="READY")],
            firmware_version=firmware.version,
        )

    async def read_serial(
        self,
        request: SerialReadRequest,
    ) -> AsyncIterator[BackendSerialLine]:
        self.serial_requests.append(request)
        yield BackendSerialLine(text=request.until_pattern or "serial")

    async def reset(self) -> None:
        self.reset_calls += 1

    async def debug_status(self) -> object:
        return self.debug_value


class _NoDebugCallbackTarget:
    def __init__(self) -> None:
        self.delegate = _FakePhysicalTarget()

    @property
    def id(self) -> str:
        return self.delegate.id

    @property
    def capabilities(self) -> set[str]:
        return self.delegate.capabilities

    async def probe(self) -> TargetHealth:
        return await self.delegate.probe()

    async def snapshot(self) -> Any:
        return await self.delegate.snapshot()

    def flash(self, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]:
        return self.delegate.flash(firmware)

    def read_serial(
        self,
        request: SerialReadRequest,
    ) -> AsyncIterator[BackendSerialLine]:
        return self.delegate.read_serial(request)

    async def reset(self) -> None:
        await self.delegate.reset()


def _firmware(tmp_path: Path, content: bytes = b"firmware") -> FirmwareInput:
    path = tmp_path / "firmware.bin"
    path.write_bytes(content)
    return FirmwareInput(
        filename=path.name,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        version="1.2.3",
    )


def test_static_hardware_plugin_enforces_lifecycle_health_and_unique_devices() -> None:
    metadata = PluginMetadata(
        name="static-test",
        version="1.0.0",
        description="Static plugin fixture.",
    )
    healthy = _HealthDriver("healthy", DeviceHealthStatus.HEALTHY)
    degraded = _HealthDriver("degraded", DeviceHealthStatus.DEGRADED)
    warning = DiagnosticCheck(
        name="optional dependency",
        status=DiagnosticStatus.WARNING,
        message="optional dependency missing",
    )
    plugin = StaticHardwarePlugin(
        metadata,
        [healthy, degraded],
        dependency_checks=[warning],
    )

    async def scenario() -> None:
        assert await plugin.discover() == []
        assert (await plugin.health()).status is PluginHealthStatus.UNKNOWN
        await plugin.initialize()
        assert [item.id for item in await plugin.discover()] == ["degraded", "healthy"]
        health = await plugin.health()
        assert health.status is PluginHealthStatus.DEGRADED
        assert health.details == {"device_count": 2}
        assert await plugin.get_driver("healthy") is healthy
        with pytest.raises(DeviceUnavailableError) as unavailable:
            await plugin.get_driver("missing")
        assert unavailable.value.details["plugin"] == "static-test"
        assert await plugin.diagnostics() == [warning]

    asyncio.run(scenario())

    with pytest.raises(ValueError, match="device IDs must be unique"):
        StaticHardwarePlugin(metadata, [healthy, healthy])

    failure = DiagnosticCheck(
        name="required dependency",
        status=DiagnosticStatus.FAIL,
        message="required tool missing",
    )
    failed = StaticHardwarePlugin(metadata, dependency_checks=[failure])

    async def failed_scenario() -> None:
        await failed.initialize()
        health = await failed.health()
        assert health.status is PluginHealthStatus.UNHEALTHY
        assert health.message == "required tool missing"

    asyncio.run(failed_scenario())


def test_relay_transports_validate_argv_and_translate_process_failures() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        CommandRelayTransport(_OutcomeRunner([]), [])
    with pytest.raises(ValueError, match="unsupported relay command placeholder"):
        CommandRelayTransport(_OutcomeRunner([]), ["relay", "{password}"])

    success_runner = _OutcomeRunner([ProcessResult(0, ("ok",), ())])
    transport = CommandRelayTransport(
        success_runner,
        ["relayctl", "--channel", "{channel}", "--state", "{state}"],
        timeout_seconds=3,
    )

    async def scenario() -> None:
        await transport.set_state("rack_1.aux-2", True)
        assert success_runner.calls == [
            (("relayctl", "--channel", "rack_1.aux-2", "--state", "on"), 3)
        ]
        assert await transport.health() == (True, "Relay command configured: relayctl")
        with pytest.raises(ValueError, match="unsupported characters"):
            await transport.set_state("1; reboot", False)

        missing = CommandRelayTransport(
            _OutcomeRunner([ProcessExecutableNotFoundError("missing")]),
            ["missing-relay", "{state}"],
        )
        with pytest.raises(ExternalToolNotAvailableError, match="not installed"):
            await missing.set_state("1", True)

        timed_out = CommandRelayTransport(
            _OutcomeRunner([ProcessExecutionTimeoutError("timeout")]),
            ["slow-relay", "{state}"],
        )
        with pytest.raises(ExternalToolCommandFailedError, match="timed out"):
            await timed_out.set_state("1", False)

        failed = CommandRelayTransport(
            _OutcomeRunner([ProcessResult(7, ("stdout detail",), ("stderr detail",))]),
            ["bad-relay", "{state}"],
        )
        with pytest.raises(ExternalToolCommandFailedError) as error:
            await failed.set_state("1", False)
        assert error.value.details == {"return_code": 7}
        assert str(error.value) == "stdout detail"

    asyncio.run(scenario())


def test_relay_capabilities_drivers_and_plugins_cover_independent_power() -> None:
    events: list[tuple[str, bool]] = []

    async def setter(channel: str, enabled: bool) -> None:
        events.append((channel, enabled))

    async def unhealthy() -> tuple[bool, str]:
        return False, "network relay unreachable"

    default_network = NetworkRelayTransport(setter)
    checked_network = NetworkRelayTransport(setter, health_check=unhealthy)
    memory = MemoryRelayTransport()

    async def no_sleep(_seconds: float) -> None:
        return None

    async def scenario() -> None:
        await default_network.set_state("a", True)
        assert await default_network.health() == (True, "Network relay client is configured")
        assert await checked_network.health() == (False, "network relay unreachable")

        with pytest.raises(ValueError, match="cannot be negative"):
            UsbRelayPowerCapability(memory, "1", cycle_delay_seconds=-1)
        power = UsbRelayPowerCapability(
            memory,
            "1",
            cycle_delay_seconds=0,
            sleep=no_sleep,
        )
        await power.on()
        await power.off()
        await power.cycle()
        assert memory.states == {"1": True}

        driver = UsbRelayDriver(
            device_id="relay-1",
            name="Relay 1",
            channel="1",
            transport=_UnhealthyRelayTransport(),
        )
        assert (await driver.health()).status is DeviceHealthStatus.UNHEALTHY
        assert (await driver.get_capability(" POWER ")) is not None
        with pytest.raises(UnsupportedCapabilityError):
            await driver.get_capability("flash")

        usb = UsbRelayPlugin({"2": memory, "1": memory})
        network = NetworkRelayPlugin({"a": default_network})
        await usb.initialize()
        await network.initialize()
        assert [item.id for item in await usb.discover()] == [
            "usb-relay:1",
            "usb-relay:2",
        ]
        assert (await network.discover())[0].type == "network-relay-channel"

    asyncio.run(scenario())
    assert events == [("a", True)]


def test_can_filter_capture_driver_and_plugin_contracts() -> None:
    standard = CanFrame(arbitration_id=0x123, data=b"one")
    extended = CanFrame(arbitration_id=0x1ABCDE, data=b"two", extended=True)
    assert frame_matches(standard, CanFilter())
    assert frame_matches(standard, CanFilter(arbitration_id=0x120, mask=0x7F0))
    assert not frame_matches(standard, CanFilter(arbitration_id=0x456))
    assert not frame_matches(standard, CanFilter(extended=True))
    assert frame_matches(extended, CanFilter(extended=True))

    transport = MemoryCanTransport([standard, extended])
    sink = _ArtifactSink()
    capture = CanCaptureCapability(transport, sink)

    async def scenario() -> None:
        artifact = await capture.capture(
            CaptureRequest(
                duration_seconds=0.01,
                trigger=TriggerDefinition(type="can_id", value="0x123"),
            )
        )
        assert artifact.metadata == {"frame_count": 1}
        assert b'"arbitration_id":291' in sink.content

        numeric = await CanCaptureCapability(transport).capture(
            CaptureRequest(
                duration_seconds=0.01,
                trigger=TriggerDefinition(type="can_id", value=0x1ABCDE),
            )
        )
        assert numeric.metadata["frame_count"] == 1
        invalid_trigger = await CanCaptureCapability(transport).capture(
            CaptureRequest(
                duration_seconds=0.01,
                trigger=TriggerDefinition(type="can_id", value=None),
            )
        )
        assert invalid_trigger.metadata["frame_count"] == 2

        driver = SocketCanDriver(interface="vcan0", transport=transport)
        assert (await driver.health()).status is DeviceHealthStatus.HEALTHY
        assert await driver.get_capability(" CAN ") is not None
        assert await driver.get_capability("capture") is not None
        with pytest.raises(UnsupportedCapabilityError):
            await driver.get_capability("serial")

        plugin = SocketCanPlugin(
            interfaces=["vcan1", "vcan0", "vcan0"],
            transports={"vcan0": transport, "vcan1": transport},
        )
        await plugin.initialize()
        assert [item.id for item in await plugin.discover()] == [
            "socketcan:vcan0",
            "socketcan:vcan1",
        ]

    asyncio.run(scenario())


def test_socketcan_transport_validates_and_translates_socket_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        SocketCanTransport("can0;down")
    transport = SocketCanTransport("vcan0")

    async def scenario() -> None:
        oversized = CanFrame(arbitration_id=1, data=b"012345678")
        with pytest.raises(PluginOperationError, match="up to 8 bytes"):
            await transport.send(oversized)

        sent_socket = _FakeCanSocket()
        monkeypatch.setattr(transport, "_open_socket", lambda: sent_socket)
        frame = CanFrame(
            arbitration_id=0x1ABCDE,
            data=b"\x01\x02",
            extended=True,
            remote=True,
        )
        await transport.send(frame)
        identifier, length, data = struct.unpack("=IB3x8s", sent_socket.sent[0])
        assert identifier & 0x1FFFFFFF == frame.arbitration_id
        assert identifier & 0x80000000
        assert identifier & 0x40000000
        assert length == 2
        assert data[:2] == b"\x01\x02"
        assert sent_socket.closed

        def open_fails() -> Any:
            raise OSError("interface down")

        monkeypatch.setattr(transport, "_open_socket", open_fails)
        with pytest.raises(PluginOperationError, match="transmit failed"):
            await transport.send(CanFrame(arbitration_id=1))
        with pytest.raises(PluginOperationError, match="receive failed"):
            await anext(transport.receive(CanFilter()))

        raw = struct.pack("=IB3x8s", 0x80000123, 2, b"ok".ljust(8, b"\0"))
        receiving_socket = _FakeCanSocket([TimeoutError(), raw])
        monkeypatch.setattr(transport, "_open_socket", lambda: receiving_socket)
        stream = cast(
            AsyncGenerator[CanFrame, None],
            transport.receive(CanFilter(arbitration_id=0x123, extended=True)),
        )
        received = await anext(stream)
        assert received.arbitration_id == 0x123
        assert received.data == b"ok"
        assert received.extended
        await stream.aclose()
        assert receiving_socket.timeout == 0.25
        assert receiving_socket.closed

        broken_socket = _FakeCanSocket([OSError("adapter removed")])
        monkeypatch.setattr(transport, "_open_socket", lambda: broken_socket)
        with pytest.raises(PluginOperationError, match="adapter removed"):
            await anext(transport.receive(CanFilter()))
        assert broken_socket.closed

    asyncio.run(scenario())


def test_instrument_measure_capture_power_and_driver_failures() -> None:
    transport = MemoryInstrumentTransport({"voltage": (3.3, "V"), "current": (0.2, "A")})

    async def no_sleep(_seconds: float) -> None:
        return None

    async def scenario() -> None:
        assert await transport.health() == (True, "Memory instrument is available")
        await transport.set_output("1", True)
        reading = await transport.measure("VOLTAGE", MeasureOptions(channel="CH1"))
        assert reading.metadata == {"channel": "CH1"}
        with pytest.raises(PluginOperationError) as missing:
            await transport.measure("resistance", MeasureOptions())
        assert missing.value.details == {"quantity": "resistance"}
        artifact = await transport.capture(
            CaptureRequest(duration_seconds=0.01, channels=["CH1"], format=None)
        )
        assert artifact.metadata["format"] == "json"

        with pytest.raises(ValueError, match="cannot be negative"):
            PowerSupplyCapability(transport, "1", cycle_delay_seconds=-1)
        supply_power = PowerSupplyCapability(
            transport,
            "1",
            cycle_delay_seconds=0,
            sleep=no_sleep,
        )
        await supply_power.off()
        await supply_power.cycle()
        assert transport.outputs == {"1": True}

        with pytest.raises(ValueError, match="unsupported generic instrument category"):
            InstrumentDriver(
                device_id="bad",
                name="Bad",
                category="multimeter",
                transport=transport,
            )
        instrument = InstrumentDriver(
            device_id="scope",
            name="Scope",
            category="oscilloscope",
            transport=_UnhealthyInstrumentTransport(),
        )
        assert (await instrument.health()).status is DeviceHealthStatus.UNHEALTHY
        assert await instrument.get_capability("measure") is not None
        assert await instrument.get_capability("CAPTURE") is not None
        with pytest.raises(UnsupportedCapabilityError):
            await instrument.get_capability("power")

        supply = ProgrammablePowerSupplyDriver(
            device_id="psu",
            name="PSU",
            channel="1",
            transport=transport,
            cycle_delay_seconds=0,
            sleep=no_sleep,
        )
        assert supply.descriptor.metadata == {"channel": "1"}
        assert supply.capabilities == {"power", "measure"}
        assert (await supply.health()).status is DeviceHealthStatus.HEALTHY
        assert await supply.get_capability("power") is not None
        assert await supply.get_capability("measure") is not None
        with pytest.raises(UnsupportedCapabilityError):
            await supply.get_capability("capture")

        plugin = InstrumentPlugin([instrument, supply])
        await plugin.initialize()
        assert len(await plugin.discover()) == 2

    asyncio.run(scenario())


def test_physical_target_adapter_maps_all_stable_capabilities(tmp_path: Path) -> None:
    target = _FakePhysicalTarget(
        debug_value={
            "status": "available",
            "connected": True,
            "endpoint": "localhost:3333",
            "detail": "debugger ready",
        }
    )
    driver = PhysicalTargetDriver(target, device_type="fixture", name="Fixture target")
    firmware = _firmware(tmp_path)
    artifact = FirmwareArtifact(
        filename=firmware.filename,
        local_path=firmware.local_path,
        sha256=firmware.sha256,
        size_bytes=firmware.size_bytes,
        version=firmware.version,
    )

    async def scenario() -> None:
        assert driver.descriptor.name == "Fixture target"
        assert driver.capabilities == {"probe", "reset", "flash", "serial", "debug"}
        for status, expected in (
            (TargetHealthStatus.ONLINE, DeviceHealthStatus.HEALTHY),
            (TargetHealthStatus.DEGRADED, DeviceHealthStatus.DEGRADED),
            (TargetHealthStatus.OFFLINE, DeviceHealthStatus.OFFLINE),
            (TargetHealthStatus.UNKNOWN, DeviceHealthStatus.UNKNOWN),
        ):
            target.target_health = target.target_health.model_copy(update={"status": status})
            assert (await driver.health()).status is expected

        target.target_health = target.target_health.model_copy(
            update={"status": TargetHealthStatus.ONLINE}
        )
        probe = cast(Any, await driver.get_capability("probe"))
        result = await probe.probe()
        assert result.online
        assert result.device_type == "fixture-mcu"
        assert result.details["serial_port"] == "/dev/fixture"

        reset = cast(Any, await driver.get_capability("reset"))
        assert (await reset.reset()).message == "Target reset completed"
        assert target.reset_calls == 1

        flash = cast(Any, await driver.get_capability("firmware"))
        updates = [
            update
            async for update in flash.flash(
                artifact,
                FlashOptions(),
            )
        ]
        assert updates[0].details["firmware_version"] == "1.2.3"
        assert updates[0].details["verify_requested"] is True
        assert updates[0].details["reset_after_requested"] is True
        assert updates[0].details["serial_lines"][0]["text"] == "READY"
        assert target.flash_calls == [firmware]

        serial = cast(Any, await driver.get_capability("serial"))
        lines = [
            line
            async for line in serial.stream(
                SerialOptions(timeout_seconds=1, until_pattern="READY", max_lines=2)
            )
        ]
        assert lines[0].text == "READY"
        assert target.serial_requests == [
            SerialReadRequest(timeout_seconds=1, until_pattern="READY", max_lines=2)
        ]

        debug = cast(Any, await driver.get_capability("debug"))
        status = await debug.status()
        assert status == DebugStatus(
            available=True,
            connected=True,
            endpoint="localhost:3333",
            message="debugger ready",
        )
        with pytest.raises(UnsupportedCapabilityError) as unsupported:
            await driver.get_capability("gpio")
        assert unsupported.value.details["capability"] == "gpio"

    asyncio.run(scenario())


def test_physical_target_adapter_rejects_unsupported_option_overrides(
    tmp_path: Path,
) -> None:
    target = _FakePhysicalTarget()
    driver = PhysicalTargetDriver(target, device_type="fixture")
    firmware = _firmware(tmp_path)
    artifact = FirmwareArtifact(
        filename=firmware.filename,
        local_path=firmware.local_path,
        sha256=firmware.sha256,
        size_bytes=firmware.size_bytes,
        version=firmware.version,
    )

    async def scenario() -> None:
        flash = cast(Any, await driver.get_capability("flash"))
        for options, expected in (
            (FlashOptions(verify=False), {"verify": False}),
            (FlashOptions(reset_after=False), {"reset_after": False}),
            (FlashOptions(timeout_seconds=30), {"timeout_seconds": 30.0}),
            (FlashOptions(values={"erase": "all"}), {"values": {"erase": "all"}}),
        ):
            with pytest.raises(UnsupportedCapabilityError) as unsupported:
                _ = [update async for update in flash.flash(artifact, options)]
            assert unsupported.value.details == {
                "device_id": "physical-01",
                "capability": "flash",
                "unsupported_options": expected,
            }
        assert target.flash_calls == []

        serial = cast(Any, await driver.get_capability("serial"))
        with pytest.raises(UnsupportedCapabilityError) as unsupported:
            _ = [
                line
                async for line in serial.stream(SerialOptions(baud_rate=230400, timeout_seconds=1))
            ]
        assert unsupported.value.details == {
            "device_id": "physical-01",
            "capability": "serial",
            "unsupported_options": {"baud_rate": 230400},
        }
        assert target.serial_requests == []

    asyncio.run(scenario())


def test_debug_adapter_handles_absent_typed_text_and_unavailable_callbacks() -> None:
    async def scenario() -> None:
        no_callback = PhysicalTargetDriver(
            cast(Any, _NoDebugCallbackTarget()),
            device_type="fixture",
        )
        debug = cast(Any, await no_callback.get_capability("debug"))
        assert (await debug.status()).message == "Debug capability is configured"

        typed_target = _FakePhysicalTarget(debug_value=DebugStatus(available=True))
        typed = PhysicalTargetDriver(typed_target, device_type="fixture")
        typed_debug = cast(Any, await typed.get_capability("debug"))
        assert await typed_debug.status() == DebugStatus(available=True)

        text_target = _FakePhysicalTarget(debug_value="debug bridge ready")
        text = PhysicalTargetDriver(text_target, device_type="fixture")
        text_debug = cast(Any, await text.get_capability("debug"))
        assert (await text_debug.status()).message == "debug bridge ready"

        unavailable_target = _FakePhysicalTarget(
            debug_value={"status": "failed", "message": "probe missing"}
        )
        unavailable = PhysicalTargetDriver(unavailable_target, device_type="fixture")
        unavailable_debug = cast(Any, await unavailable.get_capability("debug"))
        assert not (await unavailable_debug.status()).available

    asyncio.run(scenario())


def test_official_target_plugins_wrap_existing_physical_targets() -> None:
    target = cast(Any, _FakePhysicalTarget())
    plugins = [
        Esp32Plugin.from_targets([target]),
        OpenOcdPlugin.from_targets([target], executable_available=True),
        JLinkPlugin.from_targets([target], executable_available=True),
        Rp2040Plugin.from_targets([target], dependency_available=True),
        Nrf52Plugin.from_targets([target], executable_available=True),
    ]

    async def scenario() -> None:
        for plugin in plugins:
            await plugin.initialize()
            descriptors = await plugin.discover()
            assert len(descriptors) == 1
            assert (await plugin.health()).status is PluginHealthStatus.HEALTHY
            assert (
                (await plugin.diagnostics())[0].status is DiagnosticStatus.PASS
                if (await plugin.diagnostics())
                else True
            )

    asyncio.run(scenario())


def test_official_dependency_diagnostics_fail_closed(tmp_path: Path) -> None:
    missing_mount = tmp_path / "not-mounted"
    plugins = [
        OpenOcdPlugin(executable_available=False),
        JLinkPlugin(executable_available=False),
        Rp2040Plugin(dependency_available=False),
        Rp2040Plugin(
            settings=Rp2040Settings(tool="uf2", mount_path=missing_mount),
        ),
        Nrf52Plugin(executable_available=False),
        Nrf52Plugin(
            settings=Nrf52Settings(tool="jlink"),
            jlink_settings=JLinkSettings(executable="custom-jlink"),
            executable_available=False,
        ),
    ]

    async def scenario() -> None:
        for plugin in plugins:
            await plugin.initialize()
            health = await plugin.health()
            assert health.status is PluginHealthStatus.UNHEALTHY
            checks = await plugin.diagnostics()
            assert checks[0].status is DiagnosticStatus.FAIL
            assert checks[0].remediation

    asyncio.run(scenario())

    jlink = Nrf52Plugin(
        settings=Nrf52Settings(tool="jlink"),
        jlink_settings=JLinkSettings(executable="custom-jlink"),
        executable_available=True,
    )

    async def jlink_scenario() -> None:
        await jlink.initialize()
        check = (await jlink.diagnostics())[0]
        assert check.name == "J-Link"
        assert check.message == "J-Link is available as custom-jlink"

    asyncio.run(jlink_scenario())


def test_rp2040_uf2_mode_does_not_advertise_unavailable_reset(tmp_path: Path) -> None:
    target = Rp2040Target(
        HardwareBenchSettings(
            id="pico-uf2",
            name="Pico UF2",
            target_type="rp2040",
            rp2040=Rp2040Settings(tool="uf2", mount_path=tmp_path),
        ),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )
    assert "reset" not in target.capabilities
    driver = PhysicalTargetDriver(target, device_type="rp2040")
    assert "reset" not in driver.capabilities

    picotool = Rp2040Target(
        HardwareBenchSettings(
            id="pico-tool",
            name="Pico picotool",
            target_type="rp2040",
        ),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
    )
    assert "reset" in picotool.capabilities


def test_firmware_tooling_revalidates_files_and_command_language_paths(
    tmp_path: Path,
) -> None:
    firmware = _firmware(tmp_path)

    async def scenario() -> None:
        await verify_firmware(firmware)
        firmware.local_path.write_bytes(b"tampered")
        with pytest.raises(FirmwareVerificationFailedError) as changed:
            await verify_firmware(firmware)
        assert changed.value.details["expected_size"] == len(b"firmware")

        missing = firmware.model_copy(update={"local_path": tmp_path / "missing.bin"})
        with pytest.raises(FirmwareVerificationFailedError, match="no longer exists"):
            await verify_firmware(missing)

    asyncio.run(scenario())

    assert safe_tool_path("/tmp/firmware.bin", tool="test") == "/tmp/firmware.bin"
    for unsafe in ("bad\npath", "bad;path", 'bad"path', "bad{path"):
        with pytest.raises(FirmwareVerificationFailedError, match="unsafe"):
            safe_tool_path(unsafe, tool="test")

    assert last_output(ProcessResult(0, ("stdout",), ("stderr",)), "fallback") == "stdout"
    assert last_output(ProcessResult(0, ("",), ("",)), "fallback") == "fallback"


def test_openocd_builder_rejects_empty_or_multiline_commands() -> None:
    with pytest.raises(ValueError, match="at least one command"):
        build_openocd_args(OpenOcdSettings(), [])
    for command in ("", "reset\nshutdown", "reset\rshutdown"):
        with pytest.raises(ValueError, match="single-line"):
            build_openocd_args(OpenOcdSettings(), [command])

    settings = OpenOcdSettings(transport="jtag")
    args = build_openocd_args(settings, ["init"])
    assert args[-4:] == ["-c", "transport select jtag", "-c", "init"]
