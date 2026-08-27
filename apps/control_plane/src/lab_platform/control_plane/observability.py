from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from lab_platform.models.operational import (
    AlertSeverity,
    FailureCategory,
    QueueOutcome,
)

_DURATION_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0, 3_600.0)
_WORKFLOW_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
_RESERVATION_OUTCOMES = frozenset({"released", "expired", "cancelled"})
_AVAILABILITY_KINDS = frozenset({"agent", "bench", "resource"})


@dataclass(frozen=True, slots=True)
class HttpMetricKey:
    method: str
    route: str
    status_code: int


@dataclass(frozen=True, slots=True)
class HistogramMetricKey:
    name: str
    labels: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class _Histogram:
    buckets: list[int]
    count: int = 0
    total: float = 0.0

    @classmethod
    def empty(cls) -> _Histogram:
        return cls([0] * len(_DURATION_BUCKETS))

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for index, boundary in enumerate(_DURATION_BUCKETS):
            if value <= boundary:
                self.buckets[index] += 1

    def snapshot(self) -> tuple[tuple[int, ...], int, float]:
        return tuple(self.buckets), self.count, self.total


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
        self._histograms: dict[HistogramMetricKey, _Histogram] = {}
        self._workflow_outcomes: defaultdict[str, int] = defaultdict(int)
        self._operation_failures: defaultdict[FailureCategory, int] = defaultdict(int)
        self._operational_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

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

    def observe_queue_wait(self, duration_seconds: float, *, outcome: QueueOutcome) -> None:
        if outcome is QueueOutcome.WAITING:
            raise ValueError("queue wait metrics require a terminal outcome")
        self._observe_duration(
            "queue_wait_seconds",
            duration_seconds,
            outcome=outcome.value.casefold(),
        )

    def observe_reservation_duration(self, duration_seconds: float, *, outcome: str) -> None:
        normalized = _bounded_choice(outcome, "reservation outcome", _RESERVATION_OUTCOMES)
        self._observe_duration(
            "reservation_duration_seconds",
            duration_seconds,
            outcome=normalized,
        )

    def observe_workflow_duration(self, duration_seconds: float, *, outcome: str) -> None:
        normalized = _bounded_choice(outcome, "workflow outcome", _WORKFLOW_OUTCOMES)
        self._observe_duration(
            "workflow_duration_seconds",
            duration_seconds,
            outcome=normalized,
        )
        with self._lock:
            self._workflow_outcomes[normalized] += 1

    def record_operation_failure(self, category: FailureCategory) -> None:
        with self._lock:
            self._operation_failures[category] += 1

    def set_bench_utilisation(self, ratio: float) -> None:
        self._set_operational_gauge("bench_utilisation_ratio", ratio, scope="aggregate")

    def set_availability(self, kind: str, ratio: float) -> None:
        normalized = _bounded_choice(kind, "availability kind", _AVAILABILITY_KINDS)
        self._set_operational_gauge(
            "availability_ratio",
            ratio,
            resource_type=normalized,
        )

    def set_open_alerts(self, severity: AlertSeverity, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("open alert count must be a non-negative integer")
        self._set_operational_gauge(
            "alerts_open",
            float(count),
            severity=severity.value,
        )

    def _observe_duration(self, name: str, duration_seconds: float, **labels: str) -> None:
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("metric duration must be finite and non-negative")
        key = HistogramMetricKey(name=name, labels=tuple(sorted(labels.items())))
        with self._lock:
            histogram = self._histograms.setdefault(key, _Histogram.empty())
            histogram.observe(duration_seconds)

    def _set_operational_gauge(self, name: str, value: float, **labels: str) -> None:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or (name.endswith("_ratio") and numeric > 1):
            raise ValueError("operational gauge must be finite and inside its valid range")
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._operational_gauges[key] = numeric

    def render_prometheus(self, gauges: Mapping[str, int | float]) -> str:
        # Keep every line numeric for compatibility with the platform's original
        # lightweight scrapers. HELP/TYPE comments are optional in Prometheus text.
        lines: list[str] = []
        with self._lock:
            counts = dict(self._request_counts)
            durations = dict(self._duration_seconds)
            failures = dict(self._background_failures)
            histograms = {key: value.snapshot() for key, value in self._histograms.items()}
            workflow_outcomes = dict(self._workflow_outcomes)
            operation_failures = dict(self._operation_failures)
            operational_gauges = dict(self._operational_gauges)
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
        for histogram_key, (bucket_counts, count, total) in sorted(
            histograms.items(), key=lambda item: (item[0].name, item[0].labels)
        ):
            base_name = f"lab_platform_{histogram_key.name}"
            common_labels = dict(histogram_key.labels)
            for boundary, bucket_count in zip(_DURATION_BUCKETS, bucket_counts, strict=True):
                lines.append(
                    f"{base_name}_bucket"
                    f"{_labels(**common_labels, le=_bucket_label(boundary))} {bucket_count}"
                )
            lines.append(f"{base_name}_bucket{_labels(**common_labels, le='+Inf')} {count}")
            lines.append(f"{base_name}_sum{_labels(**common_labels)} {total:.9f}")
            lines.append(f"{base_name}_count{_labels(**common_labels)} {count}")
        for outcome, value in sorted(workflow_outcomes.items()):
            lines.append(f"lab_platform_workflows_total{_labels(outcome=outcome)} {value}")
        for category, value in sorted(operation_failures.items(), key=lambda item: item[0].value):
            lines.append(
                f"lab_platform_operation_failures_total{_labels(category=category.value)} {value}"
            )
        for (name, label_items), gauge_value in sorted(operational_gauges.items()):
            rendered = str(int(gauge_value)) if gauge_value.is_integer() else repr(gauge_value)
            lines.append(f"lab_platform_{name}{_labels(**dict(label_items))} {rendered}")
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


def _bounded_choice(value: str, field: str, choices: frozenset[str]) -> str:
    normalized = value.strip().casefold()
    if normalized not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _bucket_label(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


__all__ = ["HistogramMetricKey", "HttpMetricKey", "OperationalMetrics"]
