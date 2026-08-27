from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from statistics import fmean, median

from lab_platform.models.operational import (
    INFRASTRUCTURE_FAILURE_CATEGORIES,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    BenchUtilisation,
    FailureCategory,
    FailureClassification,
    FlakyBenchAssessment,
    FlakyBenchPolicy,
    MaintenanceRecommendation,
    OperationalInterval,
    OperationReliabilityObservation,
    QueueMetrics,
    QueueOutcome,
    QueueWaitObservation,
    ReliabilityMetrics,
)

_EXACT_FAILURE_CATEGORIES: dict[str, FailureCategory] = {
    "SERIAL_DISCONNECTED": FailureCategory.DEVICE_DISCONNECTED,
    "DEVICE_DISCONNECTED": FailureCategory.DEVICE_DISCONNECTED,
    "USB_DISCONNECTED": FailureCategory.DEVICE_DISCONNECTED,
    "BENCH_OFFLINE": FailureCategory.DEVICE_DISCONNECTED,
    "AGENT_RESTARTED": FailureCategory.AGENT_ERROR,
    "AGENT_RESTARTED_DURING_OPERATION": FailureCategory.AGENT_ERROR,
    "WORKFLOW_ASSERTION_FAILED": FailureCategory.WORKFLOW_ERROR,
    "FIRMWARE_VERIFICATION_FAILED": FailureCategory.FIRMWARE_ERROR,
    "BOOT_VERIFICATION_FAILED": FailureCategory.FIRMWARE_ERROR,
    "INVALID_FIRMWARE_FILE": FailureCategory.FIRMWARE_ERROR,
    "FIRMWARE_FILE_TOO_LARGE": FailureCategory.USER_ERROR,
}

# Order matters: more specific hardware surfaces precede broad infrastructure prefixes.
_PREFIX_FAILURE_CATEGORIES: tuple[tuple[tuple[str, ...], FailureCategory], ...] = (
    (("SERIAL_",), FailureCategory.SERIAL_ERROR),
    (("ESPTOOL_", "OPENOCD_", "JLINK_", "FLASH_"), FailureCategory.FLASH_ERROR),
    (("PLUGIN_",), FailureCategory.PLUGIN_ERROR),
    (("NETWORK_", "WEBSOCKET_", "SOCKET_", "PROTOCOL_"), FailureCategory.NETWORK_ERROR),
    (("AGENT_", "REMOTE_COMMAND_", "REMOTE_OPERATION_"), FailureCategory.AGENT_ERROR),
    (("WORKFLOW_",), FailureCategory.WORKFLOW_ERROR),
    (("FIRMWARE_",), FailureCategory.FIRMWARE_ERROR),
    (
        (
            "TARGET_",
            "DEVICE_",
            "BOOT_",
            "GPIO_",
            "CAN_",
            "POWER_",
            "MEASUREMENT_",
            "CAPTURE_",
            "WRONG_TARGET_",
        ),
        FailureCategory.TARGET_ERROR,
    ),
    (
        (
            "VALIDATION_",
            "AUTHENTICATION_",
            "PERMISSION_",
            "INVALID_",
            "RESERVATION_OWNER_",
            "RESERVATION_NOT_",
            "CAPABILITY_NOT_SUPPORTED",
        ),
        FailureCategory.USER_ERROR,
    ),
    (
        (
            "BACKEND_",
            "DATABASE_",
            "STORAGE_",
            "ARTIFACT_TRANSFER_",
            "SCHEDULER_",
            "RECOVERY_",
            "INTERNAL_",
            "INFRASTRUCTURE_",
        ),
        FailureCategory.INFRASTRUCTURE_ERROR,
    ),
)


def classify_failure(error_code: str | None) -> FailureClassification:
    """Map a stable operation error code to one documented operational category.

    Unknown or malformed values intentionally become ``UNKNOWN``. Classification must
    never turn an already-failed operation into a new processing failure.
    """

    normalized = _normalize_error_code(error_code)
    category = _EXACT_FAILURE_CATEGORIES.get(normalized or "")
    if category is None and normalized is not None:
        category = next(
            (
                candidate
                for prefixes, candidate in _PREFIX_FAILURE_CATEGORIES
                if normalized.startswith(prefixes)
            ),
            FailureCategory.UNKNOWN,
        )
    category = category or FailureCategory.UNKNOWN
    return FailureClassification(
        error_code=normalized,
        category=category,
        infrastructure_related=category in INFRASTRUCTURE_FAILURE_CATEGORIES,
    )


