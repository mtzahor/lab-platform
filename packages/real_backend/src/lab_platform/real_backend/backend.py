from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from lab_platform.config import HardwareSettings
from lab_platform.core.backend import current_operation_id
from lab_platform.core.errors import (
    BenchNotFoundError,
    CapabilityNotSupportedError,
    ConfigurationError,
)
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
)
from lab_platform.real_backend.discovery import SerialPortDiscovery, SerialPortProvider
from lab_platform.real_backend.process_runner import AsyncSubprocessRunner, ProcessRunner
from lab_platform.real_backend.targets import PhysicalTarget
from lab_platform.real_backend.targets.esp32 import Esp32Target
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader, SerialFactory


class RealLabBackend:
    def __init__(self, benches: dict[str, PhysicalTarget]) -> None:
        self._benches = benches
        self._started = False
        self._logger = logging.getLogger("lab-platform.backend.real")

    @classmethod
    def from_config(
        cls,
        config: HardwareSettings,
        *,
        process_runner: ProcessRunner | None = None,
        serial_port_provider: SerialPortProvider | None = None,
        serial_factory: SerialFactory | None = None,
    ) -> RealLabBackend:
        if not config.benches:
            raise ConfigurationError("The real backend requires one configured hardware bench.")
        runner = process_runner or AsyncSubprocessRunner()
        discovery = SerialPortDiscovery(serial_port_provider)
        reader = (
            Esp32SerialReader(serial_factory) if serial_factory is not None else Esp32SerialReader()
        )
        targets: dict[str, PhysicalTarget] = {}
        for bench in config.benches:
            if bench.target_type.lower() != "esp32":
                raise ConfigurationError(
                    f"Unsupported physical target type: {bench.target_type}",
                    bench_id=bench.id,
                    target_type=bench.target_type,
                )
            targets[bench.id] = Esp32Target(bench, discovery, runner, reader)
        return cls(targets)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for target in self._benches.values():
            await target.probe()

    async def stop(self) -> None:
        self._started = False

    async def list_benches(self) -> list[BenchSnapshot]:
        return [await self._benches[key].snapshot() for key in sorted(self._benches)]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return await self._target(bench_id).snapshot()

    async def power_on(self, bench_id: str) -> None:
        self._unsupported_power(bench_id, "power_on")

    async def power_off(self, bench_id: str) -> None:
        self._unsupported_power(bench_id, "power_off")

    async def power_cycle(self, bench_id: str) -> None:
        self._unsupported_power(bench_id, "power_cycle")

    async def reset(self, bench_id: str) -> None:
        started = time.perf_counter()
        target = self._target(bench_id)
        result = "failure"
        try:
            await target.reset()
            result = "success"
        finally:
            self._log(bench_id, "reset", started, result=result)

    async def probe(self, bench_id: str) -> TargetHealth:
        started = time.perf_counter()
        result = "failure"
        try:
            health = await self._target(bench_id).probe()
            result = health.status.value
            return health
        finally:
            self._log(bench_id, "probe", started, result=result)

    async def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:
        started = time.perf_counter()
        result = "failure"
        try:
            async for progress in self._target(bench_id).flash(firmware):
                yield progress
            result = "success"
        finally:
            self._log(
                bench_id,
                "flash_firmware",
                started,
                result=result,
                firmware_sha256=firmware.sha256,
            )

    async def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]:
        started = time.perf_counter()
        result = "failure"
        try:
            async for line in self._target(bench_id).read_serial(request):
                yield line
            result = "success"
        finally:
            self._log(bench_id, "serial_read", started, result=result)

    def _target(self, bench_id: str) -> PhysicalTarget:
        try:
            return self._benches[bench_id]
        except KeyError as exc:
            raise BenchNotFoundError(
                f"Bench {bench_id} does not exist.", bench_id=bench_id
            ) from exc

    def _unsupported_power(self, bench_id: str, capability: str) -> None:
        self._target(bench_id)
        raise CapabilityNotSupportedError(
            f"Bench {bench_id} does not support {capability} without a power relay.",
            bench_id=bench_id,
            capability=capability,
        )

    def _log(
        self,
        bench_id: str,
        operation_type: str,
        started: float,
        result: str,
        firmware_sha256: str | None = None,
    ) -> None:
        self._logger.info(
            "Physical backend operation",
            extra={
                "backend_type": "real",
                "bench_id": bench_id,
                "target_type": "esp32",
                "serial_port": getattr(self._benches.get(bench_id), "serial_port", None),
                "operation_id": current_operation_id(),
                "operation_type": operation_type,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "result": result,
                "firmware_sha256": firmware_sha256,
            },
        )
