from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import suppress

from lab_platform.config import HardwareBenchSettings, OpenOcdSettings
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
    OpenOcdCommandFailedError,
    OpenOcdNotAvailableError,
    OpenOcdTimeoutError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessResult, ProcessRunner
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader
from lab_platform.real_backend.targets.tooling import last_output, safe_tool_path, verify_firmware


def build_openocd_args(settings: OpenOcdSettings, commands: Sequence[str]) -> list[str]:
    """Build OpenOCD argv without invoking a shell or interpolating config as commands."""

    if not commands:
        raise ValueError("OpenOCD requires at least one command")
    arguments = [
        settings.executable,
        "-f",
        settings.interface_config,
        "-f",
        settings.target_config,
        "-c",
        f"transport select {settings.transport}",
    ]
    for command in commands:
        if not command.strip() or "\n" in command or "\r" in command:
            raise ValueError("OpenOCD commands must be non-empty single-line strings")
        arguments.extend(("-c", command))
    return arguments


def build_openocd_probe_args(settings: OpenOcdSettings) -> list[str]:
    return build_openocd_args(settings, ("init", "targets", "shutdown"))


def build_openocd_reset_args(settings: OpenOcdSettings) -> list[str]:
    return build_openocd_args(settings, ("init", "reset run", "shutdown"))


def build_openocd_flash_args(
    settings: OpenOcdSettings,
    firmware: FirmwareInput,
) -> list[str]:
    path = safe_tool_path(str(firmware.local_path.resolve()), tool="OpenOCD")
    return build_openocd_args(
        settings,
        ("init", f"program {{{path}}} verify reset", "shutdown"),
    )


class OpenOcdTarget:
    """Generic OpenOCD target used initially for an STM32 Nucleo path."""

    def __init__(
        self,
        config: HardwareBenchSettings,
        discovery: SerialPortDiscovery,
        process_runner: ProcessRunner,
        serial_reader: Esp32SerialReader,
    ) -> None:
        self._config = config
        self._settings = config.openocd
        self._discovery = discovery
        self._runner = process_runner
        self._serial = serial_reader
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
        return {"probe", "flash", "firmware", "reset", "debug", "serial"}

    @property
    def serial_port(self) -> str | None:
        return self._health.serial_port

    async def probe(self) -> TargetHealth:
        try:
            result = await self._run(build_openocd_probe_args(self._settings))
        except (OpenOcdNotAvailableError, OpenOcdTimeoutError) as exc:
            return self._degraded(exc.code, str(exc))
        except OpenOcdCommandFailedError as exc:
            return self._degraded(exc.code, str(exc))
        serial_port: str | None = None
        # Serial is useful but is not required to prove debugger/target presence.
        with suppress(Exception):
            serial_port = self._discovery.resolve(self._config.connection).device
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type=self._config.target_type,
            serial_port=serial_port,
            details={
                "probe": "OpenOCD target examination succeeded",
                "output": last_output(result, "target examined"),
            },
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
        await verify_firmware(firmware)
        yield BackendProgress(percent=5, message="Firmware artifact verified")
        result = await self._run(build_openocd_flash_args(self._settings, firmware))
        yield BackendProgress(
            percent=95,
            message=last_output(result, "OpenOCD programmed and verified the target"),
        )
        self._firmware_version = firmware.version
        self._health = self._health.model_copy(
            update={
                "status": TargetHealthStatus.ONLINE,
                "details": {"flash": "OpenOCD program/verify/reset succeeded"},
            }
        )
        yield BackendProgress(
            percent=100,
            message="Firmware programmed and target reset",
            firmware_version=firmware.version,
        )

    async def read_serial(self, request: SerialReadRequest) -> AsyncIterator[SerialLine]:
        port = self._discovery.resolve(self._config.connection).device
        lines = await self._serial.read(port, self._config.connection.baud_rate, request)
        for line in lines:
            yield line

    async def reset(self) -> None:
        await self._run(build_openocd_reset_args(self._settings))
        self._health = self._health.model_copy(
            update={"status": TargetHealthStatus.ONLINE, "details": {"reset": "succeeded"}}
        )

    async def debug_status(self) -> dict[str, str]:
        result = await self._run(build_openocd_probe_args(self._settings))
        return {
            "status": "available",
            "transport": self._settings.transport,
            "detail": last_output(result, "OpenOCD debugger available"),
        }

    async def _run(self, args: Sequence[str]) -> ProcessResult:
        try:
            result = await self._runner.run(
                args,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ProcessExecutableNotFoundError as exc:
            raise OpenOcdNotAvailableError(
                f"OpenOCD executable is not installed: {self._settings.executable}"
            ) from exc
        except ProcessExecutionTimeoutError as exc:
            raise OpenOcdTimeoutError(
                f"OpenOCD exceeded {self._settings.timeout_seconds:g} seconds."
            ) from exc
        if result.returncode != 0:
            raise OpenOcdCommandFailedError(
                last_output(result, "OpenOCD command failed"),
                return_code=result.returncode,
            )
        return result

    def _degraded(self, code: str, message: str) -> TargetHealth:
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.DEGRADED,
            chip_type=self._config.target_type,
            details={"error_code": code, "message": message},
        )
        return self._health


__all__ = [
    "OpenOcdTarget",
    "build_openocd_args",
    "build_openocd_flash_args",
    "build_openocd_probe_args",
    "build_openocd_reset_args",
]
