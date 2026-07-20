from __future__ import annotations

import asyncio
import errno
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from lab_platform.config import HardwareBenchSettings, HardwareSettings, SerialConnectionSettings
from lab_platform.core import ConfigurationError
from lab_platform.models import FirmwareInput, SerialPortInfo, SerialReadRequest
from lab_platform.real_backend import RealLabBackend
from lab_platform.real_backend.discovery import SerialPortDiscovery
from lab_platform.real_backend.errors import (
    BootTimeoutError,
    BootVerificationFailedError,
    DeviceNotFoundError,
    EsptoolFlashFailedError,
    EsptoolNotAvailableError,
    EsptoolTimeoutError,
    FirmwareVerificationFailedError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
    SerialDisconnectedError,
    SerialPermissionDeniedError,
    SerialPortAmbiguousError,
    SerialPortBusyError,
    SerialPortNotFoundError,
    SerialReadTimeoutError,
)
from lab_platform.real_backend.process_runner import (
    AsyncSubprocessRunner,
    OutputCallback,
    ProcessLine,
    ProcessResult,
    ProcessRunner,
)
from lab_platform.real_backend.targets.esp32.adapter import Esp32Target, build_probe_args
from lab_platform.real_backend.targets.esp32.flasher import Esp32Flasher, build_flash_args
from lab_platform.real_backend.targets.esp32.parser import (
    extract_firmware_version,
    matches_any,
    parse_chip_info,
    parse_esptool_progress,
    parse_wrong_chip,
)
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader


class StaticPortProvider:
    def __init__(self, ports: list[SerialPortInfo]) -> None:
        self.ports = ports

    def list_ports(self) -> list[SerialPortInfo]:
        return self.ports


