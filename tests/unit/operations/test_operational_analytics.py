from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane import operational_api
from lab_platform.control_plane.observability import OperationalMetrics
from lab_platform.control_plane.operational_analytics import build_operational_snapshot
from lab_platform.control_plane.operational_state import (
    ALERT_EVENT_TYPE,
    MAINTENANCE_EVENT_TYPE,
    EventBackedOperationalState,
)
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
    AgentRecord,
    AgentStatus,
    AgentTimelineRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    BenchUtilisation,
    DistributedOperation,
    DistributedOperationStatus,
    EventRecord,
    FailureCategory,
    FlakyBenchPolicy,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OperationalInterval,
    OperationReliabilityObservation,
    QueueEntry,
    QueueEntryStatus,
    QueueOutcome,
    QueueWaitObservation,
    Reservation,
    ReservationStatus,
)

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


ORGANISATION_ID = UUID(int=900)


OTHER_ORGANISATION_ID = UUID(int=901)


AGENT_ID = UUID(int=902)


class _MemoryEvents:
    def __init__(self) -> None:
        self.items: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event

    async def list(
        self,
        *,
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        selected = [
            item
            for item in self.items
            if (bench_id is None or item.bench_id == bench_id)
            and (event_type is None or item.type == event_type)
            and (after is None or item.timestamp > after)
            and (before is None or item.timestamp < before)
        ]
        return sorted(selected, key=lambda item: item.timestamp, reverse=True)[:limit]


def _bench(
    local_id: str,
    *,
    status: GlobalBenchStatus = GlobalBenchStatus.DEGRADED,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"phase9-agent/{local_id}",
        organisation_id=ORGANISATION_ID,
        agent_id=AGENT_ID,
        agent_slug="phase9-agent",
        local_bench_id=local_id,
        name=f"Phase 9 {local_id}",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        target_type="esp32",
        status=status,
        health=(
            HealthStatus.UNHEALTHY if status is GlobalBenchStatus.OFFLINE else HealthStatus.WARNING
        ),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        last_seen_at=NOW,
    )


def _operation(
    bench_id: str,
    index: int,
    *,
    status: DistributedOperationStatus,
    result: dict[str, object] | None = None,
    error_code: str | None = None,
) -> DistributedOperation:
    created_at = NOW - timedelta(minutes=55 - index * 4)
    terminal = status in {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }
    return DistributedOperation(
        remote_command_id=uuid4(),
        agent_id=AGENT_ID,
        bench_id=bench_id,
        operation_type="RUN_WORKFLOW",
        status=status,
        created_at=created_at,
        started_at=created_at + timedelta(minutes=1),
        completed_at=created_at + timedelta(minutes=2) if terminal else None,
        result=result,
        error_code=error_code,
    )


def _timeline(event_type: str, minutes_ago: int) -> AgentTimelineRecord:
    return AgentTimelineRecord(
        agent_id=AGENT_ID,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        event_type=event_type,
        message=event_type,
    )


def test_snapshot_separates_windowed_queue_reliability_and_availability() -> None:
    observed = _bench("observed")
    offline = _bench("offline", status=GlobalBenchStatus.OFFLINE)
    operations = [
        _operation(
            observed.id,
            index,
            status=(
                DistributedOperationStatus.FAILED
                if index < 3
                else DistributedOperationStatus.SUCCEEDED
            ),
            result={
                "firmware_version": f"outer-{index}",
                "workflow_run": {
                    "workflow_name": f"regression-{index % 2}",
                    "firmware_version": f"nested-{index}",
                    "owner": f"operator-{index % 3}",
                },
            },
            error_code="SERIAL_DISCONNECTED" if index < 3 else None,
        )
        for index in range(10)
    ]
    operations.extend(
        [
            _operation(
                observed.id,
                10,
                status=DistributedOperationStatus.RUNNING,
                result={"workflow_run": "invalid-shape", "actor_id": "fallback-owner"},
            ),
            _operation(
                observed.id,
                11,
                status=DistributedOperationStatus.CANCELLED,
            ),
            DistributedOperation(
                remote_command_id=uuid4(),
                agent_id=AGENT_ID,
                bench_id=observed.id,
                operation_type="RUN_WORKFLOW",
                status=DistributedOperationStatus.SUCCEEDED,
                created_at=NOW - timedelta(hours=3),
                completed_at=NOW - timedelta(hours=2),
            ),
            _operation(
                "unknown-agent/other",
                0,
                status=DistributedOperationStatus.FAILED,
                error_code="NETWORK_TIMEOUT",
            ),
        ]
    )
    reservations = [
        Reservation(
            id=uuid4(),
            bench_id=observed.id,
            owner="operator",
            created_at=NOW - timedelta(minutes=58),
            starts_at=NOW - timedelta(minutes=58),
            activated_at=NOW - timedelta(minutes=58),
            released_at=NOW - timedelta(minutes=56),
            status=ReservationStatus.RELEASED,
        ),
        Reservation(
            id=uuid4(),
            bench_id=observed.id,
            owner="operator",
            created_at=NOW - timedelta(minutes=10),
            starts_at=NOW - timedelta(minutes=10),
            ends_at=NOW + timedelta(minutes=20),
            status=ReservationStatus.ACTIVE,
        ),
        Reservation(
            id=uuid4(),
            bench_id=observed.id,
            owner="old-operator",
            created_at=NOW - timedelta(hours=3),
            starts_at=NOW - timedelta(hours=3),
            released_at=NOW - timedelta(hours=2),
            status=ReservationStatus.RELEASED,
        ),
    ]
    queue_entries = [
        QueueEntry(
            bench_id=observed.id,
            owner="old",
            requested_duration_seconds=60,
            status=QueueEntryStatus.PROMOTED,
            created_at=NOW - timedelta(hours=3),
            promoted_at=NOW - timedelta(hours=2),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="promoted",
            requested_duration_seconds=60,
            status=QueueEntryStatus.PROMOTED,
            created_at=NOW - timedelta(minutes=55),
            promoted_at=NOW - timedelta(minutes=40),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="cancelled",
            requested_duration_seconds=60,
            status=QueueEntryStatus.CANCELLED,
            created_at=NOW - timedelta(minutes=35),
            cancelled_at=NOW - timedelta(minutes=30),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="expired",
            requested_duration_seconds=60,
            status=QueueEntryStatus.EXPIRED,
            created_at=NOW - timedelta(minutes=25),
            cancelled_at=NOW - timedelta(minutes=20),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="waiting",
            requested_duration_seconds=60,
            created_at=NOW - timedelta(minutes=15),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="future",
            requested_duration_seconds=60,
            created_at=NOW + timedelta(minutes=1),
        ),
        QueueEntry(
            bench_id=observed.id,
            owner="incomplete-terminal",
            requested_duration_seconds=60,
            status=QueueEntryStatus.PROMOTED,
            created_at=NOW - timedelta(minutes=5),
        ),
    ]

    snapshot = build_operational_snapshot(
        benches=[offline, observed],
        operations=operations,
        reservations=reservations,
        queue_entries=queue_entries,
        agent_timelines={
            AGENT_ID: [
                _timeline("AGENT_DISCONNECTED", 45),
                _timeline("AGENT_CONNECTED", 15),
                _timeline("HEARTBEAT", 10),
                _timeline("AGENT_CONNECTED", 90),
            ]
        },
        maintenance_states={},
        generated_at=NOW,
        window=timedelta(hours=1),
    )

    observed_row = next(item for item in snapshot.benches if item.bench_id == observed.id)
    offline_row = next(item for item in snapshot.benches if item.bench_id == offline.id)
    assert observed_row.availability_ratio == pytest.approx(0.5)
    assert observed_row.reliability.operations == 10
    assert observed_row.reliability.success_rate == pytest.approx(0.7)
    assert observed_row.flaky.potentially_flaky is True
    assert observed_row.recommendation is not None
    assert observed_row.recommendation.code == "INSPECT_DEVICE_CONNECTION"
    assert offline_row.availability_ratio == pytest.approx(0.25)
    assert offline_row.utilisation.utilisation_ratio == 0
    assert offline_row.maintenance.status is BenchMaintenanceStatus.OFFLINE
    assert snapshot.reliability.operations == 10
    assert {item.workflow_name for item in snapshot.workflows} == {
        "regression-0",
        "regression-1",
    }
    assert snapshot.queue.promoted_samples == 1
    assert snapshot.queue.average_wait_seconds == 900
    assert snapshot.queue.abandoned == 2
    assert snapshot.queue.abandonment_rate == pytest.approx(2 / 3)
    assert snapshot.queue.queue_depth == 1

    agents = [
        AgentRecord(
            id=AGENT_ID,
            organisation_id=ORGANISATION_ID,
            slug="offline-agent",
            name="Offline Agent",
            status=AgentStatus.OFFLINE,
            version="0.9.0-beta",
            protocol_version="1.0",
            registered_at=NOW - timedelta(days=1),
        ),
        AgentRecord(
            id=UUID(int=903),
            organisation_id=ORGANISATION_ID,
            slug="incompatible-agent",
            name="Incompatible Agent",
            status=AgentStatus.INCOMPATIBLE,
            version="0.1.0",
            protocol_version="0.1",
            registered_at=NOW - timedelta(days=1),
        ),
    ]
    candidates = operational_api._alert_candidates(
        ORGANISATION_ID,
        snapshot,
        [observed, offline],
        agents,
        generated_at=NOW,
    )
    assert {item.type for item in candidates} == {
        AlertType.AGENT_OFFLINE,
        AlertType.BENCH_DEGRADED,
        AlertType.HIGH_QUEUE_WAIT_TIME,
        AlertType.INCOMPATIBLE_VERSION,
        AlertType.REPEATED_FAILURES,
    }


def test_snapshot_rejects_invalid_window_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_operational_snapshot(
            benches=[],
            operations=[],
            reservations=[],
            queue_entries=[],
            agent_timelines={},
            maintenance_states={},
            generated_at=NOW.replace(tzinfo=None),
            window=timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="window must be positive"):
        build_operational_snapshot(
            benches=[],
            operations=[],
            reservations=[],
            queue_entries=[],
            agent_timelines={},
            maintenance_states={},
            generated_at=NOW,
            window=timedelta(0),
        )


def test_operational_models_reject_inconsistent_durations_and_outcomes() -> None:
    with pytest.raises(ValueError, match="later than"):
        OperationalInterval(started_at=NOW, ended_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        OperationalInterval(
            started_at=NOW.replace(tzinfo=None),
            ended_at=NOW + timedelta(seconds=1),
        )
    invalid_utilisations: list[dict[str, object]] = [
        {
            "observation_seconds": 10,
            "available_seconds": 11,
            "unavailable_seconds": 0,
            "utilised_seconds": 0,
            "utilisation_ratio": 0,
        },
        {
            "observation_seconds": 10,
            "available_seconds": 5,
            "unavailable_seconds": 5,
            "utilised_seconds": 6,
            "utilisation_ratio": 1,
        },
        {
            "observation_seconds": 10,
            "available_seconds": 5,
            "unavailable_seconds": 4,
            "utilised_seconds": 1,
            "utilisation_ratio": 0.2,
        },
        {
            "observation_seconds": 10,
            "available_seconds": 0,
            "unavailable_seconds": 10,
            "utilised_seconds": 0,
            "utilisation_ratio": 0,
        },
        {
            "observation_seconds": 10,
            "available_seconds": 5,
            "unavailable_seconds": 5,
            "utilised_seconds": 0,
            "utilisation_ratio": None,
        },
    ]
    for values in invalid_utilisations:
        with pytest.raises(ValueError):
            BenchUtilisation.model_validate(values)

    with pytest.raises(ValueError, match="waiting"):
        QueueWaitObservation(
            queued_at=NOW,
            outcome=QueueOutcome.WAITING,
            resolved_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="requires"):
        QueueWaitObservation(queued_at=NOW, outcome=QueueOutcome.PROMOTED)
    with pytest.raises(ValueError, match="earlier"):
        QueueWaitObservation(
            queued_at=NOW,
            outcome=QueueOutcome.CANCELLED,
            resolved_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="successful"):
        OperationReliabilityObservation(
            bench_id="phase9-agent/observed",
            succeeded=True,
            completed_at=NOW,
            error_code="SHOULD_NOT_EXIST",
        )
    with pytest.raises(ValueError, match="minimum_operations"):
        FlakyBenchPolicy(window_size=2, minimum_operations=3)
    with pytest.raises(ValueError, match="minimum_infrastructure_failures"):
        FlakyBenchPolicy(
            window_size=2,
            minimum_operations=1,
            minimum_infrastructure_failures=3,
        )


def test_core_metrics_handle_empty_samples_future_queue_and_maintenance_edges() -> None:
    window = OperationalInterval(started_at=NOW, ended_at=NOW + timedelta(minutes=1))
    utilisation = calculate_bench_utilisation(window, available_intervals=[])
    assert utilisation.available_seconds == 0
    assert utilisation.utilisation_ratio is None

    empty_queue = calculate_queue_metrics([], observed_at=NOW)
    assert empty_queue.average_wait_seconds is None
    assert empty_queue.abandonment_rate is None
    with pytest.raises(ValueError, match="begin after"):
        calculate_queue_metrics(
            [
                QueueWaitObservation(
                    queued_at=NOW + timedelta(seconds=1),
                    outcome=QueueOutcome.WAITING,
                )
            ],
            observed_at=NOW,
        )
    with pytest.raises(ValueError, match="resolve after"):
        calculate_queue_metrics(
            [
                QueueWaitObservation(
                    queued_at=NOW,
                    outcome=QueueOutcome.PROMOTED,
                    resolved_at=NOW + timedelta(seconds=2),
                )
            ],
            observed_at=NOW + timedelta(seconds=1),
        )

    explicit_failure = OperationReliabilityObservation(
        bench_id="phase9-agent/observed",
        succeeded=False,
        completed_at=NOW,
        failure_category=FailureCategory.NETWORK_ERROR,
    )
    reliability = calculate_reliability([explicit_failure])
    assert reliability.infrastructure_failures == 1
    low_sample = assess_flaky_bench("phase9-agent/observed", [])
    assert low_sample.potentially_flaky is False
    assert len(low_sample.reasons) == 4

    thermal = maintenance_recommendation("target temperature high")
    power = maintenance_recommendation("power_cycle_failed")
    assert thermal is not None and thermal.code == "INSPECT_COOLING"
    assert power is not None and power.code == "INSPECT_POWER_CONTROL"
    assert maintenance_recommendation(None) is None
    state = BenchMaintenanceState(bench_id="phase9-agent/observed", updated_at=NOW)
    with pytest.raises(ValueError, match="must not be empty"):
        start_bench_maintenance(state, observed_at=NOW, reason="   ")
    with pytest.raises(ValueError, match="not in maintenance"):
        end_bench_maintenance(state, observed_at=NOW)
    maintenance = start_bench_maintenance(state, observed_at=NOW, reason="Inspect")
    with pytest.raises(ValueError, match="must end maintenance"):
        end_bench_maintenance(
            maintenance,
            observed_at=NOW + timedelta(minutes=1),
            resulting_status=BenchMaintenanceStatus.MAINTENANCE,
        )


def test_event_backed_alert_state_is_tenant_scoped_idempotent_and_reconcilable() -> None:
    async def scenario() -> None:
        events = _MemoryEvents()
        state = EventBackedOperationalState(events)
        first = create_alert(
            AlertType.BENCH_DEGRADED,
            resource_type="bench",
            resource_id="phase9-agent/observed",
            message="Bench is degraded",
            created_at=NOW,
            alert_id=UUID(int=1),
        )
        duplicate = first.model_copy(update={"id": UUID(int=2)})

        selected, created = await state.create_or_get_alert(
            first,
            organisation_id=ORGANISATION_ID,
        )
        selected_again, created_again = await state.create_or_get_alert(
            duplicate,
            organisation_id=ORGANISATION_ID,
        )
        selected_other, created_other = await state.create_or_get_alert(
            duplicate,
            organisation_id=OTHER_ORGANISATION_ID,
        )
        assert selected == selected_again == first
        assert selected_other == duplicate
        assert (created, created_again, created_other) == (True, False, True)

        acknowledged = await state.acknowledge_alert(
            first.id,
            organisation_id=ORGANISATION_ID,
            observed_at=NOW + timedelta(minutes=1),
        )
        event_count = len(events.items)
        assert (
            await state.acknowledge_alert(
                first.id,
                organisation_id=ORGANISATION_ID,
                observed_at=NOW + timedelta(minutes=2),
            )
            == acknowledged
        )
        assert len(events.items) == event_count
        resolved = await state.resolve_alert(
            first.id,
            organisation_id=ORGANISATION_ID,
            observed_at=NOW + timedelta(minutes=3),
        )
        assert resolved.status is AlertStatus.RESOLVED
        assert await state.list_alerts(
            organisation_id=ORGANISATION_ID,
            status=AlertStatus.RESOLVED,
        ) == [resolved]
        assert await state.list_alerts(
            organisation_id=OTHER_ORGANISATION_ID,
            status=AlertStatus.OPEN,
        ) == [duplicate]
        with pytest.raises(ValueError, match="resolved alert"):
            await state.acknowledge_alert(
                first.id,
                organisation_id=ORGANISATION_ID,
                observed_at=NOW + timedelta(minutes=4),
            )
        with pytest.raises(KeyError):
            await state.resolve_alert(
                UUID(int=999),
                organisation_id=ORGANISATION_ID,
                observed_at=NOW,
            )

        unmanaged = create_alert(
            AlertType.BACKUP_FAILURE,
            resource_type="deployment",
            resource_id="primary",
            message="Backup failed",
            created_at=NOW + timedelta(minutes=5),
        )
        await state.create_or_get_alert(unmanaged, organisation_id=ORGANISATION_ID)
        candidate = create_alert(
            AlertType.AGENT_OFFLINE,
            resource_type="agent",
            resource_id=str(AGENT_ID),
            message="Agent is offline",
            created_at=NOW + timedelta(minutes=5),
        )
        assert await state.reconcile_alerts(
            [candidate, candidate.model_copy(update={"id": uuid4()})],
            organisation_id=ORGANISATION_ID,
            managed_types=frozenset({AlertType.AGENT_OFFLINE}),
            observed_at=NOW + timedelta(minutes=5),
        ) == (1, 0)
        assert await state.reconcile_alerts(
            [],
            organisation_id=ORGANISATION_ID,
            managed_types=frozenset({AlertType.AGENT_OFFLINE}),
            observed_at=NOW + timedelta(minutes=6),
        ) == (0, 1)
        assert await state.reconcile_alerts(
            [],
            organisation_id=ORGANISATION_ID,
            managed_types=frozenset({AlertType.AGENT_OFFLINE}),
            observed_at=NOW + timedelta(minutes=7),
        ) == (0, 0)
        current = await state.list_alerts(organisation_id=ORGANISATION_ID)
        assert next(item for item in current if item.id == unmanaged.id).status is AlertStatus.OPEN
        assert (
            next(item for item in current if item.id == candidate.id).status is AlertStatus.RESOLVED
        )

        events.items.extend(
            [
                EventRecord(
                    timestamp=NOW + timedelta(minutes=8),
                    type=ALERT_EVENT_TYPE,
                    source="untrusted-source",
                    payload={
                        "organisation_id": str(ORGANISATION_ID),
                        "alert": first.model_dump(mode="json"),
                    },
                ),
                EventRecord(
                    timestamp=NOW + timedelta(minutes=8),
                    type=ALERT_EVENT_TYPE,
                    source="control-plane-alerts",
                    payload={"organisation_id": "not-a-uuid", "alert": "invalid"},
                ),
                EventRecord(
                    timestamp=NOW + timedelta(minutes=8),
                    type=ALERT_EVENT_TYPE,
                    source="control-plane-alerts",
                    payload={
                        "organisation_id": "not-a-uuid",
                        "alert": first.model_dump(mode="json"),
                    },
                ),
            ]
        )
        assert all(
            item.id != first.id or item.status is AlertStatus.RESOLVED
            for item in await state.list_alerts(organisation_id=ORGANISATION_ID)
        )

    asyncio.run(scenario())


def test_event_backed_maintenance_uses_latest_valid_tenant_state() -> None:
    async def scenario() -> None:
        events = _MemoryEvents()
        state = EventBackedOperationalState(events)
        bench_id = "phase9-agent/observed"
        initial = await state.maintenance_state(
            bench_id,
            organisation_id=ORGANISATION_ID,
            default_status=BenchMaintenanceStatus.DEGRADED,
        )
        assert initial.status is BenchMaintenanceStatus.DEGRADED
        maintenance = await state.start_maintenance(
            initial,
            organisation_id=ORGANISATION_ID,
            observed_at=NOW,
            reason="Inspect relay",
        )
        assert (
            await state.maintenance_state(
                bench_id,
                organisation_id=OTHER_ORGANISATION_ID,
                default_status=BenchMaintenanceStatus.OFFLINE,
            )
        ).status is BenchMaintenanceStatus.OFFLINE
        events.items.append(
            EventRecord(
                timestamp=NOW + timedelta(seconds=30),
                type=MAINTENANCE_EVENT_TYPE,
                source="control-plane-maintenance",
                bench_id=bench_id,
                payload={"organisation_id": str(ORGANISATION_ID), "state": "invalid"},
            )
        )
        assert (
            await state.maintenance_state(
                bench_id,
                organisation_id=ORGANISATION_ID,
            )
            == maintenance
        )
        healthy = await state.end_maintenance(
            maintenance,
            organisation_id=ORGANISATION_ID,
            observed_at=NOW + timedelta(minutes=1),
            resulting_status=BenchMaintenanceStatus.HEALTHY,
        )
        assert (
            await state.maintenance_state(
                bench_id,
                organisation_id=ORGANISATION_ID,
            )
            == healthy
        )

    asyncio.run(scenario())


NOW_OPERATIONAL_MATURITY = datetime(2026, 8, 24, 10, tzinfo=UTC)


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
        started_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=start),
        ended_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=end),
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
            queued_at=NOW_OPERATIONAL_MATURITY,
            resolved_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=seconds),
            outcome=QueueOutcome.PROMOTED,
        )
        for seconds in (10, 30, 100)
    ]
    observations.extend(
        [
            QueueWaitObservation(
                queued_at=NOW_OPERATIONAL_MATURITY,
                resolved_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=20),
                outcome=QueueOutcome.CANCELLED,
            ),
            QueueWaitObservation(
                queued_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=5),
                outcome=QueueOutcome.WAITING,
            ),
        ]
    )

    result = calculate_queue_metrics(
        observations, observed_at=NOW_OPERATIONAL_MATURITY + timedelta(seconds=120)
    )

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
        "completed_at": NOW_OPERATIONAL_MATURITY + timedelta(minutes=index),
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

    initial = BenchMaintenanceState(
        bench_id="home-lab/esp32-03", updated_at=NOW_OPERATIONAL_MATURITY
    )
    maintenance = start_bench_maintenance(
        initial,
        observed_at=NOW_OPERATIONAL_MATURITY + timedelta(minutes=1),
        reason="Inspect repeated serial disconnects",
    )
    assert maintenance.status is BenchMaintenanceStatus.MAINTENANCE
    assert maintenance.manually_set is True
    assert maintenance.accepts_new_reservations is False

    recovered = end_bench_maintenance(
        maintenance,
        observed_at=NOW_OPERATIONAL_MATURITY + timedelta(minutes=30),
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
        created_at=NOW_OPERATIONAL_MATURITY,
        alert_id=UUID(int=1),
    )
    duplicate = create_alert(
        AlertType.BACKUP_FAILURE,
        resource_type="deployment",
        resource_id="primary",
        message="The retry also failed.",
        created_at=NOW_OPERATIONAL_MATURITY + timedelta(minutes=1),
        alert_id=UUID(int=2),
    )

    alerts, selected, created = add_or_get_active_alert([], first)
    alerts, selected_again, created_again = add_or_get_active_alert(alerts, duplicate)
    assert first.severity is AlertSeverity.CRITICAL
    assert selected == first
    assert selected_again == first
    assert created is True and created_again is False

    acknowledged = first.acknowledge(NOW_OPERATIONAL_MATURITY + timedelta(minutes=2))
    alerts = replace_alert(alerts, acknowledged)
    resolved = acknowledged.resolve(NOW_OPERATIONAL_MATURITY + timedelta(minutes=3))
    alerts = replace_alert(alerts, resolved)
    assert alerts[0].status is AlertStatus.RESOLVED
    assert alerts[0].acknowledged_at == NOW_OPERATIONAL_MATURITY + timedelta(minutes=2)
    assert alerts[0].resolved_at == NOW_OPERATIONAL_MATURITY + timedelta(minutes=3)

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
