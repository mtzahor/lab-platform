from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from lab_platform.real_backend.errors import (
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)


@dataclass(frozen=True, slots=True)
class ProcessLine:
    stream: Literal["stdout", "stderr"]
    text: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: tuple[str, ...]
    stderr: tuple[str, ...]


OutputCallback = Callable[[ProcessLine], Awaitable[None]]


class ProcessRunner:
    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        raise NotImplementedError


class AsyncSubprocessRunner(ProcessRunner):
    def __init__(self, *, maximum_capture_bytes: int = 1024 * 1024) -> None:
        if maximum_capture_bytes <= 0:
            raise ValueError("Process output capture limit must be positive")
        self._maximum_capture_bytes = maximum_capture_bytes

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        on_output: OutputCallback | None = None,
    ) -> ProcessResult:
        if not args:
            raise ValueError("Process arguments cannot be empty")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProcessExecutableNotFoundError(str(exc)) from exc

        stdout = _BoundedLines(self._maximum_capture_bytes)
        stderr = _BoundedLines(self._maximum_capture_bytes)

        async def pump(
            stream: asyncio.StreamReader | None,
            name: Literal["stdout", "stderr"],
            destination: _BoundedLines,
        ) -> None:
            if stream is None:  # pragma: no cover - pipes are requested above
                return
            while raw := await stream.readline():
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                destination.append(line)
                if on_output is not None:
                    await on_output(ProcessLine(name, line))

        tasks = [
            asyncio.create_task(pump(process.stdout, "stdout", stdout)),
            asyncio.create_task(pump(process.stderr, "stderr", stderr)),
        ]
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            await asyncio.gather(*tasks)
        except TimeoutError as exc:
            await self._terminate(process)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise ProcessExecutionTimeoutError(
                f"Process exceeded {timeout_seconds:g} seconds"
            ) from exc
        except asyncio.CancelledError:
            await self._terminate(process)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return ProcessResult(process.returncode or 0, stdout.items, stderr.items)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()


class _BoundedLines:
    """Keep only the most recent decoded process output within a byte budget."""

    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._items: deque[tuple[str, int]] = deque()
        self._size = 0

    @property
    def items(self) -> tuple[str, ...]:
        return tuple(text for text, _size in self._items)

    def append(self, text: str) -> None:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) + 1 > self._maximum_bytes:
            available = self._maximum_bytes - 1
            encoded = encoded[-available:] if available else b""
            text = encoded.decode("utf-8", errors="replace")
        size = len(text.encode("utf-8", errors="replace")) + 1
        self._items.append((text, size))
        self._size += size
        while self._items and self._size > self._maximum_bytes:
            _removed, removed_size = self._items.popleft()
            self._size -= removed_size
