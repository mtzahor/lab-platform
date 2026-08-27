from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.control_plane.observability import OperationalMetrics
from lab_platform.core import (
    add_or_get_active_alert,
    assess_flaky_bench,
    calculate_bench_utilisation,
    calculate_queue_metrics,
    calculate_reliability,
    classify_failure,
    create_alert,
    end_bench_maintenance,
    maintenance_recommendation,
    replace_alert,
    start_bench_maintenance,
)
from lab_platform.models import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    FailureCategory,
    OperationalInterval,
    OperationReliabilityObservation,
    QueueOutcome,
    QueueWaitObservation,
)

NOW = datetime(2026, 8, 24, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("error_code", "category", "infrastructure_related"),
    [
        ("serial disconnected", FailureCategory.DEVICE_DISCONNECTED, True),
        ("SERIAL_PORT_BUSY", FailureCategory.SERIAL_ERROR, True),
        ("ESPTOOL_FLASH_FAILED", FailureCategory.FLASH_ERROR, True),
        ("WORKFLOW_ASSERTION_FAILED", FailureCategory.WORKFLOW_ERROR, False),
        ("FIRMWARE_VERIFICATION_FAILED", FailureCategory.FIRMWARE_ERROR, False),
        ("PLUGIN_TIMEOUT", FailureCategory.PLUGIN_ERROR, True),
        ("BACKEND_UNAVAILABLE", FailureCategory.INFRASTRUCTURE_ERROR, True),
        ("unmapped-vendor-code", FailureCategory.UNKNOWN, False),
        (None, FailureCategory.UNKNOWN, False),
    ],
)
def test_failure_classification_is_stable_and_safe(
    error_code: str | None,
    category: FailureCategory,
    infrastructure_related: bool,
) -> None:
    result = classify_failure(error_code)
    assert result.category is category
    assert result.infrastructure_related is infrastructure_related


def _interval(start: int, end: int) -> OperationalInterval:
    return OperationalInterval(
        started_at=NOW + timedelta(seconds=start),
        ended_at=NOW + timedelta(seconds=end),
    )


def test_utilisation_merges_overlap_and_excludes_offline_time() -> None:
    result = calculate_bench_utilisation(
        _interval(0, 1_000),
        available_intervals=[_interval(0, 400), _interval(600, 1_000)],
        reservation_intervals=[_interval(100, 300), _interval(700, 900)],
        operation_intervals=[_interval(250, 350), _interval(450, 550)],
    )

    assert result.observation_seconds == 1_000
    assert result.available_seconds == 800
    assert result.unavailable_seconds == 200
    assert result.utilised_seconds == 450
    assert result.utilisation_ratio == pytest.approx(0.5625)


def test_queue_metrics_separate_waits_abandonment_and_depth() -> None:
    observations = [
        QueueWaitObservation(
            queued_at=NOW,
            resolved_at=NOW + timedelta(seconds=seconds),
            outcome=QueueOutcome.PROMOTED,
        )
        for seconds in (10, 30, 100)
    ]
    observations.extend(
        [
            QueueWaitObservation(
                queued_at=NOW,
                resolved_at=NOW + timedelta(seconds=20),
                outcome=QueueOutcome.CANCELLED,
            ),
            QueueWaitObservation(
                queued_at=NOW + timedelta(seconds=5),
                outcome=QueueOutcome.WAITING,
            ),
        ]
    )

    result = calculate_queue_metrics(observations, observed_at=NOW + timedelta(seconds=120))

    assert result.promoted_samples == 3
    assert result.average_wait_seconds == pytest.approx(140 / 3)
    assert result.median_wait_seconds == 30
    assert result.p95_wait_seconds == 100
    assert result.abandoned == 1
    assert result.abandonment_rate == 0.25
    assert result.queue_depth == 1


def _reliability_observation(index: int, *, failed: bool) -> OperationReliabilityObservation:
    values: dict[str, object] = {
        "bench_id": "home-lab/esp32-03",
        "succeeded": not failed,
        "completed_at": NOW + timedelta(minutes=index),
        "workflow_name": f"regression-{index % 2}",
        "firmware_version": f"1.{index % 2}.0",
        "actor_id": f"user-{index % 3}",
    }
    if failed:
        values["error_code"] = "SERIAL_DISCONNECTED"
    return OperationReliabilityObservation.model_validate(values)


def test_reliability_and_flaky_bench_heuristic_are_explainable() -> None:
    observations = [_reliability_observation(index, failed=index < 6) for index in range(20)]

    reliability = calculate_reliability(observations)
    flaky = assess_flaky_bench("home-lab/esp32-03", observations)

    assert reliability.operations == 20
    assert reliability.succeeded == 14
    assert reliability.failed == 6
    assert reliability.infrastructure_failures == 6
    assert reliability.success_rate == 0.7
    assert reliability.failure_counts == {FailureCategory.DEVICE_DISCONNECTED: 6}
    assert flaky.potentially_flaky is True
    assert flaky.infrastructure_failure_rate == 0.3
    assert flaky.primary_failure == "SERIAL_DISCONNECTED"
    assert flaky.distinct_contexts > 1
    assert "6 of the last 20" in flaky.reasons[0]


