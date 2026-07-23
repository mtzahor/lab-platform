from __future__ import annotations

from typing import Protocol

from lab_platform.models import QueueEntry, QueueEntryStatus


class QueuePolicy(Protocol):
    async def select_next(self, entries: list[QueueEntry]) -> QueueEntry | None: ...


class FifoQueuePolicy:
    async def select_next(self, entries: list[QueueEntry]) -> QueueEntry | None:
        waiting = (entry for entry in entries if entry.status is QueueEntryStatus.WAITING)
        return min(
            waiting,
            key=lambda entry: (
                entry.position if entry.position is not None else float("inf"),
                entry.created_at,
                str(entry.id),
            ),
            default=None,
        )
