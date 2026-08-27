from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import suppress

from lab_platform.config import HardwareBenchSettings, Nrf52Settings
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
    NordicToolCommandFailedError,
    NordicToolNotAvailableError,
    NordicToolTimeoutError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessResult, ProcessRunner
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader
from lab_platform.real_backend.targets.tooling import last_output, safe_tool_path, verify_firmware


def build_nrfjprog_probe_args(settings: Nrf52Settings) -> list[str]:
    return [settings.executable, "--ids", "--family", settings.family]


def build_nrfjprog_flash_args(
    settings: Nrf52Settings,
    firmware: FirmwareInput,
) -> list[str]:
    path = safe_tool_path(str(firmware.local_path.resolve()), tool="nrfjprog")
    return [
        settings.executable,
        "--program",
        path,
        "--verify",
        "--reset",
        "--family",
        settings.family,
    ]


def build_nrfjprog_reset_args(settings: Nrf52Settings) -> list[str]:
    return [settings.executable, "--reset", "--family", settings.family]


class Nrf52Target:
    """nRF51/nRF52/nRF53 programming through Nordic command-line tooling."""

    def __init__(
        self,
        config: HardwareBenchSettings,
        discovery: SerialPortDiscovery,
        process_runner: ProcessRunner,
        serial_reader: Esp32SerialReader,
    ) -> None:
        self._config = config
        self._settings = config.nrf52
        self._discovery = discovery
        self._runner = process_runner
        self._serial = serial_reader
        self._health = TargetHealth(
            bench_id=config.id,
            status=TargetHealthStatus.UNKNOWN,
            chip_type="nrf52",
            details={"message": "Target has not been probed"},
        )
        self._firmware_version: str | None = None

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def capabilities(self) -> set[str]:
        return {"probe", "flash", "firmware", "reset", "serial"}

    async def probe(self) -> TargetHealth:
        try:
            result = await self._run(build_nrfjprog_probe_args(self._settings))
        except (
            NordicToolNotAvailableError,
            NordicToolTimeoutError,
            NordicToolCommandFailedError,
        ) as exc:
            return self._degraded(exc.code, str(exc))
        serial_port: str | None = None
        with suppress(Exception):
            serial_port = self._discovery.resolve(self._config.connection).device
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.ONLINE,
            chip_type="nrf52",
            serial_port=serial_port,
            details={"probe": last_output(result, "Nordic probe detected")},
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
        result = await self._run(build_nrfjprog_flash_args(self._settings, firmware))
        message = last_output(result, "Nordic tooling programmed, verified, and reset the target")
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
        await self._run(build_nrfjprog_reset_args(self._settings))

    async def _run(self, args: Sequence[str]) -> ProcessResult:
        try:
            result = await self._runner.run(
                args,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except ProcessExecutableNotFoundError as exc:
            raise NordicToolNotAvailableError(
                f"Nordic tooling is not installed: {self._settings.executable}"
            ) from exc
        except ProcessExecutionTimeoutError as exc:
            raise NordicToolTimeoutError(
                f"Nordic tooling exceeded {self._settings.timeout_seconds:g} seconds."
            ) from exc
        if result.returncode != 0:
            raise NordicToolCommandFailedError(
                last_output(result, "Nordic tool command failed"),
                return_code=result.returncode,
            )
        return result

    def _degraded(self, code: str, message: str) -> TargetHealth:
        self._health = TargetHealth(
            bench_id=self.id,
            status=TargetHealthStatus.DEGRADED,
            chip_type="nrf52",
            details={"error_code": code, "message": message},
        )
        return self._health


__all__ = [
    "Nrf52Target",
    "build_nrfjprog_flash_args",
    "build_nrfjprog_probe_args",
    "build_nrfjprog_reset_args",
]