def calculate_bench_utilisation(
    observation_window: OperationalInterval,
    *,
    available_intervals: Sequence[OperationalInterval],
    reservation_intervals: Sequence[OperationalInterval] = (),
    operation_intervals: Sequence[OperationalInterval] = (),
) -> BenchUtilisation:
    """Calculate reserved-or-operating time over available time.

    Intervals are clipped to the observation window and merged, so overlapping
    reservations and operations are counted once. Work during Agent/bench-offline
    intervals is excluded from both numerator and denominator.
    """

    window = (observation_window.started_at, observation_window.ended_at)
    available = _merge_clipped_intervals(available_intervals, window)
    busy = _merge_clipped_intervals((*reservation_intervals, *operation_intervals), window)
    utilised = _intersect_intervals(available, busy)
    observation_seconds = _seconds(window)
    available_seconds = sum(_seconds(interval) for interval in available)
    utilised_seconds = sum(_seconds(interval) for interval in utilised)
    unavailable_seconds = max(0.0, observation_seconds - available_seconds)
    ratio = (
        min(1.0, max(0.0, utilised_seconds / available_seconds)) if available_seconds > 0 else None
    )
    return BenchUtilisation(
        observation_seconds=observation_seconds,
        available_seconds=available_seconds,
        unavailable_seconds=unavailable_seconds,
        utilised_seconds=utilised_seconds,
        utilisation_ratio=ratio,
    )


def calculate_queue_metrics(
    observations: Sequence[QueueWaitObservation],
    *,
    observed_at: datetime,
) -> QueueMetrics:
    """Summarize queue pressure with explicitly separated service and abandonment.

    Average/median/p95 use only promoted entries (time-to-service). Cancelled and
    expired entries contribute to abandonment, while unresolved entries contribute
    only to current queue depth. P95 uses the deterministic nearest-rank definition.
    """

    now = _as_utc(observed_at)
    promoted_waits: list[float] = []
    abandoned = 0
    queue_depth = 0
    terminal = 0
    for observation in observations:
        if observation.queued_at > now:
            raise ValueError("queue observations cannot begin after observed_at")
        if observation.resolved_at is not None and observation.resolved_at > now:
            raise ValueError("queue observations cannot resolve after observed_at")
        if observation.outcome is QueueOutcome.WAITING:
            queue_depth += 1
            continue
        terminal += 1
        assert observation.resolved_at is not None
        if observation.outcome is QueueOutcome.PROMOTED:
            promoted_waits.append((observation.resolved_at - observation.queued_at).total_seconds())
        else:
            abandoned += 1

    promoted_waits.sort()
    return QueueMetrics(
        promoted_samples=len(promoted_waits),
        average_wait_seconds=fmean(promoted_waits) if promoted_waits else None,
        median_wait_seconds=float(median(promoted_waits)) if promoted_waits else None,
        p95_wait_seconds=_nearest_rank(promoted_waits, 0.95),
        abandoned=abandoned,
        abandonment_rate=(abandoned / terminal if terminal else None),
        queue_depth=queue_depth,
    )


def calculate_reliability(
    observations: Iterable[OperationReliabilityObservation],
) -> ReliabilityMetrics:
    records = tuple(observations)
    failures: Counter[FailureCategory] = Counter()
    succeeded = 0
    infrastructure_failures = 0
    for observation in records:
        if observation.succeeded:
            succeeded += 1
            continue
        category = observation.failure_category or classify_failure(observation.error_code).category
        failures[category] += 1
        if category in INFRASTRUCTURE_FAILURE_CATEGORIES:
            infrastructure_failures += 1
    total = len(records)
    failed = total - succeeded
    return ReliabilityMetrics(
        operations=total,
        succeeded=succeeded,
        failed=failed,
        infrastructure_failures=infrastructure_failures,
        success_rate=(succeeded / total if total else None),
        failure_counts={category: failures[category] for category in sorted(failures, key=str)},
    )