def test_maintenance_recommendations_and_manual_state_transitions() -> None:
    recommendation = maintenance_recommendation("SERIAL_DISCONNECTED")
    assert recommendation is not None
    assert recommendation.heuristic is True
    assert recommendation.code == "INSPECT_DEVICE_CONNECTION"
    assert "cabling" in recommendation.message

    initial = BenchMaintenanceState(bench_id="home-lab/esp32-03", updated_at=NOW)
    maintenance = start_bench_maintenance(
        initial,
        observed_at=NOW + timedelta(minutes=1),
        reason="Inspect repeated serial disconnects",
    )
    assert maintenance.status is BenchMaintenanceStatus.MAINTENANCE
    assert maintenance.manually_set is True
    assert maintenance.accepts_new_reservations is False

    recovered = end_bench_maintenance(
        maintenance,
        observed_at=NOW + timedelta(minutes=30),
        resulting_status=BenchMaintenanceStatus.DEGRADED,
    )
    assert recovered.status is BenchMaintenanceStatus.DEGRADED
    assert recovered.manually_set is False
    assert recovered.accepts_new_reservations is True


def test_alerts_have_deduplicated_identity_and_valid_lifecycle() -> None:
    first = create_alert(
        AlertType.BACKUP_FAILURE,
        resource_type="deployment",
        resource_id="primary",
        message="The scheduled backup failed.",
        created_at=NOW,
        alert_id=UUID(int=1),
    )
    duplicate = create_alert(
        AlertType.BACKUP_FAILURE,
        resource_type="deployment",
        resource_id="primary",
        message="The retry also failed.",
        created_at=NOW + timedelta(minutes=1),
        alert_id=UUID(int=2),
    )

    alerts, selected, created = add_or_get_active_alert([], first)
    alerts, selected_again, created_again = add_or_get_active_alert(alerts, duplicate)
    assert first.severity is AlertSeverity.CRITICAL
    assert selected == first
    assert selected_again == first
    assert created is True and created_again is False

    acknowledged = first.acknowledge(NOW + timedelta(minutes=2))
    alerts = replace_alert(alerts, acknowledged)
    resolved = acknowledged.resolve(NOW + timedelta(minutes=3))
    alerts = replace_alert(alerts, resolved)
    assert alerts[0].status is AlertStatus.RESOLVED
    assert alerts[0].acknowledged_at == NOW + timedelta(minutes=2)
    assert alerts[0].resolved_at == NOW + timedelta(minutes=3)

    alerts, selected_after_resolution, created_after_resolution = add_or_get_active_alert(
        alerts, duplicate
    )
    assert created_after_resolution is True
    assert selected_after_resolution == duplicate
    assert len(alerts) == 2


def test_prometheus_extensions_use_bounded_dimensions_and_histograms() -> None:
    metrics = OperationalMetrics()
    metrics.observe_queue_wait(2, outcome=QueueOutcome.PROMOTED)
    metrics.observe_queue_wait(20, outcome=QueueOutcome.CANCELLED)
    metrics.observe_reservation_duration(120, outcome="released")
    metrics.observe_workflow_duration(9, outcome="succeeded")
    metrics.record_operation_failure(FailureCategory.SERIAL_ERROR)
    metrics.set_bench_utilisation(0.75)
    metrics.set_availability("agent", 0.99)
    metrics.set_open_alerts(AlertSeverity.WARNING, 2)

    rendered = metrics.render_prometheus({})

    assert 'lab_platform_queue_wait_seconds_bucket{le="5",outcome="promoted"} 1' in rendered
    assert 'lab_platform_queue_wait_seconds_count{outcome="cancelled"} 1' in rendered
    assert 'lab_platform_reservation_duration_seconds_count{outcome="released"} 1' in rendered
    assert 'lab_platform_workflow_duration_seconds_sum{outcome="succeeded"} 9.000000000' in rendered
    assert 'lab_platform_workflows_total{outcome="succeeded"} 1' in rendered
    assert 'lab_platform_operation_failures_total{category="SERIAL_ERROR"} 1' in rendered
    assert 'lab_platform_bench_utilisation_ratio{scope="aggregate"} 0.75' in rendered
    assert 'lab_platform_availability_ratio{resource_type="agent"} 0.99' in rendered
    assert 'lab_platform_alerts_open{severity="WARNING"} 2' in rendered

    with pytest.raises(ValueError):
        metrics.observe_queue_wait(1, outcome=QueueOutcome.WAITING)
    with pytest.raises(ValueError):
        metrics.set_bench_utilisation(1.1)
