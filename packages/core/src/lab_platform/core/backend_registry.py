from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from lab_platform.core.backend import LabBackend
from lab_platform.core.errors import BackendNotFoundError, BenchNotFoundError, ConfigurationError
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
)


class DuplicateBackendIdError(ConfigurationError):
    code = "DUPLICATE_BACKEND_ID"


class DuplicateBenchIdError(ConfigurationError):
    code = "BENCH_ID_CONFLICT"


@dataclass(frozen=True, slots=True)
class BackendFailure:
    backend_id: str
    stage: Literal["start", "refresh", "stop"]
    error: Exception

    @property
    def message(self) -> str:
        return str(self.error)


@dataclass(frozen=True, slots=True)
class RegisteredBench:
    backend_id: str
    snapshot: BenchSnapshot


@dataclass(frozen=True, slots=True)
class RegistryRefreshResult:
    benches: tuple[RegisteredBench, ...]
    refreshed_backend_ids: frozenset[str]
    failures: tuple[BackendFailure, ...]

    @property
    def failed_backend_ids(self) -> frozenset[str]:
        return frozenset(failure.backend_id for failure in self.failures)


class BackendRegistry:
    """Composite ``LabBackend`` that routes globally unique bench IDs.

    Discovery is reconciled atomically. A backend that cannot list its benches is
    reported as unavailable, while successful backends remain usable. Ownership
    learned during an earlier successful refresh is retained for a failed backend,
    so a transient inventory failure does not make its bench IDs route elsewhere.
    """

    def __init__(
        self,
        backends: Mapping[str, LabBackend] | Iterable[tuple[str, LabBackend]],
    ) -> None:
        pairs = list(backends.items() if isinstance(backends, Mapping) else backends)
        resolved: dict[str, LabBackend] = {}
        for backend_id, backend in pairs:
            if not backend_id or not backend_id.strip():
                raise ConfigurationError("Backend IDs cannot be empty.")
            if backend_id in resolved:
                raise DuplicateBackendIdError(
                    f"Backend ID {backend_id!r} is registered more than once.",
                    backend_id=backend_id,
                )
            resolved[backend_id] = backend

        self._backends = resolved
        self._bench_owners: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = RegistryRefreshResult((), frozenset(), ())
        self._lifecycle_failures: tuple[BackendFailure, ...] = ()
        self._logger = logging.getLogger("lab-platform.backend.registry")

    @property
    def backend_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    @property
    def ownership(self) -> Mapping[str, str]:
        return MappingProxyType(self._bench_owners)

    @property
    def last_refresh(self) -> RegistryRefreshResult:
        return self._last_refresh

    @property
    def lifecycle_failures(self) -> tuple[BackendFailure, ...]:
        return self._lifecycle_failures

    def get_backend(self, backend_id: str) -> LabBackend:
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise BackendNotFoundError(
                f"Backend {backend_id!r} is not registered.", backend_id=backend_id
            ) from exc

    async def start(self) -> None:
        failures = await self._run_lifecycle("start")
        self._lifecycle_failures = failures
        refresh = await self.refresh()
        if refresh.failures:
            self._lifecycle_failures = failures + refresh.failures

    async def stop(self) -> None:
        self._lifecycle_failures = await self._run_lifecycle("stop")

    async def refresh(self) -> RegistryRefreshResult:
        async with self._refresh_lock:
            listings = await asyncio.gather(
                *(
                    self._list_backend(backend_id, backend)
                    for backend_id, backend in sorted(self._backends.items())
                )
            )

            successful: dict[str, list[BenchSnapshot]] = {}
            failures: list[BackendFailure] = []
            for backend_id, snapshots, error in listings:
                if error is not None:
                    failures.append(BackendFailure(backend_id, "refresh", error))
                elif snapshots is not None:
                    successful[backend_id] = snapshots

            registered: list[RegisteredBench] = []
            seen: dict[str, str] = {}
            for backend_id, snapshots in successful.items():
                for snapshot in snapshots:
                    previous = seen.get(snapshot.id)
                    if previous is not None:
                        raise DuplicateBenchIdError(
                            self._duplicate_message(snapshot.id, previous, backend_id),
                            bench_id=snapshot.id,
                            backend_ids=sorted({previous, backend_id}),
                        )
                    seen[snapshot.id] = backend_id
                    registered.append(RegisteredBench(backend_id, snapshot))

            # Keep ownership for failed backends, but completely reconcile every
            # backend that returned an authoritative listing.
            candidate = {
                bench_id: backend_id
                for bench_id, backend_id in self._bench_owners.items()
                if backend_id not in successful
            }
            for bench_id, backend_id in seen.items():
                previous = candidate.get(bench_id)
                if previous is not None:
                    raise DuplicateBenchIdError(
                        self._duplicate_message(bench_id, previous, backend_id),
                        bench_id=bench_id,
                        backend_ids=sorted({previous, backend_id}),
                    )
                candidate[bench_id] = backend_id

            self._bench_owners = candidate
            result = RegistryRefreshResult(
                benches=tuple(sorted(registered, key=lambda item: item.snapshot.id)),
                refreshed_backend_ids=frozenset(successful),
                failures=tuple(failures),
            )
            self._last_refresh = result
            for failure in failures:
                self._logger.warning(
                    "Backend inventory refresh failed",
                    extra={
                        "backend_id": failure.backend_id,
                        "stage": failure.stage,
                        "error": failure.message,
                    },
                )
            return result

    async def get_backend_for_bench(self, bench_id: str) -> LabBackend:
        backend_id = self._bench_owners.get(bench_id)
        if backend_id is None:
            await self.refresh()
            backend_id = self._bench_owners.get(bench_id)
        if backend_id is None:
            raise BenchNotFoundError(
                f"Bench {bench_id} does not exist in any registered backend.",
                bench_id=bench_id,
            )
        return self._backends[backend_id]

    async def get_backend_id_for_bench(self, bench_id: str) -> str:
        await self.get_backend_for_bench(bench_id)
        backend_id = self._bench_owners.get(bench_id)
        if backend_id is None:  # pragma: no cover - guarded by get_backend_for_bench
            raise BenchNotFoundError(f"Bench {bench_id} does not exist.", bench_id=bench_id)
        return backend_id

    async def list_benches(self) -> list[BenchSnapshot]:
        result = await self.refresh()
        return [registered.snapshot for registered in result.benches]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        backend = await self.get_backend_for_bench(bench_id)
        return await backend.get_bench(bench_id)

    async def power_on(self, bench_id: str) -> None:
        backend = await self.get_backend_for_bench(bench_id)
        await backend.power_on(bench_id)

    async def power_off(self, bench_id: str) -> None:
        backend = await self.get_backend_for_bench(bench_id)
        await backend.power_off(bench_id)

    async def power_cycle(self, bench_id: str) -> None:
        backend = await self.get_backend_for_bench(bench_id)
        await backend.power_cycle(bench_id)

    async def reset(self, bench_id: str) -> None:
        backend = await self.get_backend_for_bench(bench_id)
        await backend.reset(bench_id)

    async def probe(self, bench_id: str) -> TargetHealth:
        backend = await self.get_backend_for_bench(bench_id)
        return await backend.probe(bench_id)

    async def flash_firmware(
        self,
        bench_id: str,
        firmware: FirmwareInput,
    ) -> AsyncIterator[BackendProgress]:
        backend = await self.get_backend_for_bench(bench_id)
        async for progress in backend.flash_firmware(bench_id, firmware):
            yield progress

    async def read_serial(
        self,
        bench_id: str,
        request: SerialReadRequest,
    ) -> AsyncIterator[SerialLine]:
        backend = await self.get_backend_for_bench(bench_id)
        async for line in backend.read_serial(bench_id, request):
            yield line

    async def _run_lifecycle(
        self,
        stage: Literal["start", "stop"],
    ) -> tuple[BackendFailure, ...]:
        results = await asyncio.gather(
            *(
                self._call_lifecycle(backend_id, backend, stage)
                for backend_id, backend in sorted(self._backends.items())
            )
        )
        return tuple(result for result in results if result is not None)

    async def _call_lifecycle(
        self,
        backend_id: str,
        backend: LabBackend,
        stage: Literal["start", "stop"],
    ) -> BackendFailure | None:
        try:
            if stage == "start":
                await backend.start()
            else:
                await backend.stop()
        except Exception as exc:
            self._logger.warning(
                "Backend lifecycle call failed",
                extra={"backend_id": backend_id, "stage": stage, "error": str(exc)},
            )
            return BackendFailure(backend_id, stage, exc)
        return None

    @staticmethod
    async def _list_backend(
        backend_id: str,
        backend: LabBackend,
    ) -> tuple[str, list[BenchSnapshot] | None, Exception | None]:
        try:
            return backend_id, await backend.list_benches(), None
        except Exception as exc:
            return backend_id, None, exc

    @staticmethod
    def _duplicate_message(bench_id: str, first: str, second: str) -> str:
        if first == second:
            return f"Backend {first!r} returned bench ID {bench_id!r} more than once."
        return f"Bench ID {bench_id!r} is exposed by both {first!r} and {second!r}."