def assess_flaky_bench(
    bench_id: str,
    observations: Iterable[OperationReliabilityObservation],
    *,
    policy: FlakyBenchPolicy | None = None,
) -> FlakyBenchAssessment:
    """Apply a bounded, explainable flaky-bench heuristic.

    The recent sample must exceed the configured operation count, infrastructure
    failure count/rate, and context-diversity thresholds. Context is the tuple of
    workflow, firmware, and actor; requiring more than one helps avoid blaming a
    bench for one consistently broken test or image.
    """

    effective_policy = policy or FlakyBenchPolicy()
    recent = sorted(
        (item for item in observations if item.bench_id == bench_id),
        key=lambda item: item.completed_at,
        reverse=True,
    )[: effective_policy.window_size]
    infrastructure: list[tuple[OperationReliabilityObservation, FailureCategory]] = []
    for observation in recent:
        if observation.succeeded:
            continue
        category = observation.failure_category or classify_failure(observation.error_code).category
        if category in INFRASTRUCTURE_FAILURE_CATEGORIES:
            infrastructure.append((observation, category))

    sample_size = len(recent)
    failure_count = len(infrastructure)
    failure_rate = failure_count / sample_size if sample_size else None
    contexts = {
        (
            observation.workflow_name or "",
            observation.firmware_version or "",
            observation.actor_id or "",
        )
        for observation, _category in infrastructure
    }
    distinct_contexts = len(contexts)
    primary_failure = _primary_failure(infrastructure)
    enough_samples = sample_size >= effective_policy.minimum_operations
    enough_failures = failure_count >= effective_policy.minimum_infrastructure_failures
    high_rate = (
        failure_rate is not None
        and failure_rate >= effective_policy.infrastructure_failure_rate_threshold
    )
    diverse = distinct_contexts >= effective_policy.minimum_distinct_contexts
    potentially_flaky = enough_samples and enough_failures and high_rate and diverse

    reasons: list[str] = []
    if potentially_flaky:
        reasons.append(
            f"{failure_count} of the last {sample_size} operations had infrastructure failures."
        )
        reasons.append(f"Failures span {distinct_contexts} workflow/firmware/actor contexts.")
    else:
        if not enough_samples:
            reasons.append(
                f"Only {sample_size} recent operations are available; "
                f"{effective_policy.minimum_operations} are required."
            )
        if not enough_failures:
            reasons.append(
                f"Only {failure_count} infrastructure failures are present; "
                f"{effective_policy.minimum_infrastructure_failures} are required."
            )
        if not high_rate:
            reasons.append(
                "The recent infrastructure failure rate is below the configured threshold."
            )
        if not diverse:
            reasons.append("Failures do not yet span enough independent contexts.")

    return FlakyBenchAssessment(
        bench_id=bench_id,
        potentially_flaky=potentially_flaky,
        sample_size=sample_size,
        infrastructure_failures=failure_count,
        infrastructure_failure_rate=failure_rate,
        distinct_contexts=distinct_contexts,
        primary_failure=primary_failure,
        reasons=tuple(reasons),
    )


def maintenance_recommendation(
    error_code: str | None,
    *,
    category: FailureCategory | None = None,
) -> MaintenanceRecommendation | None:
    normalized = _normalize_error_code(error_code)
    effective_category = category or classify_failure(normalized).category
    if normalized is not None and (
        "TEMPERATURE" in normalized or normalized in {"OVERHEATED", "THERMAL_LIMIT"}
    ):
        return MaintenanceRecommendation(
            code="INSPECT_COOLING",
            message="Inspect target cooling, airflow, and ambient temperature.",
            failure_category=effective_category,
        )
    if normalized is not None and (normalized.startswith("POWER_") or "POWER_CYCLE" in normalized):
        return MaintenanceRecommendation(
            code="INSPECT_POWER_CONTROL",
            message="Inspect the relay or programmable power-supply connection.",
            failure_category=effective_category,
        )
    recommendations = {
        FailureCategory.DEVICE_DISCONNECTED: (
            "INSPECT_DEVICE_CONNECTION",
            "Inspect USB, serial, and target cabling for disconnects.",
        ),
        FailureCategory.SERIAL_ERROR: (
            "INSPECT_SERIAL_CONNECTION",
            "Inspect the USB serial adapter, cable, permissions, and port selection.",
        ),
        FailureCategory.FLASH_ERROR: (
            "INSPECT_DEBUG_CONNECTION",
            "Inspect the debugger, target connection, and target power before retrying flash.",
        ),
        FailureCategory.TARGET_ERROR: (
            "INSPECT_TARGET",
            "Inspect target power, wiring, reset state, and debugger connectivity.",
        ),
        FailureCategory.NETWORK_ERROR: (
            "INSPECT_AGENT_NETWORK",
            "Inspect Agent network connectivity and control-plane reachability.",
        ),
        FailureCategory.PLUGIN_ERROR: (
            "RUN_PLUGIN_DIAGNOSTICS",
            "Run plugin diagnostics and inspect its external dependencies.",
        ),
    }
    recommendation = recommendations.get(effective_category)
    if recommendation is None:
        return None
    code, message = recommendation
    return MaintenanceRecommendation(
        code=code,
        message=message,
        failure_category=effective_category,
    )


