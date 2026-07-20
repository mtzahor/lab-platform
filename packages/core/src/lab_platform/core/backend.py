from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from typing import Protocol

from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
)

_operation_id: ContextVar[str | None] = ContextVar("lab_platform_operation_id", default=None)


def bind_operation_id(operation_id: str) -> Token[str | None]:
    return _operation_id.set(operation_id)


def reset_operation_id(token: Token[str | None]) -> None:
    _operation_id.reset(token)


def current_operation_id() -> str | None:
    return _operation_id.get()


class LabBackend(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def list_benches(self) -> list[BenchSnapshot]: ...

    async def get_bench(self, bench_id: str) -> BenchSnapshot: ...

    async def power_on(self, bench_id: str) -> None: ...

    async def power_off(self, bench_id: str) -> None: ...

    async def power_cycle(self, bench_id: str) -> None: ...

    async def reset(self, bench_id: str) -> None: ...

    async def probe(self, bench_id: str) -> TargetHealth: ...

    def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]: ...

    def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]: ...
