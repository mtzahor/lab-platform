from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from lab_platform.config import HardwareBenchSettings, JLinkSettings
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
    JLinkCommandFailedError,
    JLinkNotAvailableError,
    JLinkTimeoutError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessResult, ProcessRunner
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader
from lab_platform.real_backend.targets.tooling import last_output, safe_tool_path, verify_firmware


def build_jlink_args(settings: JLinkSettings, command_file: Path) -> list[str]:
    return [
        settings.executable,
        "-device",
        settings.device,
        "-if",
        settings.interface,
        "-speed",
        str(settings.speed_khz),
        "-autoconnect",
        "1",
        "-CommanderScript",
        str(command_file),
    ]


def build_jlink_probe_script() -> tuple[str, ...]:
    return ("ShowEmuList", "connect", "exit")


def build_jlink_reset_script() -> tuple[str, ...]:
    return ("r", "g", "exit")


def build_jlink_flash_script(firmware: FirmwareInput) -> tuple[str, ...]:
    path = safe_tool_path(str(firmware.local_path.resolve()), tool="J-Link Commander")
    return (f'loadfile "{path}"', "r", "g", "exit")


class JLinkTarget:
    """Generic SEGGER J-Link path, initially validated for configured nRF52 devices."""

    def __init__(
        self,
        config: HardwareBenchSettings,
        discovery: SerialPortDiscovery,
        process_runner: ProcessRunner,
        serial_reader: Esp32SerialReader,
    ) -> None:
        self._config = config
        self._settings = config.jlink
        self._discovery = discovery
        self._runner = process_runner
        self._serial = serial_reader
        self._health = TargetHealth(
            bench_id=config.id,
            status=TargetHealthStatus.UNKNOWN,
            chip_type=config.target_type,
            details={"message": "Target has not been probed"},
        )
        self._firmware_version: str | None = None

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def capabilities(self) -> set[str]:
        return {"probe", "flash", "firmware", "reset", "debug", "serial"}

    async def probe(self) -> TargetHealth:
        try:
            result = await self._run_script(build_jlink_probe_script())
        except (JLinkNotAvailableError, JLinkTimeoutError, JLinkCommandFailedError) as exc:
            return self._degraded(exc.code, str(exc))
        serial_port: str | None = None
        with suppress(Exception):
            serial_port = self._discovery.resolve(self._config.connection).device
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type=self._config.target_type,
            serial_port=serial_port,
            details={"probe": last_output(result, "J-Link probe connected")},
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
        result = await self._run_script(build_jlink_flash_script(firmware))
        message = last_output(result, "J-Link programmed and reset the target")
        self._firmware_version = firmware.version
        self._health = self._health.model_copy(
            update={"status": TargetHealthStatus.ONLINE, "details": {"flash": message}}
        )
        yield BackendProgress(
            percent=100,
            message=message,
            firmware_version=firmware.version,
        )

    async def read_serial(self, request: SerialReadRequest) -> AsyncIterator[SerialLine]:
        port = self._discovery.resolve(self._config.connection).device
        lines = await self._serial.read(port, self._config.connection.baud_rate, request)
        for line in lines:
            yield line

    async def reset(self) -> None:
        await self._run_script(build_jlink_reset_script())

    async def debug_status(self) -> dict[str, str]:
        result = await self._run_script(build_jlink_probe_script())
        return {
            "status": "available",
            "transport": self._settings.interface,
            "detail": last_output(result, "J-Link debugger available"),
        }

    async def _run_script(self, commands: Sequence[str]) -> ProcessResult:
        if not commands or any(not line or "\n" in line or "\r" in line for line in commands):
            raise ValueError("J-Link commands must be non-empty single-line strings")
        with TemporaryDirectory(prefix="lab-platform-jlink-") as temporary_directory:
            command_file = Path(temporary_directory) / "commands.jlink"
            await asyncio.to_thread(
                command_file.write_text,
                "\n".join((*commands, "")),
                encoding="utf-8",
            )
            try:
                result = await self._runner.run(
                    build_jlink_args(self._settings, command_file),
                    timeout_seconds=self._settings.timeout_seconds,
                )
            except ProcessExecutableNotFoundError as exc:
                raise JLinkNotAvailableError(
                    f"J-Link tools not installed: {self._settings.executable}"
                ) from exc
            except ProcessExecutionTimeoutError as exc:
                raise JLinkTimeoutError(
                    f"J-Link exceeded {self._settings.timeout_seconds:g} seconds."
                ) from exc
        if result.returncode != 0:
            raise JLinkCommandFailedError(
                last_output(result, "J-Link command failed"),
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
    "JLinkTarget",
    "build_jlink_args",
    "build_jlink_flash_script",
    "build_jlink_probe_script",
    "build_jlink_reset_script",
]
