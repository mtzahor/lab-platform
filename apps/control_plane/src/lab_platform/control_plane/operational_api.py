from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from lab_platform.control_plane.operational_analytics import (
    OperationalAnalyticsSnapshot,
    build_operational_snapshot,
)
from lab_platform.control_plane_core.errors import AgentNotFoundError
from lab_platform.core import create_alert
from lab_platform.models import (
    BENCH_MAINTENANCE_LABEL,
    BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL,
    AgentRecord,
    AgentStatus,
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
    ApiToken,
    AuthenticationContext,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    GlobalBenchRecord,
    GlobalBenchStatus,
    QueueEntry,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from lab_platform.control_plane.runtime import ControlPlaneRuntime

AuthenticatedActor = ApiToken | AuthenticationContext
AuthenticationDependency = Callable[..., object]

_MANAGED_ALERT_TYPES = frozenset(
    {
        AlertType.AGENT_OFFLINE,
        AlertType.BENCH_DEGRADED,
        AlertType.REPEATED_FAILURES,
        AlertType.HIGH_QUEUE_WAIT_TIME,
        AlertType.INCOMPATIBLE_VERSION,
    }
)
_HIGH_QUEUE_WAIT_SECONDS = 15 * 60


class OperationalApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertsPage(OperationalApiModel):
    items: list[Alert]
    total: int = Field(ge=0)


class MaintenanceStartRequest(OperationalApiModel):
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("maintenance reason must not be empty")
        return normalized


def create_operational_router(
    runtime: ControlPlaneRuntime,
    *,
    read_auth: AuthenticationDependency,
    alert_auth: AuthenticationDependency,
    maintenance_auth: AuthenticationDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/operational", tags=["operational"])
    read_dependency = Depends(read_auth)
    alert_dependency = Depends(alert_auth)
    maintenance_dependency = Depends(maintenance_auth)

    @router.get("/analytics", response_model=OperationalAnalyticsSnapshot)
    async def analytics(
        actor: AuthenticatedActor = read_dependency,
        window_hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24 * 7,
    ) -> OperationalAnalyticsSnapshot:
        return await load_operational_snapshot(
            runtime,
            actor=actor,
            generated_at=datetime.now(UTC),
            window=timedelta(hours=window_hours),
        )

    @router.get("/alerts", response_model=AlertsPage)
    async def list_alerts(
        actor: AuthenticatedActor = read_dependency,
        alert_status: Annotated[AlertStatus | None, Query(alias="status")] = None,
    ) -> AlertsPage:
        alerts = await runtime.operational_state.list_alerts(
            organisation_id=_organisation_id(actor),
            status=alert_status,
        )
        return AlertsPage(items=alerts, total=len(alerts))

    @router.post("/alerts/{alert_id:uuid}/acknowledge", response_model=Alert)
    async def acknowledge_alert(
        alert_id: UUID,
        actor: AuthenticatedActor = alert_dependency,
    ) -> Alert:
        organisation_id = await _alert_organisation(runtime, actor, alert_id)
        try:
            return await runtime.operational_state.acknowledge_alert(
                alert_id,
                organisation_id=organisation_id,
                observed_at=datetime.now(UTC),
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alert not found",
            ) from None

    @router.post("/alerts/{alert_id:uuid}/resolve", response_model=Alert)
    async def resolve_alert(
        alert_id: UUID,
        actor: AuthenticatedActor = alert_dependency,
    ) -> Alert:
        organisation_id = await _alert_organisation(runtime, actor, alert_id)
        try:
            return await runtime.operational_state.resolve_alert(
                alert_id,
                organisation_id=organisation_id,
                observed_at=datetime.now(UTC),
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alert not found",
            ) from None

    @router.post(
        "/benches/{bench_id:path}/maintenance/start",
        response_model=BenchMaintenanceState,
    )
    async def start_maintenance(
        bench_id: str,
        body: MaintenanceStartRequest,
        actor: AuthenticatedActor = maintenance_dependency,
    ) -> BenchMaintenanceState:
        bench = await _visible_bench(runtime, actor, bench_id)
        now = datetime.now(UTC)
        bench_update_at = max(now, bench.updated_at + timedelta(microseconds=1))
        current = await _maintenance_state(runtime, bench, observed_at=now)
        labels = dict(bench.labels)
        labels[BENCH_MAINTENANCE_LABEL] = "true"
        labels.setdefault(BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL, bench.status.value)
        await runtime.benches.upsert(
            bench.model_copy(
                update={
                    "labels": labels,
                    "status": (
                        GlobalBenchStatus.OFFLINE
                        if bench.status is GlobalBenchStatus.OFFLINE
                        else GlobalBenchStatus.DEGRADED
                    ),
                    "updated_at": bench_update_at,
                }
            )
        )
        return await runtime.operational_state.start_maintenance(
            current,
            organisation_id=bench.organisation_id,
            observed_at=now,
            reason=body.reason,
        )

    @router.post(
        "/benches/{bench_id:path}/maintenance/end",
        response_model=BenchMaintenanceState,
    )
    async def end_maintenance(
        bench_id: str,
        actor: AuthenticatedActor = maintenance_dependency,
    ) -> BenchMaintenanceState:
        bench = await _visible_bench(runtime, actor, bench_id)
        now = datetime.now(UTC)
        bench_update_at = max(now, bench.updated_at + timedelta(microseconds=1))
        current = await _maintenance_state(runtime, bench, observed_at=now)
        if current.status is not BenchMaintenanceStatus.MAINTENANCE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bench is not in maintenance",
            )
        previous = bench.labels.get(BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL)
        restored = (
            GlobalBenchStatus.OFFLINE
            if bench.status is GlobalBenchStatus.OFFLINE
            else _safe_bench_status(previous)
        )
        resulting_state = (
            BenchMaintenanceStatus.OFFLINE
            if restored is GlobalBenchStatus.OFFLINE
            else BenchMaintenanceStatus.DEGRADED
            if restored is GlobalBenchStatus.DEGRADED
            else BenchMaintenanceStatus.HEALTHY
        )
        updated = await runtime.operational_state.end_maintenance(
            current,
            organisation_id=bench.organisation_id,
            observed_at=now,
            resulting_status=resulting_state,
        )
        labels = dict(bench.labels)
        labels[BENCH_MAINTENANCE_LABEL] = "false"
        labels.pop(BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL, None)
        await runtime.benches.upsert(
            bench.model_copy(
                update={
                    "labels": labels,
                    "status": restored,
                    "updated_at": bench_update_at,
                }
            )
        )
        return updated

    return router


async def load_operational_snapshot(
    runtime: ControlPlaneRuntime,
    *,
    actor: AuthenticatedActor | None,
    generated_at: datetime,
    window: timedelta,
    organisation_id: UUID | None = None,
) -> OperationalAnalyticsSnapshot:
    identity = actor if isinstance(actor, AuthenticationContext) else None
    legacy = isinstance(actor, ApiToken)
    internal = actor is None
    scope = _organisation_id(actor) if actor is not None else organisation_id
    benches, operations = await asyncio.gather(
        runtime.operational_access.list_benches(
            authentication_context=identity,
            allow_legacy_authorisation=legacy,
            allow_internal_authorisation=internal,
            organisation_id=scope,
        ),
        runtime.operational_access.list_operations(
            limit=10_000,
            authentication_context=identity,
            allow_legacy_authorisation=legacy,
            allow_internal_authorisation=internal,
            organisation_id=scope,
        ),
    )
    coordinated, scheduled = await asyncio.gather(
        runtime.reservation_repository.list(organisation_id=scope, limit=10_000),
        runtime.scheduled_reservation_repository.list(
            organisation_id=scope,
            limit=10_000,
        ),
    )
    reservations_by_id = {item.reservation.id: item.reservation for item in coordinated}
    reservations_by_id.update({item.id: item for item in scheduled})
    queue_groups = await asyncio.gather(
        *(
            runtime.reservation_queue.list(
                bench_id=bench.id,
                organisation_id=bench.organisation_id,
                status=None,
            )
            for bench in benches
        )
    )
    queue_entries: list[QueueEntry] = [item for group in queue_groups for item in group]
    timeline_groups = await asyncio.gather(
        *(
            runtime.timeline.list(
                agent_id,
                since=generated_at - window,
                limit=10_000,
            )
            for agent_id in sorted({bench.agent_id for bench in benches}, key=str)
        )
    )
    timeline_by_agent = dict(
        zip(
            sorted({bench.agent_id for bench in benches}, key=str),
            timeline_groups,
            strict=True,
        )
    )
    states = await asyncio.gather(
        *(_maintenance_state(runtime, bench, observed_at=generated_at) for bench in benches)
    )
    return build_operational_snapshot(
        benches=benches,
        operations=operations,
        reservations=list(reservations_by_id.values()),
        queue_entries=queue_entries,
        agent_timelines=timeline_by_agent,
        maintenance_states={state.bench_id: state for state in states},
        generated_at=generated_at,
        window=window,
    )


async def reconcile_operational_alerts(
    runtime: ControlPlaneRuntime,
    *,
    generated_at: datetime,
    window: timedelta = timedelta(days=7),
) -> None:
    benches = await runtime.operational_access.list_benches(
        allow_internal_authorisation=True,
    )
    agents = await runtime.operational_access.list_agents(
        allow_internal_authorisation=True,
    )
    benches_by_organisation: dict[UUID, list[GlobalBenchRecord]] = {}
    agents_by_organisation: dict[UUID, list[AgentRecord]] = {}
    for bench in benches:
        benches_by_organisation.setdefault(bench.organisation_id, []).append(bench)
    for agent in agents:
        agents_by_organisation.setdefault(agent.organisation_id, []).append(agent)
    snapshots: list[OperationalAnalyticsSnapshot] = []
    for organisation_id in sorted(
        set(benches_by_organisation) | set(agents_by_organisation), key=str
    ):
        snapshot = await load_operational_snapshot(
            runtime,
            actor=None,
            generated_at=generated_at,
            window=window,
            organisation_id=organisation_id,
        )
        snapshots.append(snapshot)
        candidates = _alert_candidates(
            organisation_id,
            snapshot,
            benches_by_organisation.get(organisation_id, []),
            agents_by_organisation.get(organisation_id, []),
            generated_at=generated_at,
        )
        await runtime.operational_state.reconcile_alerts(
            candidates,
            organisation_id=organisation_id,
            managed_types=_MANAGED_ALERT_TYPES,
            observed_at=generated_at,
        )
    _update_prometheus_snapshot(runtime, snapshots, agents)
    active_alerts = await runtime.operational_state.list_alerts()
    for severity in AlertSeverity:
        runtime.observability.set_open_alerts(
            severity,
            sum(
                alert.severity is severity and alert.status is not AlertStatus.RESOLVED
                for alert in active_alerts
            ),
        )


def _alert_candidates(
    organisation_id: UUID,
    snapshot: OperationalAnalyticsSnapshot,
    benches: list[GlobalBenchRecord],
    agents: list[AgentRecord],
    *,
    generated_at: datetime,
) -> list[Alert]:
    candidates: list[Alert] = []
    for agent in agents:
        if agent.status is AgentStatus.OFFLINE:
            candidates.append(
                create_alert(
                    AlertType.AGENT_OFFLINE,
                    resource_type="agent",
                    resource_id=str(agent.id),
                    message=f"Agent {agent.slug} is offline.",
                    created_at=generated_at,
                )
            )
        if agent.status is AgentStatus.INCOMPATIBLE:
            candidates.append(
                create_alert(
                    AlertType.INCOMPATIBLE_VERSION,
                    resource_type="agent",
                    resource_id=str(agent.id),
                    message=f"Agent {agent.slug} is running an incompatible version.",
                    created_at=generated_at,
                )
            )
    by_id = {item.bench_id: item for item in snapshot.benches}
    for bench in benches:
        analytics = by_id.get(bench.id)
        if bench.status is GlobalBenchStatus.DEGRADED and (
            analytics is None
            or analytics.maintenance.status is not BenchMaintenanceStatus.MAINTENANCE
        ):
            candidates.append(
                create_alert(
                    AlertType.BENCH_DEGRADED,
                    resource_type="bench",
                    resource_id=bench.id,
                    message=f"Bench {bench.id} is degraded.",
                    created_at=generated_at,
                )
            )
        if analytics is not None and analytics.flaky.potentially_flaky:
            candidates.append(
                create_alert(
                    AlertType.REPEATED_FAILURES,
                    resource_type="bench",
                    resource_id=bench.id,
                    message=(
                        f"Bench {bench.id} has {analytics.flaky.infrastructure_failures} "
                        f"infrastructure failures in its last {analytics.flaky.sample_size} "
                        "operations."
                    ),
                    created_at=generated_at,
                )
            )
    if (
        snapshot.queue.p95_wait_seconds is not None
        and snapshot.queue.p95_wait_seconds >= _HIGH_QUEUE_WAIT_SECONDS
    ):
        candidates.append(
            create_alert(
                AlertType.HIGH_QUEUE_WAIT_TIME,
                resource_type="organisation",
                resource_id=str(organisation_id),
                message=(f"Queue p95 wait time is {snapshot.queue.p95_wait_seconds:.0f} seconds."),
                created_at=generated_at,
            )
        )
    return candidates


def _update_prometheus_snapshot(
    runtime: ControlPlaneRuntime,
    snapshots: list[OperationalAnalyticsSnapshot],
    agents: list[AgentRecord],
) -> None:
    utilisation = [
        item.utilisation.utilisation_ratio
        for snapshot in snapshots
        for item in snapshot.benches
        if item.utilisation.utilisation_ratio is not None
    ]
    availability = [
        item.availability_ratio
        for snapshot in snapshots
        for item in snapshot.benches
        if item.availability_ratio is not None
    ]
    runtime.observability.set_bench_utilisation(
        sum(utilisation) / len(utilisation) if utilisation else 0
    )
    runtime.observability.set_availability(
        "bench",
        sum(availability) / len(availability) if availability else 0,
    )
    runtime.observability.set_availability(
        "resource",
        sum(availability) / len(availability) if availability else 0,
    )
    runtime.observability.set_availability(
        "agent",
        (
            sum(agent.status is AgentStatus.ONLINE for agent in agents) / len(agents)
            if agents
            else 0
        ),
    )


async def _visible_bench(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor,
    bench_id: str,
) -> GlobalBenchRecord:
    return await runtime.operational_access.get_bench(
        bench_id,
        authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
        allow_legacy_authorisation=isinstance(actor, ApiToken),
    )


async def _maintenance_state(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
    *,
    observed_at: datetime,
) -> BenchMaintenanceState:
    default_status = (
        BenchMaintenanceStatus.OFFLINE
        if bench.status is GlobalBenchStatus.OFFLINE
        else BenchMaintenanceStatus.DEGRADED
        if bench.status is GlobalBenchStatus.DEGRADED
        else BenchMaintenanceStatus.HEALTHY
    )
    state = await runtime.operational_state.maintenance_state(
        bench.id,
        organisation_id=bench.organisation_id,
        default_status=default_status,
    )
    if bench.labels.get(BENCH_MAINTENANCE_LABEL) == "true" and (
        state.status is not BenchMaintenanceStatus.MAINTENANCE
    ):
        return BenchMaintenanceState(
            bench_id=bench.id,
            status=BenchMaintenanceStatus.MAINTENANCE,
            updated_at=observed_at,
            reason="Maintenance safety flag is active.",
            manually_set=True,
        )
    return state


async def _alert_organisation(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor,
    alert_id: UUID,
) -> UUID:
    organisation_id = _organisation_id(actor)
    if organisation_id is not None:
        return organisation_id
    alerts = await runtime.operational_state.list_alerts()
    alert = next((item for item in alerts if item.id == alert_id), None)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    if alert.resource_type == "bench":
        bench = await runtime.benches.get(alert.resource_id)
        if bench is not None:
            return bench.organisation_id
    if alert.resource_type == "agent":
        try:
            agent = await runtime.presence.get_agent(UUID(alert.resource_id))
            return agent.organisation_id
        except (AgentNotFoundError, TypeError, ValueError):
            pass
    try:
        return UUID(alert.resource_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alert not found",
        ) from None


def _organisation_id(actor: AuthenticatedActor | None) -> UUID | None:
    return actor.principal.organisation_id if isinstance(actor, AuthenticationContext) else None


def _safe_bench_status(value: str | None) -> GlobalBenchStatus:
    if value is None:
        return GlobalBenchStatus.ONLINE
    try:
        return GlobalBenchStatus(value)
    except ValueError:
        return GlobalBenchStatus.ONLINE


__all__ = [
    "AlertsPage",
    "MaintenanceStartRequest",
    "create_operational_router",
    "load_operational_snapshot",
    "reconcile_operational_alerts",
]
