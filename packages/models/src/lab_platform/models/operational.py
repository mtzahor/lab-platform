from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from lab_platform.models.domain import LabModel, utc_now
from pydantic import Field, field_validator, model_validator

BENCH_MAINTENANCE_LABEL = "lab-platform.io/maintenance"
BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL = "lab-platform.io/pre-maintenance-status"


class FailureCategory(StrEnum):
    USER_ERROR = "USER_ERROR"
    WORKFLOW_ERROR = "WORKFLOW_ERROR"
    FIRMWARE_ERROR = "FIRMWARE_ERROR"
    TARGET_ERROR = "TARGET_ERROR"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    FLASH_ERROR = "FLASH_ERROR"
    SERIAL_ERROR = "SERIAL_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    AGENT_ERROR = "AGENT_ERROR"
    PLUGIN_ERROR = "PLUGIN_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    UNKNOWN = "UNKNOWN"


INFRASTRUCTURE_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.TARGET_ERROR,
        FailureCategory.DEVICE_DISCONNECTED,
        FailureCategory.FLASH_ERROR,
        FailureCategory.SERIAL_ERROR,
        FailureCategory.NETWORK_ERROR,
        FailureCategory.AGENT_ERROR,
        FailureCategory.PLUGIN_ERROR,
        FailureCategory.INFRASTRUCTURE_ERROR,
    }
)


class FailureClassification(LabModel):
    error_code: str | None = Field(default=None, max_length=200)
    category: FailureCategory
    infrastructure_related: bool


