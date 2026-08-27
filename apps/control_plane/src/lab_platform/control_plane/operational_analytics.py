from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from lab_platform.core import (
    assess_flaky_bench,
    calculate_bench_utilisation,
    calculate_queue_metrics,
    calculate_reliability,
    maintenance_recommendation,
)
from lab_platform.models import (
    AgentTimelineRecord,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    BenchUtilisation,
    DistributedOperation,
    DistributedOperationStatus,
    FlakyBenchAssessment,
    GlobalBenchRecord,
    GlobalBenchStatus,
    MaintenanceRecommendation,
    OperationalInterval,
    OperationReliabilityObservation,
    QueueEntry,
    QueueEntryStatus,
    QueueMetrics,
    QueueOutcome,
    QueueWaitObservation,
    ReliabilityMetrics,
    Reservation,
    ReservationStatus,
)
from pydantic import BaseModel, ConfigDict, Field

_TERMINAL_OPERATION_STATUSES = frozenset(
    {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
    }
)
_ACTIVE_RESERVATION_STATUSES = frozenset(
    {
        ReservationStatus.ACTIVE,
    }
)


class OperationalAnalyticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchOperationalAnalytics(OperationalAnalyticsModel):
    bench_id: str
    name: str
    target_type: str | None = None
    availability_ratio: float | None = Field(default=None, ge=0, le=1)
    utilisation: BenchUtilisation
    reservation_utilisation_ratio: float | None = Field(default=None, ge=0, le=1)
    reliability: ReliabilityMetrics
    flaky: FlakyBenchAssessment
    maintenance: BenchMaintenanceState
    recommendation: MaintenanceRecommendation | None = None


class WorkflowOperationalAnalytics(OperationalAnalyticsModel):
    workflow_name: str
    benches_observed: int = Field(ge=0)
    reliability: ReliabilityMetrics


class OperationalAnalyticsSnapshot(OperationalAnalyticsModel):
    generated_at: datetime
    window: OperationalInterval
    semantics: dict[str, str]
    queue: QueueMetrics
    reliability: ReliabilityMetrics
    benches: list[BenchOperationalAnalytics]
    workflows: list[WorkflowOperationalAnalytics]


