from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from collections.abc import AsyncIterator

from lab_platform.config import HardwareBenchSettings
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
from lab_platform.real_backend.discovery import SerialPortDiscovery
from lab_platform.real_backend.errors import (
    BootTimeoutError,
    BootVerificationFailedError,
    DeviceNotFoundError,
    EsptoolConnectionFailedError,
    EsptoolNotAvailableError,
    EsptoolTimeoutError,
    FirmwareVerificationFailedError,
    HardwareError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
    SerialPortAmbiguousError,
    SerialPortNotFoundError,
    SerialReadTimeoutError,
    WrongTargetTypeError,
)
from lab_platform.real_backend.process_runner import ProcessRunner
from lab_platform.real_backend.targets.esp32.flasher import Esp32Flasher
from lab_platform.real_backend.targets.esp32.parser import (
    extract_firmware_version,
    matches_any,
    parse_chip_info,
    parse_wrong_chip,
)
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader


def build_probe_args(config: HardwareBenchSettings, port: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        config.flash.chip,
        "--port",
        port,
        "--before",
        config.flash.reset_mode,
        "--after",
        config.flash.after,
        "read-mac",
    ]


class Esp32Target:
    def __init__(
        self,
        config: HardwareBenchSettings,
        discovery: SerialPortDiscovery,
        process_runner: ProcessRunner,
        serial_reader: Esp32SerialReader,
    ) -> None:
        self._config = config
        self._discovery = discovery
        self._runner = process_runner
        self._serial = serial_reader
        self._flasher = Esp32Flasher(config, process_runner)
        self._health = TargetHealth(
            bench_id=config.id,
            status=TargetHealthStatus.UNKNOWN,
            details={"message": "Target has not been probed"},
        )
        self._firmware_version: str | None = None

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def capabilities(self) -> set[str]:
        # ``flash`` is the stable Plugin API 1.0 capability. ``firmware`` is
        # retained as the documented pre-1.0 workflow compatibility alias.
        return {"flash", "firmware", "serial", "probe", "reset"}

    @property
    def serial_port(self) -> str | None:
        return self._health.serial_port

    async def probe(self) -> TargetHealth:
        try:
            port = self._discovery.resolve(self._config.connection).device
        except HardwareError as exc:
            self._health = TargetHealth(
                bench_id=self.id,
                status=TargetHealthStatus.OFFLINE,
                details={"error_code": exc.code, "message": str(exc)},
            )
            return self._health
        except Exception as exc:
            self._health = TargetHealth(
                bench_id=self.id,
                status=TargetHealthStatus.DEGRADED,
                details={
                    "error_code": "SERIAL_PORT_NOT_FOUND",
                    "message": f"Serial discovery failed: {exc}",
                },
            )
            return self._health

        try:
            result = await self._runner.run(
                build_probe_args(self._config, port),
                timeout_seconds=min(30, self._config.flash.timeout_seconds),
            )
        except ProcessExecutableNotFoundError:
            return self._degraded(
                port,
                "ESPTOOL_NOT_AVAILABLE",
                "esptool is not installed in the Agent Python environment.",
            )
        except ProcessExecutionTimeoutError:
            return self._degraded(port, "ESPTOOL_TIMEOUT", "ESP32 probe timed out.")
        except Exception as exc:
            return self._degraded(port, "ESPTOOL_CONNECTION_FAILED", str(exc))

        output = "\n".join((*result.stdout, *result.stderr))
        if result.returncode != 0:
            wrong_chip = parse_wrong_chip(output)
            if wrong_chip is not None:
                return self._degraded(
                    port,
                    "WRONG_TARGET_TYPE",
                    f"Expected {self._config.flash.chip}, detected {wrong_chip}.",
                    chip=wrong_chip,
                )
            return self._degraded(
                port,
                "ESPTOOL_CONNECTION_FAILED",
                _last_output_line(output, "esptool could not communicate with the target"),
            )
        chip, mac = parse_chip_info(output)
        if chip is not None and self._config.flash.chip.upper() not in chip.upper():
            return self._degraded(
                port,
                "WRONG_TARGET_TYPE",
                f"Expected {self._config.flash.chip}, detected {chip}.",
                chip=chip,
                mac=mac,
            )
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type=chip or self._config.flash.chip.upper(),
            mac_address=mac,
            serial_port=port,
            details={"probe": "esptool read-mac succeeded"},
        )
        return self._health

    async def snapshot(self) -> BenchSnapshot:
        online = self._health.status is TargetHealthStatus.ONLINE
        return BenchSnapshot(
            id=self.id,
            name=self._config.name,
            status=BenchStatus.AVAILABLE if online else BenchStatus.OFFLINE,
            online=online,
            powered=None,
            firmware_version=self._firmware_version,
            capabilities=sorted(self.capabilities),
        )

    async def flash(self, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]:
        await self._verify_firmware(firmware)
        yield BackendProgress(percent=5, message="Resolving serial port")
        health = await self.probe()
        yield BackendProgress(percent=10, message="Probing ESP32")
        self._require_online(health)
        port = health.serial_port
        if port is None:  # pragma: no cover - online health invariant
            raise DeviceNotFoundError("The ESP32 serial port could not be resolved.")

        try:
            async for progress in self._flash_online(port, firmware):
                yield progress
        except asyncio.CancelledError:
            self._firmware_version = None
            self._health = self._health.model_copy(
                update={
                    "status": TargetHealthStatus.UNKNOWN,
                    "details": {
                        "error_code": "PROCESS_CANCELLED",
                        "message": "Flash was cancelled; probe the target before reuse.",
                    },
                }
            )
            raise
        except HardwareError as exc:
            self._firmware_version = None
            self._health = self._health.model_copy(
                update={
                    "status": TargetHealthStatus.DEGRADED,
                    "details": {"error_code": exc.code, "message": str(exc)},
                }
            )
            raise

    async def _flash_online(
        self, port: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:

        yield BackendProgress(percent=20, message="Connecting to bootloader")
        async for progress in self._flasher.flash(port, firmware):
            yield progress
        yield BackendProgress(percent=95, message="Resetting target")

        boot_request = SerialReadRequest(
            timeout_seconds=self._config.boot.timeout_seconds,
            until_pattern=self._config.boot.ready_pattern,
            max_lines=500,
        )
        try:
            lines = await self._serial.read(
                port,
                self._config.connection.baud_rate,
                boot_request,
            )
        except SerialReadTimeoutError as exc:
            captured = _captured_serial_lines(exc)
            if captured:
                yield BackendProgress(
                    percent=96,
                    message="Boot verification timed out",
                    serial_lines=captured,
                )
            self._health = self._health.model_copy(update={"status": TargetHealthStatus.DEGRADED})
            raise BootTimeoutError(
                f"ESP32 did not emit {self._config.boot.ready_pattern!r} before timeout.",
                bench_id=self.id,
            ) from exc
        except HardwareError as exc:
            captured = _captured_serial_lines(exc)
            if captured:
                yield BackendProgress(
                    percent=96,
                    message="Boot verification interrupted",
                    serial_lines=captured,
                )
            raise

        yield BackendProgress(
            percent=96,
            message="Validating boot output",
            serial_lines=lines,
        )

        version: str | None = None
        for line in lines:
            failure = matches_any(line.text, self._config.boot.failure_patterns)
            if failure is not None:
                raise BootVerificationFailedError(
                    f"ESP32 boot output matched failure pattern {failure!r}.",
                    bench_id=self.id,
                )
            extracted = extract_firmware_version(line.text, self._config.boot.version_pattern)
            version = extracted or version
        if not any(re.search(self._config.boot.ready_pattern, line.text) for line in lines):
            raise BootVerificationFailedError(
                f"ESP32 boot output did not contain {self._config.boot.ready_pattern!r}.",
                bench_id=self.id,
            )
        self._firmware_version = version or firmware.version
        yield BackendProgress(
            percent=100,
            message="READY detected",
            firmware_version=self._firmware_version,
        )

    async def read_serial(self, request: SerialReadRequest) -> AsyncIterator[SerialLine]:
        port = self._discovery.resolve(self._config.connection).device
        try:
            lines = await self._serial.read(port, self._config.connection.baud_rate, request)
        except HardwareError as exc:
            for line in _captured_serial_lines(exc):
                yield line
            raise
        for line in lines:
            yield line

    async def reset(self) -> None:
        health = await self.probe()
        self._require_online(health)

    async def _verify_firmware(self, firmware: FirmwareInput) -> None:
        if not firmware.local_path.is_file():
            raise FirmwareVerificationFailedError(
                f"Firmware file {firmware.filename} no longer exists.",
                filename=firmware.filename,
            )

        def calculate() -> tuple[str, int]:
            digest = hashlib.sha256()
            size = 0
            with firmware.local_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            return digest.hexdigest(), size

        checksum, size = await asyncio.to_thread(calculate)
        if checksum != firmware.sha256 or size != firmware.size_bytes:
            raise FirmwareVerificationFailedError(
                "Firmware checksum or size changed after upload.",
                expected_sha256=firmware.sha256,
                actual_sha256=checksum,
                expected_size=firmware.size_bytes,
                actual_size=size,
            )

    def _require_online(self, health: TargetHealth) -> None:
        if health.status is TargetHealthStatus.ONLINE:
            return
        code = health.details.get("error_code", "ESPTOOL_CONNECTION_FAILED")
        message = health.details.get("message", "ESP32 is not online.")
        errors: dict[str, type[HardwareError]] = {
            "DEVICE_NOT_FOUND": DeviceNotFoundError,
            "SERIAL_PORT_NOT_FOUND": SerialPortNotFoundError,
            "SERIAL_PORT_AMBIGUOUS": SerialPortAmbiguousError,
            "ESPTOOL_NOT_AVAILABLE": EsptoolNotAvailableError,
            "ESPTOOL_TIMEOUT": EsptoolTimeoutError,
            "WRONG_TARGET_TYPE": WrongTargetTypeError,
        }
        raise errors.get(code, EsptoolConnectionFailedError)(message, bench_id=self.id)

    def _degraded(
        self,
        port: str,
        code: str,
        message: str,
        *,
        chip: str | None = None,
        mac: str | None = None,
    ) -> TargetHealth:
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.DEGRADED,
            chip_type=chip,
            mac_address=mac,
            serial_port=port,
            details={"error_code": code, "message": message},
        )
        return self._health


def _last_output_line(output: str, fallback: str) -> str:
    return next((line for line in reversed(output.splitlines()) if line.strip()), fallback)


def _captured_serial_lines(exc: HardwareError) -> list[SerialLine]:
    return [
        SerialLine.model_validate(item)
        for item in exc.details.get("captured_lines", [])
        if isinstance(item, dict)
    ]
