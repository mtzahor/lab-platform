from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    requests: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.requests <= 0:
            raise ValueError("rate-limit request count must be positive")
        if self.window_seconds <= 0:
            raise ValueError("rate-limit window must be positive")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class InMemoryRateLimiter:
    """Bounded sliding-window limiter for one control-plane process.

    It protects expensive public edges; it is not presented as a distributed
    quota system. An external proxy remains the right fleet-wide abuse boundary.
    """

    def __init__(
        self,
        *,
        maximum_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_keys <= 0:
            raise ValueError("maximum rate-limit keys must be positive")
        self._maximum_keys = maximum_keys
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(
        self,
        category: str,
        client_key: str,
        policy: RateLimitPolicy,
    ) -> RateLimitDecision:
        if not category.strip() or len(category) > 100:
            raise ValueError("rate-limit category must contain 1 to 100 characters")
        if not client_key.strip() or len(client_key) > 500:
            raise ValueError("rate-limit client key must contain 1 to 500 characters")
        now = self._clock()
        if not math.isfinite(now):
            raise ValueError("rate-limit clock must be finite")
        threshold = now - policy.window_seconds
        key = (category, client_key)
        with self._lock:
            history = self._entries.pop(key, deque())
            while history and history[0] <= threshold:
                history.popleft()
            if len(history) >= policy.requests:
                retry_after = max(1, math.ceil(history[0] + policy.window_seconds - now))
                self._entries[key] = history
                return RateLimitDecision(False, policy.requests, 0, retry_after)
            history.append(now)
            self._entries[key] = history
            while len(self._entries) > self._maximum_keys:
                self._entries.popitem(last=False)
            return RateLimitDecision(
                True,
                policy.requests,
                policy.requests - len(history),
                0,
            )


__all__ = ["InMemoryRateLimiter", "RateLimitDecision", "RateLimitPolicy"]