def build_operational_snapshot(
    *,
    benches: Sequence[GlobalBenchRecord],
    operations: Sequence[DistributedOperation],
    reservations: Sequence[Reservation],
    queue_entries: Sequence[QueueEntry],
    agent_timelines: Mapping[UUID, Sequence[AgentTimelineRecord]],
    maintenance_states: Mapping[str, BenchMaintenanceState],
    generated_at: datetime,
    window: timedelta,
) -> OperationalAnalyticsSnapshot:
    """Build an explainable operational view from durable control-plane records."""

    observed_at = _as_utc(generated_at)
    if window <= timedelta(0):
        raise ValueError("analytics window must be positive")
    observed_window = OperationalInterval(
        started_at=observed_at - window,
        ended_at=observed_at,
    )
    visible_bench_ids = {bench.id for bench in benches}
    selected_operations = [
        operation
        for operation in operations
        if operation.bench_id in visible_bench_ids
        and operation.created_at < observed_window.ended_at
        and (operation.completed_at or observed_at) > observed_window.started_at
    ]
    selected_reservations = [
        reservation
        for reservation in reservations
        if reservation.bench_id in visible_bench_ids
        and (reservation.starts_at or reservation.created_at) < observed_window.ended_at
        and _reservation_end(reservation, observed_at) > observed_window.started_at
    ]
    reliability_observations = _reliability_observations(selected_operations)
    workflow_groups: defaultdict[str, list[OperationReliabilityObservation]] = defaultdict(list)
    workflow_benches: defaultdict[str, set[str]] = defaultdict(set)
    for operation, observation in reliability_observations:
        if operation.operation_type != "RUN_WORKFLOW":
            continue
        name = observation.workflow_name or "unknown-workflow"
        workflow_groups[name].append(observation)
        workflow_benches[name].add(observation.bench_id)

    rows: list[BenchOperationalAnalytics] = []
    for bench in sorted(benches, key=lambda item: item.id):
        state = maintenance_states.get(
            bench.id,
            BenchMaintenanceState(
                bench_id=bench.id,
                status=_default_maintenance_status(bench),
                updated_at=observed_at,
            ),
        )
        available = _availability_intervals(
            observed_window,
            currently_available=(
                bench.status is not GlobalBenchStatus.OFFLINE and state.accepts_new_reservations
            ),
            timeline=agent_timelines.get(bench.agent_id, ()),
        )
        bench_reservations = [
            _reservation_interval(item, observed_at)
            for item in selected_reservations
            if item.bench_id == bench.id
        ]
        bench_operations = [
            _operation_interval(item, observed_at)
            for item in selected_operations
            if item.bench_id == bench.id
        ]
        utilisation = calculate_bench_utilisation(
            observed_window,
            available_intervals=available,
            reservation_intervals=bench_reservations,
            operation_intervals=bench_operations,
        )
        reservation_utilisation = calculate_bench_utilisation(
            observed_window,
            available_intervals=available,
            reservation_intervals=bench_reservations,
        )
        observations = [
            observation
            for _operation, observation in reliability_observations
            if observation.bench_id == bench.id
        ]
        reliability = calculate_reliability(observations)
        flaky = assess_flaky_bench(bench.id, observations)
        recommendation = (
            maintenance_recommendation(flaky.primary_failure)
            if flaky.primary_failure is not None
            else None
        )
        rows.append(
            BenchOperationalAnalytics(
                bench_id=bench.id,
                name=bench.name,
                target_type=bench.target_type,
                availability_ratio=(
                    utilisation.available_seconds / utilisation.observation_seconds
                    if utilisation.observation_seconds > 0
                    else None
                ),
                utilisation=utilisation,
                reservation_utilisation_ratio=reservation_utilisation.utilisation_ratio,
                reliability=reliability,
                flaky=flaky,
                maintenance=state,
                recommendation=recommendation,
            )
        )

    return OperationalAnalyticsSnapshot(
        generated_at=observed_at,
        window=observed_window,
        semantics={
            "bench_utilisation": (
                "reserved or actively operating time divided by available observation time"
            ),
            "availability": (
                "Agent-connected, non-maintenance time divided by the observation window"
            ),
            "queue_wait": "time from queue creation until promotion; abandonment is separate",
            "reliability": "successful operations divided by succeeded plus failed operations",
        },
        queue=calculate_queue_metrics(
            _queue_observations(
                queue_entries,
                observed_at=observed_at,
                window_started_at=observed_window.started_at,
            ),
            observed_at=observed_at,
        ),
        reliability=calculate_reliability(
            observation for _operation, observation in reliability_observations
        ),
        benches=rows,
        workflows=[
            WorkflowOperationalAnalytics(
                workflow_name=name,
                benches_observed=len(workflow_benches[name]),
                reliability=calculate_reliability(items),
            )
            for name, items in sorted(workflow_groups.items())
        ],
    )


def _reliability_observations(
    operations: Sequence[DistributedOperation],
) -> list[tuple[DistributedOperation, OperationReliabilityObservation]]:
    observations: list[tuple[DistributedOperation, OperationReliabilityObservation]] = []
    for operation in operations:
        if operation.status not in _TERMINAL_OPERATION_STATUSES or operation.completed_at is None:
            continue
        workflow_name, firmware_version, actor_id = _operation_context(operation)
        observation_values: dict[str, object] = {
            "bench_id": operation.bench_id,
            "succeeded": operation.status is DistributedOperationStatus.SUCCEEDED,
            "completed_at": operation.completed_at,
            "workflow_name": workflow_name,
            "firmware_version": firmware_version,
            "actor_id": actor_id,
        }
        if operation.status is DistributedOperationStatus.FAILED:
            observation_values["error_code"] = operation.error_code
        observations.append(
            (operation, OperationReliabilityObservation.model_validate(observation_values))
        )
    return observations