def start_bench_maintenance(
    state: BenchMaintenanceState,
    *,
    observed_at: datetime,
    reason: str,
) -> BenchMaintenanceState:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("maintenance reason must not be empty")
    return BenchMaintenanceState(
        bench_id=state.bench_id,
        status=BenchMaintenanceStatus.MAINTENANCE,
        updated_at=observed_at,
        reason=normalized_reason,
        manually_set=True,
    )


def end_bench_maintenance(
    state: BenchMaintenanceState,
    *,
    observed_at: datetime,
    resulting_status: BenchMaintenanceStatus = BenchMaintenanceStatus.HEALTHY,
) -> BenchMaintenanceState:
    if state.status is not BenchMaintenanceStatus.MAINTENANCE:
        raise ValueError("bench is not in maintenance")
    if resulting_status is BenchMaintenanceStatus.MAINTENANCE:
        raise ValueError("resulting status must end maintenance")
    return BenchMaintenanceState(
        bench_id=state.bench_id,
        status=resulting_status,
        updated_at=observed_at,
        manually_set=False,
    )


def _normalize_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9_]+", "_", value.strip().upper()).strip("_")
    return normalized[:200] or None


def _merge_clipped_intervals(
    intervals: Sequence[OperationalInterval],
    window: tuple[datetime, datetime],
) -> list[tuple[datetime, datetime]]:
    clipped = sorted(
        (
            (max(item.started_at, window[0]), min(item.ended_at, window[1]))
            for item in intervals
            if item.ended_at > window[0] and item.started_at < window[1]
        ),
        key=lambda item: item[0],
    )
    merged: list[tuple[datetime, datetime]] = []
    for started_at, ended_at in clipped:
        if started_at >= ended_at:
            continue
        if not merged or started_at > merged[-1][1]:
            merged.append((started_at, ended_at))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, ended_at))
    return merged


def _intersect_intervals(
    left: Sequence[tuple[datetime, datetime]],
    right: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    intersections: list[tuple[datetime, datetime]] = []
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        started_at = max(left[left_index][0], right[right_index][0])
        ended_at = min(left[left_index][1], right[right_index][1])
        if started_at < ended_at:
            intersections.append((started_at, ended_at))
        if left[left_index][1] <= right[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return intersections


def _seconds(interval: tuple[datetime, datetime]) -> float:
    return max(0.0, (interval[1] - interval[0]).total_seconds())


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    if not 0 < percentile <= 1 or not math.isfinite(percentile):
        raise ValueError("percentile must be finite and in (0, 1]")
    rank = max(1, math.ceil(percentile * len(values)))
    return values[rank - 1]


def _primary_failure(
    failures: Sequence[tuple[OperationReliabilityObservation, FailureCategory]],
) -> str | None:
    counts = Counter(
        _normalize_error_code(observation.error_code) or category.value
        for observation, category in failures
    )
    if not counts:
        return None
    return min(counts, key=lambda value: (-counts[value], value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "assess_flaky_bench",
    "calculate_bench_utilisation",
    "calculate_queue_metrics",
    "calculate_reliability",
    "classify_failure",
    "end_bench_maintenance",
    "maintenance_recommendation",
    "start_bench_maintenance",
]
