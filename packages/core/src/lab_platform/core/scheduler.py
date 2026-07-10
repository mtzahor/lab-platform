from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class Scheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule_once(
        self,
        name: str,
        task_factory: Callable[[], Coroutine[Any, Any, None]],
        delay_seconds: float = 0.0,
    ) -> None:
        if name in self._tasks:
            raise ValueError(f"Task already scheduled: {name}")
        task = asyncio.create_task(self._run_once(task_factory, delay_seconds))
        self._tasks[name] = task
        task.add_done_callback(lambda completed: self._remove(name, completed))

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def _run_once(
        self,
        task_factory: Callable[[], Coroutine[Any, Any, None]],
        delay_seconds: float,
    ) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await task_factory()

    def _remove(self, name: str, completed: asyncio.Task[None]) -> None:
        if self._tasks.get(name) is completed:
            self._tasks.pop(name, None)