def _operation_context(
    operation: DistributedOperation,
) -> tuple[str | None, str | None, str | None]:
    result = operation.result or {}
    workflow = result.get("workflow_run")
    workflow_values = workflow if isinstance(workflow, dict) else {}
    workflow_name = workflow_values.get("workflow_name")
    firmware_version = result.get("firmware_version") or workflow_values.get("firmware_version")
    actor_id = workflow_values.get("owner") or result.get("actor_id")
    return (
        str(workflow_name) if workflow_name is not None else None,
        str(firmware_version) if firmware_version is not None else None,
        str(actor_id) if actor_id is not None else None,
    )


def _queue_observations(
    entries: Sequence[QueueEntry],
    *,
    observed_at: datetime,
    window_started_at: datetime,
) -> list[QueueWaitObservation]:
    outcomes = {
        QueueEntryStatus.WAITING: QueueOutcome.WAITING,
        QueueEntryStatus.PROMOTED: QueueOutcome.PROMOTED,
        QueueEntryStatus.CANCELLED: QueueOutcome.CANCELLED,
        QueueEntryStatus.EXPIRED: QueueOutcome.EXPIRED,
    }
    observations: list[QueueWaitObservation] = []
    for entry in entries:
        if entry.created_at > observed_at:
            continue
        resolved_at = entry.promoted_at or entry.cancelled_at
        if resolved_at is not None and resolved_at > observed_at:
            continue
        if entry.status is not QueueEntryStatus.WAITING and resolved_at is None:
            continue
        if resolved_at is not None and resolved_at <= window_started_at:
            continue
        observations.append(
            QueueWaitObservation(
                queued_at=entry.created_at,
                outcome=outcomes[entry.status],
                resolved_at=resolved_at,
            )
        )
    return observations


def _availability_intervals(
    window: OperationalInterval,
    *,
    currently_available: bool,
    timeline: Sequence[AgentTimelineRecord],
) -> list[OperationalInterval]:
    events = sorted(
        (
            event
            for event in timeline
            if event.event_type in {"AGENT_CONNECTED", "AGENT_DISCONNECTED"}
            and window.started_at < event.timestamp < window.ended_at
        ),
        key=lambda item: item.timestamp,
        reverse=True,
    )
    intervals: list[OperationalInterval] = []
    cursor = window.ended_at
    available = currently_available
    for event in events:
        if available and event.timestamp < cursor:
            intervals.append(OperationalInterval(started_at=event.timestamp, ended_at=cursor))
        available = event.event_type == "AGENT_DISCONNECTED"
        cursor = event.timestamp
    if available and cursor > window.started_at:
        intervals.append(OperationalInterval(started_at=window.started_at, ended_at=cursor))
    return intervals


def _reservation_interval(reservation: Reservation, observed_at: datetime) -> OperationalInterval:
    started_at = reservation.activated_at or reservation.starts_at or reservation.created_at
    return OperationalInterval(
        started_at=started_at,
        ended_at=_reservation_end(reservation, observed_at),
    )


def _reservation_end(reservation: Reservation, observed_at: datetime) -> datetime:
    terminal = reservation.released_at or reservation.expired_at
    if terminal is not None:
        return terminal
    if reservation.status in _ACTIVE_RESERVATION_STATUSES:
        return min(reservation.ends_at or observed_at, observed_at)
    return reservation.ends_at or reservation.created_at + timedelta(microseconds=1)


def _operation_interval(
    operation: DistributedOperation,
    observed_at: datetime,
) -> OperationalInterval:
    started_at = operation.started_at or operation.dispatched_at or operation.created_at
    ended_at = operation.completed_at or observed_at
    if ended_at <= started_at:
        ended_at = started_at + timedelta(microseconds=1)
    return OperationalInterval(started_at=started_at, ended_at=ended_at)


def _default_maintenance_status(bench: GlobalBenchRecord) -> BenchMaintenanceStatus:
    if bench.status is GlobalBenchStatus.OFFLINE:
        return BenchMaintenanceStatus.OFFLINE
    if bench.status is GlobalBenchStatus.DEGRADED:
        return BenchMaintenanceStatus.DEGRADED
    return BenchMaintenanceStatus.HEALTHY


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "BenchOperationalAnalytics",
    "OperationalAnalyticsSnapshot",
    "WorkflowOperationalAnalytics",
    "build_operational_snapshot",
]