class FakeRunner(ProcessRunner):
    def __init__(
        self,
        result: ProcessResult | None = None,
        *,
        output: tuple[ProcessLine, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.result = result or ProcessResult(0, (), ())
        self.output = output
        self.error = error
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        for line in self.output:
            if on_output is not None:
                await on_output(line)
        return self.result


class RoutingRunner(ProcessRunner):
    def __init__(self, chip: str = "ESP32-D0WD", returncode: int = 0) -> None:
        self.chip = chip
        self.returncode = returncode
        self.calls: list[list[str]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        values = list(args)
        self.calls.append(values)
        if values[-1] == "chip_id":
            output = (f"Chip is {self.chip} (revision 1)\nMAC: AA:BB:CC:DD:EE:FF",)
            return ProcessResult(self.returncode, output, ())
        if on_output is not None:
            for text in (
                "Connecting...",
                "Erasing flash...",
                "Writing at 0x00010000... (50 %)",
                "Hash of data verified.",
            ):
                await on_output(ProcessLine("stdout", text))
        return ProcessResult(self.returncode, (), ())


class CancellableRoutingRunner(ProcessRunner):
    def __init__(self) -> None:
        self.flash_started = asyncio.Event()
        self.cancelled = False

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        if list(args)[-1] == "chip_id":
            return ProcessResult(0, ("Chip is ESP32-D0WD\nMAC: AA:BB:CC:DD:EE:FF",), ())
        self.flash_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class FakeSerialHandle:
    def __init__(self, lines: list[bytes], error: OSError | None = None) -> None:
        self.lines = lines
        self.error = error
        self.closed = False

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.error is not None:
            raise self.error
        return b""

    def close(self) -> None:
        self.closed = True


class FakeSerialFactory:
    def __init__(
        self,
        lines: list[bytes] | None = None,
        *,
        open_error: Exception | None = None,
        read_error: OSError | None = None,
    ) -> None:
        self.lines = lines or []
        self.open_error = open_error
        self.handle = FakeSerialHandle(self.lines.copy(), read_error)

    def __call__(self, *, port: str, baudrate: int, timeout: float) -> FakeSerialHandle:
        if self.open_error is not None:
            raise self.open_error
        return self.handle


def _port(device: str = "/dev/cu.esp32", **changes: object) -> SerialPortInfo:
    values: dict[str, object] = {
        "device": device,
        "description": "USB UART",
        "serial_number": "board-01",
        "vendor_id": 0x10C4,
        "product_id": 0xEA60,
    }
    values.update(changes)
    return SerialPortInfo.model_validate(values)


def _bench(**changes: object) -> HardwareBenchSettings:
    values: dict[str, object] = {"id": "esp32-devkit-01", "name": "ESP32 DevKit V1"}
    values.update(changes)
    return HardwareBenchSettings.model_validate(values)


def _firmware(path: Path, content: bytes = b"firmware") -> FirmwareInput:
    path.write_bytes(content)
    return FirmwareInput(
        filename=path.name,
        local_path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        version="0.1.0",
    )


def test_serial_port_discovery_priority_and_stable_failures(tmp_path: Path) -> None:
    first = _port()
    second = _port("/dev/cu.other", serial_number="board-02", product_id=0x1234)
    discovery = SerialPortDiscovery(StaticPortProvider([first, second]))

    assert (
        discovery.resolve(
            SerialConnectionSettings.model_validate(
                {"serial_port": "auto", "usb": {"serial_number": "board-01"}}
            )
        )
        == first
    )
    assert (
        discovery.resolve(
            SerialConnectionSettings.model_validate(
                {"serial_port": "auto", "usb": {"vid": 0x10C4, "pid": 0xEA60}}
            )
        )
        == first
    )

    with pytest.raises(SerialPortAmbiguousError):
        discovery.resolve(SerialConnectionSettings())
    with pytest.raises(DeviceNotFoundError):
        discovery.resolve(
            SerialConnectionSettings.model_validate(
                {"serial_port": "auto", "usb": {"serial_number": "missing"}}
            )
        )
    with pytest.raises(SerialPortNotFoundError):
        discovery.resolve(SerialConnectionSettings(serial_port=str(tmp_path / "missing")))
    explicit = tmp_path / "ttyUSB0"
    explicit.touch()
    assert discovery.resolve(SerialConnectionSettings(serial_port=str(explicit))).device == str(
        explicit
    )


def test_real_backend_configuration_factory() -> None:
    async def scenario() -> None:
        backend = RealLabBackend.from_config(HardwareSettings(benches=[_bench()]))
        snapshots = await backend.list_benches()
        assert snapshots[0].id == "esp32-devkit-01"
        assert snapshots[0].online is False

    asyncio.run(scenario())
    with pytest.raises(ConfigurationError):
        RealLabBackend.from_config(HardwareSettings())
    with pytest.raises(ConfigurationError):
        RealLabBackend.from_config(HardwareSettings(benches=[_bench(target_type="unsupported")]))


def test_esptool_parsers_and_argument_builders(tmp_path: Path) -> None:
    firmware = _firmware(tmp_path / "firmware.bin")
    config = _bench()
    flash_args = build_flash_args(config, "/dev/ttyUSB0", firmware)
    assert flash_args[:3] == [sys.executable, "-m", "esptool"]
    assert flash_args[-3:] == ["write_flash", "0x10000", str(firmware.local_path)]
    assert build_probe_args(config, "/dev/ttyUSB0")[-1] == "chip_id"

    assert parse_esptool_progress("Connecting...") == (20, "Connecting to bootloader")
    assert parse_esptool_progress("Erasing flash...") == (30, "Erasing flash region")
    assert parse_esptool_progress("Writing at 0x1 (50 %)") == (62, "Writing firmware")
    assert parse_esptool_progress("Hash of data verified.") == (90, "Verifying firmware")
    assert parse_esptool_progress("noise") is None
    assert parse_chip_info("Chip is ESP32-D0WD (revision 1)\nMAC: aa:bb:cc:dd:ee:ff") == (
        "ESP32-D0WD",
        "AA:BB:CC:DD:EE:FF",
    )
    assert parse_wrong_chip("A fatal error occurred: This chip is ESP32-S3, not ESP32") == (
        "ESP32-S3"
    )
    assert extract_firmware_version("FIRMWARE_VERSION=1.2.3", r"=(?P<version>.+)$") == "1.2.3"
    assert extract_firmware_version("READY", "READY") == "READY"
    assert extract_firmware_version("other", "READY") is None
    assert matches_any("Guru Meditation Error", ["Brownout", "Guru Meditation"]) == (
        "Guru Meditation"
    )


def test_flasher_maps_progress_and_translates_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        firmware = _firmware(tmp_path / "app.bin")
        output = (
            ProcessLine("stdout", "Connecting..."),
            ProcessLine("stdout", "Erasing flash..."),
            ProcessLine("stdout", "Writing at 0x1 (100 %)"),
            ProcessLine("stdout", "Hash of data verified."),
        )
        progress = [
            item
            async for item in Esp32Flasher(_bench(), FakeRunner(output=output)).flash(
                "/dev/ttyUSB0", firmware
            )
        ]
        assert [item.percent for item in progress] == [30, 85, 90]

        with pytest.raises(EsptoolFlashFailedError):
            async for _ in Esp32Flasher(
                _bench(), FakeRunner(ProcessResult(2, (), ("fatal",)))
            ).flash("/dev/ttyUSB0", firmware):
                pass
        with pytest.raises(EsptoolNotAvailableError):
            async for _ in Esp32Flasher(
                _bench(), FakeRunner(error=ProcessExecutableNotFoundError())
            ).flash("/dev/ttyUSB0", firmware):
                pass
        with pytest.raises(EsptoolTimeoutError):
            async for _ in Esp32Flasher(
                _bench(), FakeRunner(error=ProcessExecutionTimeoutError())
            ).flash("/dev/ttyUSB0", firmware):
                pass

    asyncio.run(scenario())


def test_serial_reader_success_timeout_and_error_translation() -> None:
    async def scenario() -> None:
        factory = FakeSerialFactory([b"BOOTING\r\n", b"READY\n"])
        lines = await Esp32SerialReader(factory).read(
            "/dev/ttyUSB0",
            115200,
            SerialReadRequest(timeout_seconds=0.1, until_pattern="READY"),
        )
        assert [line.text for line in lines] == ["BOOTING", "READY"]
        assert factory.handle.closed

        with pytest.raises(SerialReadTimeoutError):
            await Esp32SerialReader(FakeSerialFactory([b"BOOTING\n"])).read(
                "/dev/ttyUSB0",
                115200,
                SerialReadRequest(
                    timeout_seconds=0.1,
                    until_pattern="READY",
                    max_lines=1,
                ),
            )
        with pytest.raises(SerialPermissionDeniedError):
            await Esp32SerialReader(FakeSerialFactory(open_error=PermissionError())).read(
                "/dev/ttyUSB0", 115200, SerialReadRequest(timeout_seconds=0.1)
            )
        with pytest.raises(SerialPortBusyError):
            await Esp32SerialReader(
                FakeSerialFactory(open_error=OSError(errno.EBUSY, "resource busy"))
            ).read("/dev/ttyUSB0", 115200, SerialReadRequest(timeout_seconds=0.1))
        with pytest.raises(SerialDisconnectedError):
            await Esp32SerialReader(
                FakeSerialFactory(
                    [b"BOOTING\n"],
                    read_error=OSError(errno.EIO, "disconnected"),
                )
            ).read("/dev/ttyUSB0", 115200, SerialReadRequest(timeout_seconds=0.1))

    asyncio.run(scenario())


def test_esp32_target_probe_flash_serial_reset_and_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = StaticPortProvider([_port()])
        runner = RoutingRunner()
        serial = FakeSerialFactory(
            [
                b"BOOTING\n",
                b"FIRMWARE_VERSION=0.1.0\n",
                b"SELF_TEST=PASS\n",
                b"READY\n",
            ]
        )
        target = Esp32Target(
            _bench(),
            SerialPortDiscovery(provider),
            runner,
            Esp32SerialReader(serial),
        )
        health = await target.probe()
        assert health.status.value == "online"
        assert health.mac_address == "AA:BB:CC:DD:EE:FF"
        assert (await target.snapshot()).online

        firmware = _firmware(tmp_path / "target.bin")
        progress = [item async for item in target.flash(firmware)]
        assert progress[0].percent == 5
        assert progress[-1].message == "READY detected"
        assert progress[-1].firmware_version == "0.1.0"
        assert (await target.snapshot()).firmware_version == "0.1.0"

        target._serial = Esp32SerialReader(FakeSerialFactory([b"READY\n"]))
        assert [
            line.text
            async for line in target.read_serial(
                SerialReadRequest(timeout_seconds=0.1, until_pattern="READY")
            )
        ] == ["READY"]
        await target.reset()

        firmware.local_path.write_bytes(b"changed")
        with pytest.raises(FirmwareVerificationFailedError):
            async for _ in target.flash(firmware):
                pass

    asyncio.run(scenario())


def test_esp32_target_degraded_and_boot_failure_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        missing = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([])),
            RoutingRunner(),
            Esp32SerialReader(FakeSerialFactory()),
        )
        assert (await missing.probe()).status.value == "offline"

        wrong = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            RoutingRunner(chip="ESP8266"),
            Esp32SerialReader(FakeSerialFactory()),
        )
        assert (await wrong.probe()).details["error_code"] == "WRONG_TARGET_TYPE"

        failed_probe = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            RoutingRunner(returncode=2),
            Esp32SerialReader(FakeSerialFactory()),
        )
        assert (await failed_probe.probe()).status.value == "degraded"

        boot_failure = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            RoutingRunner(),
            Esp32SerialReader(FakeSerialFactory([b"Guru Meditation Error\n", b"READY\n"])),
        )
        with pytest.raises(BootVerificationFailedError):
            async for _ in boot_failure.flash(_firmware(tmp_path / "bad-boot.bin")):
                pass

        boot_timeout = Esp32Target(
            _bench(boot={"timeout_seconds": 0.05, "ready_pattern": "^READY$"}),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            RoutingRunner(),
            Esp32SerialReader(FakeSerialFactory([b"BOOTING\n"])),
        )
        timeout_progress = []
        with pytest.raises(BootTimeoutError):
            async for item in boot_timeout.flash(_firmware(tmp_path / "timeout.bin")):
                timeout_progress.append(item)
        assert [line.text for item in timeout_progress for line in item.serial_lines] == ["BOOTING"]

        disconnected = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            RoutingRunner(),
            Esp32SerialReader(
                FakeSerialFactory(
                    [b"PARTIAL\n"],
                    read_error=OSError(errno.EIO, "disconnected"),
                )
            ),
        )
        serial_output = disconnected.read_serial(SerialReadRequest(timeout_seconds=0.1))
        assert (await anext(serial_output)).text == "PARTIAL"
        with pytest.raises(SerialDisconnectedError):
            await anext(serial_output)

    asyncio.run(scenario())


def test_esp32_flash_cancellation_cleans_up_and_invalidates_health(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = CancellableRoutingRunner()
        target = Esp32Target(
            _bench(),
            SerialPortDiscovery(StaticPortProvider([_port()])),
            runner,
            Esp32SerialReader(FakeSerialFactory()),
        )

        async def consume() -> None:
            async for _ in target.flash(_firmware(tmp_path / "cancel.bin")):
                pass

        task = asyncio.create_task(consume())
        await runner.flash_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.cancelled
        snapshot = await target.snapshot()
        assert snapshot.online is False
        assert snapshot.firmware_version is None

    asyncio.run(scenario())


def test_async_process_runner_streams_times_out_and_cancels() -> None:
    async def scenario() -> None:
        runner = AsyncSubprocessRunner()
        captured: list[ProcessLine] = []

        async def capture(line: ProcessLine) -> None:
            captured.append(line)

        result = await runner.run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout_seconds=2,
            on_output=capture,
        )
        assert result.returncode == 0
        assert {line.stream for line in captured} == {"stdout", "stderr"}

        with pytest.raises(ProcessExecutionTimeoutError):
            await runner.run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=0.05,
            )
        task = asyncio.create_task(
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=5,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
