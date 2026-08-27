from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from lab_platform.core import (
    add_or_get_active_alert,
    end_bench_maintenance,
    replace_alert,
    start_bench_maintenance,
)
from lab_platform.models import (
    Alert,
    AlertStatus,
    BenchMaintenanceState,
    BenchMaintenanceStatus,
    EventRecord,
)

ALERT_EVENT_TYPE = "OPERATIONAL_ALERT_STATE_CHANGED"
MAINTENANCE_EVENT_TYPE = "BENCH_MAINTENANCE_STATE_CHANGED"
OPERATIONAL_ALERT_SOURCE = "control-plane-alerts"
OPERATIONAL_MAINTENANCE_SOURCE = "control-plane-maintenance"


class OperationalEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...

    async def list(
        self,
        *,
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[EventRecord]: ...


class EventBackedOperationalState:
    """Durable alert and maintenance state stored in the existing event journal.

    Phase 9 deliberately keeps these small lifecycle records in the append-only
    event journal. That makes state restart-safe and auditable without adding a
    schema migration immediately before the 1.0 compatibility freeze.
    """

    def __init__(self, events: OperationalEventRepository) -> None:
        self._events = events
        self._lock = asyncio.Lock()

    async def list_alerts(
        self,
        *,
        organisation_id: UUID | None = None,
        status: AlertStatus | None = None,
        limit: int = 10_000,
    ) -> list[Alert]:
        events = await self._events.list(event_type=ALERT_EVENT_TYPE, limit=limit)
        latest: dict[UUID, tuple[datetime, Alert, UUID | None]] = {}
        for event in events:
            if event.source != OPERATIONAL_ALERT_SOURCE:
                continue
            alert = _alert_from_event(event)
            if alert is None:
                continue
            event_organisation = _event_organisation_id(event)
            current = latest.get(alert.id)
            if current is None or current[0] < event.timestamp:
                latest[alert.id] = (event.timestamp, alert, event_organisation)
        alerts = [
            alert
            for _timestamp, alert, event_organisation in latest.values()
            if (organisation_id is None or event_organisation == organisation_id)
            and (status is None or alert.status is status)
        ]
        return sorted(alerts, key=lambda item: (item.created_at, str(item.id)), reverse=True)

    async def create_or_get_alert(
        self,
        alert: Alert,
        *,
        organisation_id: UUID,
    ) -> tuple[Alert, bool]:
        async with self._lock:
            current = await self.list_alerts(organisation_id=organisation_id)
            _alerts, selected, created = add_or_get_active_alert(current, alert)
            if created:
                await self._record_alert(selected, organisation_id=organisation_id)
            return selected, created

    async def acknowledge_alert(
        self,
        alert_id: UUID,
        *,
        organisation_id: UUID,
        observed_at: datetime,
    ) -> Alert:
        return await self._transition_alert(
            alert_id,
            organisation_id=organisation_id,
            transition=lambda alert: alert.acknowledge(observed_at),
        )

    async def resolve_alert(
        self,
        alert_id: UUID,
        *,
        organisation_id: UUID,
        observed_at: datetime,
    ) -> Alert:
        return await self._transition_alert(
            alert_id,
            organisation_id=organisation_id,
            transition=lambda alert: alert.resolve(observed_at),
        )

    async def reconcile_alerts(
        self,
        candidates: Iterable[Alert],
        *,
        organisation_id: UUID,
        managed_types: frozenset[object],
        observed_at: datetime,
    ) -> tuple[int, int]:
        """Create new current alerts and resolve managed conditions that cleared."""

        async with self._lock:
            current = await self.list_alerts(organisation_id=organisation_id)
            active = [item for item in current if item.status is not AlertStatus.RESOLVED]
            candidate_items = tuple(candidates)
            identities = {
                (item.type, item.resource_type, item.resource_id) for item in candidate_items
            }
            created = 0
            resolved = 0
            for candidate in candidate_items:
                _alerts, _selected, was_created = add_or_get_active_alert(active, candidate)
                if was_created:
                    await self._record_alert(candidate, organisation_id=organisation_id)
                    active.append(candidate)
                    created += 1
            for alert in tuple(active):
                identity = (alert.type, alert.resource_type, alert.resource_id)
                if alert.type not in managed_types or identity in identities:
                    continue
                updated = alert.resolve(observed_at)
                active = list(replace_alert(active, updated))
                await self._record_alert(updated, organisation_id=organisation_id)
                resolved += 1
            return created, resolved

    async def maintenance_state(
        self,
        bench_id: str,
        *,
        organisation_id: UUID,
        default_status: BenchMaintenanceStatus = BenchMaintenanceStatus.HEALTHY,
    ) -> BenchMaintenanceState:
        events = await self._events.list(
            bench_id=bench_id,
            event_type=MAINTENANCE_EVENT_TYPE,
            limit=100,
        )
        for event in events:
            if (
                event.source == OPERATIONAL_MAINTENANCE_SOURCE
                and _event_organisation_id(event) == organisation_id
            ):
                state = _maintenance_from_event(event)
                if state is not None:
                    return state
        return BenchMaintenanceState(bench_id=bench_id, status=default_status)

    async def start_maintenance(
        self,
        state: BenchMaintenanceState,
        *,
        organisation_id: UUID,
        observed_at: datetime,
        reason: str,
    ) -> BenchMaintenanceState:
        updated = start_bench_maintenance(state, observed_at=observed_at, reason=reason)
        await self._record_maintenance(updated, organisation_id=organisation_id)
        return updated

    async def end_maintenance(
        self,
        state: BenchMaintenanceState,
        *,
        organisation_id: UUID,
        observed_at: datetime,
        resulting_status: BenchMaintenanceStatus,
    ) -> BenchMaintenanceState:
        updated = end_bench_maintenance(
            state,
            observed_at=observed_at,
            resulting_status=resulting_status,
        )
        await self._record_maintenance(updated, organisation_id=organisation_id)
        return updated

    async def _transition_alert(
        self,
        alert_id: UUID,
        *,
        organisation_id: UUID,
        transition: object,
    ) -> Alert:
        async with self._lock:
            current = await self.list_alerts(organisation_id=organisation_id)
            selected = next((item for item in current if item.id == alert_id), None)
            if selected is None:
                raise KeyError(str(alert_id))
            if not callable(transition):  # pragma: no cover - local invariant
                raise TypeError("alert transition must be callable")
            updated = transition(selected)
            if not isinstance(updated, Alert):  # pragma: no cover - local invariant
                raise TypeError("alert transition must return Alert")
            if updated != selected:
                await self._record_alert(updated, organisation_id=organisation_id)
            return updated

    async def _record_alert(self, alert: Alert, *, organisation_id: UUID) -> None:
        await self._events.create(
            EventRecord(
                timestamp=_alert_observed_at(alert),
                type=ALERT_EVENT_TYPE,
                source=OPERATIONAL_ALERT_SOURCE,
                payload={
                    "organisation_id": str(organisation_id),
                    "alert": alert.model_dump(mode="json"),
                },
                deduplication_key=f"operational-alert:{alert.id}:{alert.status.value}",
            )
        )

    async def _record_maintenance(
        self,
        state: BenchMaintenanceState,
        *,
        organisation_id: UUID,
    ) -> None:
        await self._events.create(
            EventRecord(
                timestamp=state.updated_at,
                type=MAINTENANCE_EVENT_TYPE,
                source=OPERATIONAL_MAINTENANCE_SOURCE,
                bench_id=state.bench_id,
                payload={
                    "organisation_id": str(organisation_id),
                    "state": state.model_dump(mode="json"),
                },
                deduplication_key=(
                    f"bench-maintenance:{organisation_id}:{state.bench_id}:"
                    f"{state.updated_at.isoformat()}:{state.status.value}"
                ),
            )
        )


def _alert_observed_at(alert: Alert) -> datetime:
    return alert.resolved_at or alert.acknowledged_at or alert.created_at


def _alert_from_event(event: EventRecord) -> Alert | None:
    try:
        return Alert.model_validate(event.payload["alert"])
    except (KeyError, TypeError, ValueError):
        return None


def _maintenance_from_event(event: EventRecord) -> BenchMaintenanceState | None:
    try:
        return BenchMaintenanceState.model_validate(event.payload["state"])
    except (KeyError, TypeError, ValueError):
        return None


def _event_organisation_id(event: EventRecord) -> UUID | None:
    try:
        return UUID(str(event.payload["organisation_id"]))
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "ALERT_EVENT_TYPE",
    "MAINTENANCE_EVENT_TYPE",
    "EventBackedOperationalState",
]
