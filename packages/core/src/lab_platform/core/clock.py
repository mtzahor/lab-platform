from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Controllable UTC clock for scheduler and recovery tests."""

    def __init__(self, current: datetime | str) -> None:
        self._current = self._parse(current)

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime | str) -> None:
        self._current = self._parse(value)

    def advance(self, delta: timedelta | None = None, **parts: float) -> datetime:
        if delta is not None and parts:
            raise ValueError("Pass either delta or keyword duration parts, not both")
        self._current += delta if delta is not None else timedelta(**parts)
        return self._current

    @staticmethod
    def _parse(value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return as_utc(value)
