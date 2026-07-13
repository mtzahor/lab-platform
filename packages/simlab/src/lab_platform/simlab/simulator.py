from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from lab_platform.models import Bench, BenchStatus, Device

_CAPABILITY_PATTERNS = (
    ("Power", "Serial", "Firmware"),
    ("Power", "Serial"),
    ("Power", "Serial", "Firmware", "Debugger"),
    ("Power",),
    ("Power", "Serial", "Firmware"),
)


class SimLabError(RuntimeError):
    pass


class SimLabBenchNotFound(SimLabError):
    pass


class SimLabBenchOffline(SimLabError):
    pass


class SimLabUnsupportedCapability(SimLabError):
    pass


class SimLabInjectedFailure(SimLabError):
    pass


class SimLabTimeout(SimLabError):
    pass


@dataclass(frozen=True, slots=True)
class SimulatedBenchSnapshot:
    id: str
    name: str
    online: bool
    powered: bool
    firmware_version: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimulatedProgress:
    percent: int
    message: str


@dataclass(slots=True)
class _SimulatedBench:
    id: str
    name: str
    online: bool
    powered: bool
    firmware_version: str | None
    capabilities: tuple[str, ...]
    failure: str | None = None


class SimLab:
    """Deterministic, stateful simulator. Mutable objects never leave this class."""

    def __init__(
        self,
        enabled: bool = True,
        bench_count: int = 5,
        *,
        clock_mode: str = "accelerated",
        speed_multiplier: float = 20.0,
        flash_duration_seconds: float = 5.0,
    ) -> None:
        self._enabled = enabled
        self._bench_count = bench_count
        self._clock_mode = clock_mode
        self._speed_multiplier = speed_multiplier
        self._flash_duration_seconds = flash_duration_seconds
        self._started = False
        self._clock = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []
        self._benches: dict[str, _SimulatedBench] = {}

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self._enabled:
            return
        width = max(2, len(str(self._bench_count)))
        for index in range(self._bench_count):
            bench_id = f"bench-{index + 1:0{width}d}"
            self._benches[bench_id] = _SimulatedBench(
                id=bench_id,
                name=f"Virtual Bench {index + 1:0{width}d}",
                online=True,
                powered=index % 2 == 0,
                firmware_version=f"1.{index}.0",
                capabilities=_CAPABILITY_PATTERNS[index % len(_CAPABILITY_PATTERNS)],
            )

    async def shutdown(self) -> None:
        for _, waiter in self._waiters:
            if not waiter.done():
                waiter.cancel()
        self._waiters.clear()
        self._benches.clear()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def benches(self) -> list[Bench]:
        """Phase 0-compatible immutable view."""
        return [
            Bench(
                name=bench.id,
                status=BenchStatus.ONLINE if bench.online else BenchStatus.OFFLINE,
                capabilities=list(bench.capabilities),
                devices=[Device(name=f"{bench.id}-controller", kind="controller")],
            )
            for bench in self._ordered_benches()
        ]

    def bench_snapshots(self) -> list[SimulatedBenchSnapshot]:
        return [self._snapshot(bench) for bench in self._ordered_benches()]

    def get_bench(self, bench_id: str) -> SimulatedBenchSnapshot:
        return self._snapshot(self._require_bench(bench_id))

    async def power_on(self, bench_id: str) -> None:
        bench = self._require_controllable(bench_id, "Power")
        self._raise_failure(bench)
        bench.powered = True

    async def power_off(self, bench_id: str) -> None:
        bench = self._require_controllable(bench_id, "Power")
        self._raise_failure(bench)
        bench.powered = False

    async def power_cycle(self, bench_id: str) -> None:
        bench = self._require_controllable(bench_id, "Power")
        self._raise_failure(bench)
        bench.powered = False
        await self._wait(2.0)
        self._raise_failure(bench)
        bench.powered = True

    async def flash_firmware(
        self,
        bench_id: str,
        *,
        version: str | None,
        sha256: str,
    ) -> AsyncIterator[SimulatedProgress]:
        bench = self._require_controllable(bench_id, "Firmware")
        if bench.failure == "checksum_failure":
            bench.failure = None
            raise SimLabInjectedFailure("Injected firmware checksum failure")
        stages = (
            (0, "Waiting to start"),
            (20, "Connecting debugger"),
            (40, "Erasing target"),
            (60, "Programming firmware"),
            (80, "Verifying image"),
            (100, "Flash completed"),
        )
        delay = self._flash_duration_seconds / max(1, len(stages) - 1)
        for index, (percent, message) in enumerate(stages):
            if index:
                await self._wait(delay)
            if bench.failure in {"flash_failure", "usb_disconnect"} and percent >= 60:
                failure = bench.failure
                bench.failure = None
                raise SimLabInjectedFailure(f"Injected {failure.replace('_', ' ')}")
            self._raise_failure(bench)
            yield SimulatedProgress(percent, message)
        bench.firmware_version = version or f"sha256:{sha256[:12]}"

    def inject_failure(self, bench_id: str, failure: str) -> None:
        supported = {
            "flash_failure",
            "usb_disconnect",
            "kernel_panic",
            "boot_failure",
            "overheat",
            "backend_timeout",
            "checksum_failure",
        }
        normalized = failure.lower()
        if normalized not in supported:
            raise ValueError(f"Unsupported SimLab failure: {failure}")
        self._require_bench(bench_id).failure = normalized

    def set_online(self, bench_id: str, online: bool) -> None:
        self._require_bench(bench_id).online = online

    def tick(self, seconds: float = 1.0) -> None:
        if seconds < 0:
            raise ValueError("Clock cannot move backwards")
        self._clock += seconds
        remaining: list[tuple[float, asyncio.Future[None]]] = []
        for target, waiter in self._waiters:
            if target <= self._clock and not waiter.done():
                waiter.set_result(None)
            elif not waiter.done():
                remaining.append((target, waiter))
        self._waiters = remaining

    async def _wait(self, simulated_seconds: float) -> None:
        if self._clock_mode == "manual":
            target = self._clock + simulated_seconds
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append((target, waiter))
            await waiter
            return
        await asyncio.sleep(simulated_seconds / self._speed_multiplier)
        self._clock += simulated_seconds

    def _ordered_benches(self) -> list[_SimulatedBench]:
        if not self._enabled or not self._started:
            return []
        return [self._benches[key] for key in sorted(self._benches)]

    def _require_bench(self, bench_id: str) -> _SimulatedBench:
        if not self._started:
            raise SimLabError("SimLab is not started")
        try:
            return self._benches[bench_id]
        except KeyError as exc:
            raise SimLabBenchNotFound(f"Bench {bench_id} does not exist") from exc

    def _require_controllable(self, bench_id: str, capability: str) -> _SimulatedBench:
        bench = self._require_bench(bench_id)
        if not bench.online:
            raise SimLabBenchOffline(f"Bench {bench_id} is offline")
        if capability not in bench.capabilities:
            raise SimLabUnsupportedCapability(
                f"Bench {bench_id} does not support {capability.lower()}"
            )
        return bench

    @staticmethod
    def _snapshot(bench: _SimulatedBench) -> SimulatedBenchSnapshot:
        return SimulatedBenchSnapshot(
            id=bench.id,
            name=bench.name,
            online=bench.online,
            powered=bench.powered,
            firmware_version=bench.firmware_version,
            capabilities=bench.capabilities,
        )

    @staticmethod
    def _raise_failure(bench: _SimulatedBench) -> None:
        if bench.failure == "backend_timeout":
            bench.failure = None
            raise SimLabTimeout("Injected backend timeout")
        if bench.failure in {"kernel_panic", "boot_failure", "overheat"}:
            failure = bench.failure
            bench.failure = None
            raise SimLabInjectedFailure(f"Injected {failure.replace('_', ' ')}")
