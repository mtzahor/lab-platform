from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from lab_platform.models.domain import LEGACY_ORGANISATION_ID, LabModel
from pydantic import Field, field_validator, model_validator


class QueueEntryStatus(StrEnum):
    WAITING = "waiting"
    PROMOTED = "promoted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class QueueEntry(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID = LEGACY_ORGANISATION_ID
    bench_id: str
    owner: str
    owner_principal_id: UUID | None = None
    owner_principal_type: Literal["USER", "SERVICE_ACCOUNT"] | None = None
    requested_duration_seconds: int = Field(gt=0)
    description: str | None = Field(default=None, max_length=2000)
    status: QueueEntryStatus = QueueEntryStatus.WAITING
    created_at: datetime
    promoted_at: datetime | None = None
    cancelled_at: datetime | None = None
    position: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = None

    @field_validator("created_at", "promoted_at", "cancelled_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @field_validator("description")
    @classmethod
    def _normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_principal_identity(self) -> QueueEntry:
        if (self.owner_principal_id is None) != (self.owner_principal_type is None):
            raise ValueError(
                "owner_principal_id and owner_principal_type must be provided together"
            )
        return self


class BenchOperationLock(LabModel):
    bench_id: str
    operation_id: UUID
    acquired_at: datetime
    expires_at: datetime | None = None

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class TimelineCategory(StrEnum):
    RESERVATION = "reservation"
    OPERATION = "operation"
    HEALTH = "health"
    HARDWARE = "hardware"
    SYSTEM = "system"
    WORKFLOW = "workflow"


class BenchTimelineEntry(LabModel):
    id: UUID = Field(default_factory=uuid4)
    bench_id: str
    timestamp: datetime
    category: TimelineCategory
    event_type: str
    actor: str | None = None
    reservation_id: UUID | None = None
    operation_id: UUID | None = None
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        assert normalized is not None
        return normalized


class RecoveryReport(LabModel):
    started_at: datetime
    completed_at: datetime
    interrupted_operations: int = Field(default=0, ge=0)
    stale_locks_removed: int = Field(default=0, ge=0)
    reservations_expired: int = Field(default=0, ge=0)
    reservations_activated: int = Field(default=0, ge=0)
    queue_entries_promoted: int = Field(default=0, ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def _validate_range(self) -> RecoveryReport:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Phase 3 timestamps must be timezone-aware")
    return value.astimezone(UTC)
