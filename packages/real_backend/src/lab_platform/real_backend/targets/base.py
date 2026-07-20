from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
)


class PhysicalTarget(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def capabilities(self) -> set[str]: ...

    async def probe(self) -> TargetHealth: ...

    async def snapshot(self) -> BenchSnapshot: ...

    def flash(self, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]: ...

    def read_serial(self, request: SerialReadRequest) -> AsyncIterator[SerialLine]: ...

    async def reset(self) -> None: ...
