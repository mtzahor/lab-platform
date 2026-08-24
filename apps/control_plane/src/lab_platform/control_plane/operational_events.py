from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from lab_platform.core.retention import RetentionEvent

OPERATIONAL_EVENT_TYPES: Final = frozenset(
    {
        "DATABASE_MIGRATION_STARTED",
        "DATABASE_MIGRATION_COMPLETED",
        "BACKUP_STARTED",
        "BACKUP_COMPLETED",
        "BACKUP_FAILED",
        "RESTORE_STARTED",
        "RESTORE_COMPLETED",
        "RETENTION_DELETION",
        "STORAGE_UNAVAILABLE",
        "VERSION_INCOMPATIBLE",
        "AGENT_UPGRADE_AVAILABLE",
    }
)

_ERROR_EVENTS = frozenset({"BACKUP_FAILED", "STORAGE_UNAVAILABLE"})
_WARNING_EVENTS = frozenset({"VERSION_INCOMPATIBLE", "AGENT_UPGRADE_AVAILABLE"})
_LOGGER = logging.getLogger("lab-platform.control-plane.operational-events")


class StructuredOperationalEventSink:
    """Emit the Phase 8 operational event catalogue through structured logging.

    Database migration, backup, restore, compatibility, and storage checks can run
    before the application database is available (or while it is being replaced).
    The process log is therefore their reliable common event transport. Retention
    additionally commits its audit record transactionally with the tombstone.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or _LOGGER

    def emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        if event_type not in OPERATIONAL_EVENT_TYPES:
            raise ValueError(f"unknown operational event type: {event_type}")
        level = (
            logging.ERROR
            if event_type in _ERROR_EVENTS
            else logging.WARNING
            if event_type in _WARNING_EVENTS
            else logging.INFO
        )
        self._logger.log(
            level,
            event_type,
            extra={
                "event_type": event_type,
                "event_payload": dict(payload),
            },
        )


class RetentionOperationalEventSink:
    """Adapt rich retention events to the shared structured event transport."""

    def __init__(self, events: StructuredOperationalEventSink | None = None) -> None:
        self._events = events or StructuredOperationalEventSink()

    async def emit(self, event: RetentionEvent) -> None:
        payload: dict[str, object] = {
            "occurred_at": event.occurred_at.isoformat(),
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "organisation_id": (
                str(event.organisation_id) if event.organisation_id is not None else None
            ),
            "count": event.count,
            **event.metadata,
        }
        self._events.emit(event.type, payload)


__all__ = [
    "OPERATIONAL_EVENT_TYPES",
    "RetentionOperationalEventSink",
    "StructuredOperationalEventSink",
]