class OperationalInterval(LabModel):
    started_at: datetime
    ended_at: datetime

    @field_validator("started_at", "ended_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_range(self) -> OperationalInterval:
        if self.ended_at <= self.started_at:
            raise ValueError("ended_at must be later than started_at")
        return self


class BenchUtilisation(LabModel):
    observation_seconds: float = Field(ge=0)
    available_seconds: float = Field(ge=0)
    unavailable_seconds: float = Field(ge=0)
    utilised_seconds: float = Field(ge=0)
    utilisation_ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_durations(self) -> BenchUtilisation:
        tolerance = 1e-6
        if self.available_seconds > self.observation_seconds + tolerance:
            raise ValueError("available_seconds cannot exceed observation_seconds")
        if self.utilised_seconds > self.available_seconds + tolerance:
            raise ValueError("utilised_seconds cannot exceed available_seconds")
        if (
            abs(self.observation_seconds - self.available_seconds - self.unavailable_seconds)
            > tolerance
        ):
            raise ValueError("available and unavailable time must cover the observation window")
        if self.available_seconds == 0 and self.utilisation_ratio is not None:
            raise ValueError("utilisation_ratio must be absent when no time was available")
        if self.available_seconds > 0 and self.utilisation_ratio is None:
            raise ValueError("utilisation_ratio is required when time was available")
        return self


class QueueOutcome(StrEnum):
    WAITING = "WAITING"
    PROMOTED = "PROMOTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class QueueWaitObservation(LabModel):
    queued_at: datetime
    outcome: QueueOutcome
    resolved_at: datetime | None = None

    @field_validator("queued_at", "resolved_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_validator(mode="after")
    def validate_timeline(self) -> QueueWaitObservation:
        if self.outcome is QueueOutcome.WAITING and self.resolved_at is not None:
            raise ValueError("a waiting queue observation cannot have resolved_at")
        if self.outcome is not QueueOutcome.WAITING and self.resolved_at is None:
            raise ValueError("a terminal queue observation requires resolved_at")
        if self.resolved_at is not None and self.resolved_at < self.queued_at:
            raise ValueError("resolved_at cannot be earlier than queued_at")
        return self


class QueueMetrics(LabModel):
    promoted_samples: int = Field(ge=0)
    average_wait_seconds: float | None = Field(default=None, ge=0)
    median_wait_seconds: float | None = Field(default=None, ge=0)
    p95_wait_seconds: float | None = Field(default=None, ge=0)
    abandoned: int = Field(ge=0)
    abandonment_rate: float | None = Field(default=None, ge=0, le=1)
    queue_depth: int = Field(ge=0)


class OperationReliabilityObservation(LabModel):
    bench_id: str = Field(min_length=1, max_length=600)
    succeeded: bool
    completed_at: datetime
    failure_category: FailureCategory | None = None
    error_code: str | None = Field(default=None, max_length=200)
    workflow_name: str | None = Field(default=None, max_length=200)
    firmware_version: str | None = Field(default=None, max_length=200)
    actor_id: str | None = Field(default=None, max_length=200)

    @field_validator("completed_at")
    @classmethod
    def normalize_completed_at(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> OperationReliabilityObservation:
        if self.succeeded and (self.failure_category is not None or self.error_code is not None):
            raise ValueError("a successful observation cannot carry failure details")
        return self


class ReliabilityMetrics(LabModel):
    operations: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    infrastructure_failures: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    failure_counts: dict[FailureCategory, int] = Field(default_factory=dict)


class FlakyBenchPolicy(LabModel):
    window_size: int = Field(default=20, ge=1, le=10_000)
    minimum_operations: int = Field(default=10, ge=1, le=10_000)
    minimum_infrastructure_failures: int = Field(default=3, ge=1, le=10_000)
    infrastructure_failure_rate_threshold: float = Field(default=0.2, gt=0, le=1)
    minimum_distinct_contexts: int = Field(default=2, ge=1, le=100)

    @model_validator(mode="after")
    def validate_window(self) -> FlakyBenchPolicy:
        if self.minimum_operations > self.window_size:
            raise ValueError("minimum_operations cannot exceed window_size")
        if self.minimum_infrastructure_failures > self.window_size:
            raise ValueError("minimum_infrastructure_failures cannot exceed window_size")
        return self


class FlakyBenchAssessment(LabModel):
    bench_id: str = Field(min_length=1, max_length=600)
    potentially_flaky: bool
    sample_size: int = Field(ge=0)
    infrastructure_failures: int = Field(ge=0)
    infrastructure_failure_rate: float | None = Field(default=None, ge=0, le=1)
    distinct_contexts: int = Field(ge=0)
    primary_failure: str | None = Field(default=None, max_length=200)
    reasons: tuple[str, ...] = ()


class BenchMaintenanceStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    MAINTENANCE_RECOMMENDED = "MAINTENANCE_RECOMMENDED"
    MAINTENANCE = "MAINTENANCE"
    OFFLINE = "OFFLINE"


class MaintenanceRecommendation(LabModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    failure_category: FailureCategory
    heuristic: bool = True


class BenchMaintenanceState(LabModel):
    bench_id: str = Field(min_length=1, max_length=600)
    status: BenchMaintenanceStatus = BenchMaintenanceStatus.HEALTHY
    updated_at: datetime = Field(default_factory=utc_now)
    reason: str | None = Field(default=None, max_length=2_000)
    manually_set: bool = False

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @property
    def accepts_new_reservations(self) -> bool:
        return self.status not in {
            BenchMaintenanceStatus.MAINTENANCE,
            BenchMaintenanceStatus.OFFLINE,
        }


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertType(StrEnum):
    AGENT_OFFLINE = "AGENT_OFFLINE"
    BENCH_DEGRADED = "BENCH_DEGRADED"
    REPEATED_FAILURES = "REPEATED_FAILURES"
    STORAGE_USAGE = "STORAGE_USAGE"
    BACKUP_FAILURE = "BACKUP_FAILURE"
    DATABASE_ISSUE = "DATABASE_ISSUE"
    HIGH_QUEUE_WAIT_TIME = "HIGH_QUEUE_WAIT_TIME"
    PLUGIN_UNHEALTHY = "PLUGIN_UNHEALTHY"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"


class Alert(LabModel):
    id: UUID = Field(default_factory=uuid4)
    severity: AlertSeverity
    type: AlertType
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=600)
    status: AlertStatus = AlertStatus.OPEN
    message: str = Field(min_length=1, max_length=2_000)
    created_at: datetime = Field(default_factory=utc_now)
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None

    @field_validator("created_at", "acknowledged_at", "resolved_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Alert:
        if self.acknowledged_at is not None and self.acknowledged_at < self.created_at:
            raise ValueError("acknowledged_at cannot be earlier than created_at")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at cannot be earlier than created_at")
        if (
            self.acknowledged_at is not None
            and self.resolved_at is not None
            and self.resolved_at < self.acknowledged_at
        ):
            raise ValueError("resolved_at cannot be earlier than acknowledged_at")
        if self.status is AlertStatus.OPEN and (
            self.acknowledged_at is not None or self.resolved_at is not None
        ):
            raise ValueError("an open alert cannot carry lifecycle timestamps")
        if self.status is AlertStatus.ACKNOWLEDGED and (
            self.acknowledged_at is None or self.resolved_at is not None
        ):
            raise ValueError("an acknowledged alert requires only acknowledged_at")
        if self.status is AlertStatus.RESOLVED and self.resolved_at is None:
            raise ValueError("a resolved alert requires resolved_at")
        return self

    def acknowledge(self, observed_at: datetime | None = None) -> Alert:
        if self.status is AlertStatus.RESOLVED:
            raise ValueError("a resolved alert cannot be acknowledged")
        if self.status is AlertStatus.ACKNOWLEDGED:
            return self
        timestamp = _as_utc(observed_at or utc_now())
        return Alert.model_validate(
            {
                **self.model_dump(mode="python"),
                "status": AlertStatus.ACKNOWLEDGED,
                "acknowledged_at": timestamp,
            }
        )

    def resolve(self, observed_at: datetime | None = None) -> Alert:
        if self.status is AlertStatus.RESOLVED:
            return self
        timestamp = _as_utc(observed_at or utc_now())
        return Alert.model_validate(
            {
                **self.model_dump(mode="python"),
                "status": AlertStatus.RESOLVED,
                "resolved_at": timestamp,
            }
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "BENCH_MAINTENANCE_LABEL",
    "BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL",
    "INFRASTRUCTURE_FAILURE_CATEGORIES",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
    "AlertType",
    "BenchMaintenanceState",
    "BenchMaintenanceStatus",
    "BenchUtilisation",
    "FailureCategory",
    "FailureClassification",
    "FlakyBenchAssessment",
    "FlakyBenchPolicy",
    "MaintenanceRecommendation",
    "OperationReliabilityObservation",
    "OperationalInterval",
    "QueueMetrics",
    "QueueOutcome",
    "QueueWaitObservation",
    "ReliabilityMetrics",
]
