from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from lab_platform.core.errors import (
    BackendFailureError,
    BackendTimeoutError,
    BenchNotFoundError,
    BenchOfflineError,
    CapabilityNotSupportedError,
    SimulationFailureError,
)
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.simlab import (
    SimLab,
    SimLabBenchNotFound,
    SimLabBenchOffline,
    SimLabError,
    SimLabInjectedFailure,
    SimLabTimeout,
    SimLabUnsupportedCapability,
)
from lab_platform.simlab_adapter.mapping import map_simlab_bench


class SimLabBackend:
    def __init__(
        self,
        *,
        enabled: bool = True,
        bench_count: int = 5,
        clock_mode: str = "accelerated",
        speed_multiplier: float = 20.0,
        flash_duration_seconds: float = 5.0,
    ) -> None:
        self._simulator = SimLab(
            enabled=enabled,
            bench_count=bench_count,
            clock_mode=clock_mode,
            speed_multiplier=speed_multiplier,
            flash_duration_seconds=flash_duration_seconds,
        )
        self._logger = logging.getLogger("lab-platform.backend.simlab")

    @property
    def simulator(self) -> SimLab:
        """Development/testing control surface; never used by application services."""
        return self._simulator

    async def start(self) -> None:
        await self._simulator.start()

    async def stop(self) -> None:
        await self._simulator.shutdown()

    async def list_benches(self) -> list[BenchSnapshot]:
        return [map_simlab_bench(bench) for bench in self._simulator.bench_snapshots()]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        try:
            return map_simlab_bench(self._simulator.get_bench(bench_id))
        except Exception as exc:
            raise _translate_error(exc, bench_id) from exc

    async def power_on(self, bench_id: str) -> None:
        await self._call("power", "power_on", self._simulator.power_on, bench_id)

    async def power_off(self, bench_id: str) -> None:
        await self._call("power", "power_off", self._simulator.power_off, bench_id)

    async def power_cycle(self, bench_id: str) -> None:
        await self._call("power", "power_cycle", self._simulator.power_cycle, bench_id)

    async def reset(self, bench_id: str) -> None:
        await self._call("reset", "reset", self._simulator.reset, bench_id)

    async def probe(self, bench_id: str) -> TargetHealth:
        try:
            bench = self._simulator.get_bench(bench_id)
        except Exception as exc:
            raise _translate_error(exc, bench_id) from exc
        return TargetHealth(
            bench_id=bench_id,
            status=(TargetHealthStatus.ONLINE if bench.online else TargetHealthStatus.OFFLINE),
            chip_type="SimLab",
            serial_port=f"sim://{bench_id}",
            details={"backend": "simlab"},
        )

    async def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:
        started = time.perf_counter()
        try:
            async for progress in self._simulator.flash_firmware(
                bench_id,
                version=firmware.version,
                sha256=firmware.sha256,
            ):
                yield BackendProgress(percent=progress.percent, message=progress.message)
        except Exception as exc:
            self._log_call(bench_id, "firmware", "flash", started, "failure")
            raise _translate_error(exc, bench_id) from exc
        else:
            self._log_call(bench_id, "firmware", "flash", started, "success")

    async def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]:
        started = time.perf_counter()
        try:
            async for text in self._simulator.read_serial(
                bench_id,
                until_pattern=request.until_pattern,
                max_lines=request.max_lines,
            ):
                yield SerialLine(timestamp=datetime.now(UTC), text=text)
        except Exception as exc:
            self._log_call(bench_id, "serial", "read", started, "failure")
            raise _translate_error(exc, bench_id) from exc
        else:
            self._log_call(bench_id, "serial", "read", started, "success")

    async def _call(
        self,
        capability: str,
        action_name: str,
        action: object,
        bench_id: str,
    ) -> None:
        started = time.perf_counter()
        try:
            await action(bench_id)  # type: ignore[operator]
        except Exception as exc:
            self._log_call(bench_id, capability, action_name, started, "failure")
            raise _translate_error(exc, bench_id) from exc
        self._log_call(bench_id, capability, action_name, started, "success")

    def _log_call(
        self,
        bench_id: str,
        capability: str,
        action: str,
        started: float,
        result: str,
    ) -> None:
        self._logger.info(
            "Backend call",
            extra={
                "backend_type": "simlab",
                "bench_id": bench_id,
                "capability": capability,
                "action": action,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "result": result,
            },
        )


def _translate_error(exc: Exception, bench_id: str) -> Exception:
    if isinstance(exc, SimLabBenchNotFound):
        return BenchNotFoundError(str(exc), bench_id=bench_id)
    if isinstance(exc, SimLabBenchOffline):
        return BenchOfflineError(str(exc), bench_id=bench_id)
    if isinstance(exc, SimLabUnsupportedCapability):
        return CapabilityNotSupportedError(str(exc), bench_id=bench_id)
    if isinstance(exc, SimLabTimeout):
        return BackendTimeoutError(str(exc), bench_id=bench_id)
    if isinstance(exc, SimLabInjectedFailure):
        return SimulationFailureError(str(exc), bench_id=bench_id)
    if isinstance(exc, SimLabError):
        return BackendFailureError(str(exc), bench_id=bench_id)
    if isinstance(exc, (BenchNotFoundError, BenchOfflineError, CapabilityNotSupportedError)):
        return exc
    return BackendFailureError(str(exc), bench_id=bench_id)
