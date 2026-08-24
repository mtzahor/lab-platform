from __future__ import annotations

import pytest
from lab_platform.control_plane.rate_limits import InMemoryRateLimiter, RateLimitPolicy


def test_sliding_window_rate_limiter_reports_retry_and_recovers() -> None:
    now = [10.0]
    limiter = InMemoryRateLimiter(maximum_keys=2, clock=lambda: now[0])
    policy = RateLimitPolicy(requests=2, window_seconds=10)

    assert limiter.check("workflow-create", "client-a", policy).remaining == 1
    assert limiter.check("workflow-create", "client-a", policy).remaining == 0
    rejected = limiter.check("workflow-create", "client-a", policy)
    assert not rejected.allowed
    assert rejected.retry_after_seconds == 10

    now[0] = 20.0
    recovered = limiter.check("workflow-create", "client-a", policy)
    assert recovered.allowed
    assert recovered.remaining == 1


def test_limiter_bounds_attacker_controlled_client_keys() -> None:
    limiter = InMemoryRateLimiter(maximum_keys=2, clock=lambda: 1.0)
    policy = RateLimitPolicy(requests=1, window_seconds=10)
    for key in ("one", "two", "three"):
        assert limiter.check("login", key, policy).allowed

    assert list(limiter._entries) == [("login", "two"), ("login", "three")]


@pytest.mark.parametrize(
    "policy",
    [
        {"requests": 0, "window_seconds": 1},
        {"requests": 1, "window_seconds": 0},
    ],
)
def test_invalid_rate_limit_policy_is_rejected(policy: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RateLimitPolicy(**policy)


def test_limiter_requires_positive_key_capacity() -> None:
    with pytest.raises(ValueError, match="maximum rate-limit keys"):
        InMemoryRateLimiter(maximum_keys=0)


@pytest.mark.parametrize(
    ("category", "client_key", "message"),
    [
        ("", "client", "category"),
        (" " * 2, "client", "category"),
        ("x" * 101, "client", "category"),
        ("login", "", "client key"),
        ("login", " " * 2, "client key"),
        ("login", "x" * 501, "client key"),
    ],
)
def test_limiter_rejects_unbounded_or_empty_keys(
    category: str,
    client_key: str,
    message: str,
) -> None:
    limiter = InMemoryRateLimiter()
    with pytest.raises(ValueError, match=message):
        limiter.check(category, client_key, RateLimitPolicy(requests=1, window_seconds=1))


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_limiter_rejects_non_finite_clock(clock_value: float) -> None:
    limiter = InMemoryRateLimiter(clock=lambda: clock_value)
    with pytest.raises(ValueError, match="clock must be finite"):
        limiter.check("login", "client", RateLimitPolicy(requests=1, window_seconds=1))
