from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HttpMetricKey:
    method: str
    route: str
    status_code: int


class OperationalMetrics:
    """Small dependency-free Prometheus accumulator for the control-plane edge.

    Labels are limited to method, route template, and response status. Callers must
    pass a route template rather than the raw URL so bench, Agent, workflow, and
    operation identifiers cannot create unbounded metric cardinality.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_counts: defaultdict[HttpMetricKey, int] = defaultdict(int)
        self._duration_seconds: defaultdict[HttpMetricKey, float] = defaultdict(float)
        self._background_failures: defaultdict[str, int] = defaultdict(int)

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        if not route.startswith("/"):
            raise ValueError("metric route must be a root-relative route template")
        if status_code < 100 or status_code > 599:
            raise ValueError("metric HTTP status code is invalid")
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("metric duration must be finite and non-negative")
        key = HttpMetricKey(method.upper(), route, status_code)
        with self._lock:
            self._request_counts[key] += 1
            self._duration_seconds[key] += duration_seconds

    def record_background_failure(self, worker: str) -> None:
        normalized = worker.strip()
        if not normalized or len(normalized) > 100:
            raise ValueError("worker metric label must contain 1 to 100 characters")
        with self._lock:
            self._background_failures[normalized] += 1

    def render_prometheus(self, gauges: Mapping[str, int | float]) -> str:
        # Keep every line numeric for compatibility with the platform's original
        # lightweight scrapers. HELP/TYPE comments are optional in Prometheus text.
        lines: list[str] = []
        with self._lock:
            counts = dict(self._request_counts)
            durations = dict(self._duration_seconds)
            failures = dict(self._background_failures)
        for key in sorted(counts, key=lambda item: (item.route, item.method, item.status_code)):
            labels = _labels(
                method=key.method,
                route=key.route,
                status=str(key.status_code),
            )
            lines.append(f"lab_platform_http_requests_total{labels} {counts[key]}")
        for key in sorted(durations, key=lambda item: (item.route, item.method, item.status_code)):
            labels = _labels(
                method=key.method,
                route=key.route,
                status=str(key.status_code),
            )
            lines.append(
                f"lab_platform_http_request_duration_seconds_sum{labels} {durations[key]:.9f}"
            )
            lines.append(f"lab_platform_http_request_duration_seconds_count{labels} {counts[key]}")
        for worker, value in sorted(failures.items()):
            lines.append(
                f"lab_platform_background_worker_failures_total{_labels(worker=worker)} {value}"
            )
        for name, gauge_value in sorted(gauges.items()):
            if not _valid_metric_label(name):
                continue
            numeric = float(gauge_value)
            if not math.isfinite(numeric):
                continue
            rendered = str(gauge_value) if isinstance(gauge_value, int) else repr(numeric)
            lines.append(f"lab_platform_runtime{_labels(name=name)} {rendered}")
            # Retain the pre-Phase-8 unlabelled metric surface for existing scrapers.
            lines.append(f"{name} {rendered}")
        return "\n".join(lines) + "\n"


def _labels(**values: str) -> str:
    rendered = ",".join(
        f'{name}="{_escape_label(value)}"' for name, value in sorted(values.items())
    )
    return "{" + rendered + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _valid_metric_label(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in "_:" for character in value)


__all__ = ["HttpMetricKey", "OperationalMetrics"]
