from __future__ import annotations

import asyncio
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

        stdout: list[str] = []
        stderr: list[str] = []

        async def pump(
            stream: asyncio.StreamReader | None,
            name: Literal["stdout", "stderr"],
            destination: list[str],
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
        return ProcessResult(process.returncode or 0, tuple(stdout), tuple(stderr))

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
