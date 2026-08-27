from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress

from lab_platform.config import HardwareBenchSettings, Rp2040Settings
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
    FirmwareVerificationFailedError,
    PicotoolCommandFailedError,
    PicotoolNotAvailableError,
    PicotoolTimeoutError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessResult, ProcessRunner
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader
from lab_platform.real_backend.targets.tooling import last_output, safe_tool_path, verify_firmware


def build_picotool_probe_args(settings: Rp2040Settings) -> list[str]:
    return [settings.executable, "info", "-a"]


def build_picotool_flash_args(
    settings: Rp2040Settings,
    firmware: FirmwareInput,
) -> list[str]:
    path = safe_tool_path(str(firmware.local_path.resolve()), tool="picotool")
    return [settings.executable, "load", path, "-f"]


def build_picotool_reset_args(settings: Rp2040Settings) -> list[str]:
    return [settings.executable, "reboot", "-f"]


class Rp2040Target:
    """RP2040 target supporting picotool and mounted UF2 workflows."""

    def __init__(
        self,
        config: HardwareBenchSettings,
        discovery: SerialPortDiscovery,
        process_runner: ProcessRunner,
        serial_reader: Esp32SerialReader,
    ) -> None:
        self._config = config
        self._settings = config.rp2040
        self._discovery = discovery
        self._runner = process_runner
        self._serial = serial_reader
        self._health = TargetHealth(
            bench_id=config.id,
            status=TargetHealthStatus.UNKNOWN,
            chip_type="rp2040",
            details={"message": "Target has not been probed"},
        )
        self._firmware_version: str | None = None

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def capabilities(self) -> set[str]:
        capabilities = {"probe", "flash", "firmware", "serial"}
        if self._settings.tool == "picotool":
            capabilities.add("reset")
        return capabilities

    async def probe(self) -> TargetHealth:
        if self._settings.tool == "uf2":
            mount = self._settings.mount_path
            if mount is None or not mount.is_dir():
                return self._degraded(
                    "RP2040_UF2_VOLUME_NOT_FOUND",
                    "The configured RP2040 UF2 volume is not mounted.",
                )
            detail = f"UF2 volume available at {mount}"
        else:
            try:
                result = await self._run(build_picotool_probe_args(self._settings))
            except (
                PicotoolNotAvailableError,
                PicotoolTimeoutError,
                PicotoolCommandFailedError,
            ) as exc:
                return self._degraded(exc.code, str(exc))
            detail = last_output(result, "picotool detected an RP2040 target")
        serial_port: str | None = None
        with suppress(Exception):
            serial_port = self._discovery.resolve(self._config.connection).device
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type="rp2040",
            serial_port=serial_port,
            details={"probe": detail},
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
        if self._settings.tool == "uf2":
            if firmware.local_path.suffix.casefold() != ".uf2":
                raise FirmwareVerificationFailedError(
                    "Mounted-volume RP2040 flashing requires a .uf2 artifact.",
                    filename=firmware.filename,
                )
            mount = self._settings.mount_path
            if mount is None or not mount.is_dir():
                raise PicotoolNotAvailableError("The configured RP2040 UF2 volume is unavailable.")
            destination = mount / firmware.local_path.name
            await asyncio.to_thread(shutil.copyfile, firmware.local_path, destination)
            message = f"UF2 copied to {mount}"
        else:
            result = await self._run(build_picotool_flash_args(self._settings, firmware))
            message = last_output(result, "picotool loaded the firmware")
            await self._run(build_picotool_reset_args(self._settings))
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
        if self._settings.tool == "uf2":
            raise PicotoolNotAvailableError(
                "Reset requires picotool; UF2 mounted-volume mode only provides flashing."
            )
        await self._run(build_picotool_reset_args(self._settings))

    async def _run(self, args: Sequence[str]) -> ProcessResult:
        try:
            result = await self._runner.run(
                args,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ProcessExecutableNotFoundError as exc:
            raise PicotoolNotAvailableError(
                f"picotool executable is not installed: {self._settings.executable}"
            ) from exc
        except ProcessExecutionTimeoutError as exc:
            raise PicotoolTimeoutError(
                f"picotool exceeded {self._settings.timeout_seconds:g} seconds."
            ) from exc
        if result.returncode != 0:
            raise PicotoolCommandFailedError(
                last_output(result, "picotool command failed"),
                return_code=result.returncode,
            )
        return result

    def _degraded(self, code: str, message: str) -> TargetHealth:
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.DEGRADED,
            chip_type="rp2040",
            details={"error_code": code, "message": message},
        )
        return self._health


__all__ = [
    "Rp2040Target",
    "build_picotool_flash_args",
    "build_picotool_probe_args",
    "build_picotool_reset_args",
]
