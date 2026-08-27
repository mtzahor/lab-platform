from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from lab_platform.models.operational import (
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
)

_DEFAULT_SEVERITIES: dict[AlertType, AlertSeverity] = {
    AlertType.AGENT_OFFLINE: AlertSeverity.WARNING,
    AlertType.BENCH_DEGRADED: AlertSeverity.WARNING,
    AlertType.REPEATED_FAILURES: AlertSeverity.WARNING,
    AlertType.STORAGE_USAGE: AlertSeverity.WARNING,
    AlertType.BACKUP_FAILURE: AlertSeverity.CRITICAL,
    AlertType.DATABASE_ISSUE: AlertSeverity.CRITICAL,
    AlertType.HIGH_QUEUE_WAIT_TIME: AlertSeverity.WARNING,
    AlertType.PLUGIN_UNHEALTHY: AlertSeverity.WARNING,
    AlertType.INCOMPATIBLE_VERSION: AlertSeverity.WARNING,
}


def create_alert(
    alert_type: AlertType,
    *,
    resource_type: str,
    resource_id: str,
    message: str,
    severity: AlertSeverity | None = None,
    created_at: datetime | None = None,
    alert_id: UUID | None = None,
) -> Alert:
    """Create one alert using the platform's conservative default severity map."""

    values: dict[str, object] = {
        "type": alert_type,
        "severity": severity or _DEFAULT_SEVERITIES[alert_type],
        "resource_type": resource_type,
        "resource_id": resource_id,
        "message": message,
    }
    if created_at is not None:
        values["created_at"] = created_at
    if alert_id is not None:
        values["id"] = alert_id
    return Alert.model_validate(values)


def alert_identity(alert: Alert) -> tuple[AlertType, str, str]:
    """Return the bounded identity used to suppress duplicate active alerts."""

    return (alert.type, alert.resource_type, alert.resource_id)


def find_active_alert(
    alerts: Iterable[Alert],
    candidate: Alert,
) -> Alert | None:
    identity = alert_identity(candidate)
    return next(
        (
            alert
            for alert in alerts
            if alert.status is not AlertStatus.RESOLVED and alert_identity(alert) == identity
        ),
        None,
    )


def add_or_get_active_alert(
    alerts: Iterable[Alert],
    candidate: Alert,
) -> tuple[tuple[Alert, ...], Alert, bool]:
    """Add a new alert unless the same resource already has that active alert type.

    The boolean is true only when a new alert was added. Persistence adapters can use
    the same identity as a conditional unique key for OPEN/ACKNOWLEDGED rows.
    """

    current = tuple(alerts)
    active = find_active_alert(current, candidate)
    if active is not None:
        return current, active, False
    return (*current, candidate), candidate, True


def replace_alert(alerts: Iterable[Alert], updated: Alert) -> tuple[Alert, ...]:
    """Replace one lifecycle state without silently inserting a missing alert."""

    current = tuple(alerts)
    if not any(alert.id == updated.id for alert in current):
        raise KeyError(str(updated.id))
    return tuple(updated if alert.id == updated.id else alert for alert in current)


__all__ = [
    "add_or_get_active_alert",
    "alert_identity",
    "create_alert",
    "find_active_alert",
    "replace_alert",
]
