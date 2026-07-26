from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from lab_platform.models.domain import LabModel, utc_now
from pydantic import Field, field_validator, model_validator


class ApiTokenScope(StrEnum):
    BENCHES_READ = "benches:read"
    RESERVATIONS_WRITE = "reservations:write"
    WORKFLOWS_RUN = "workflows:run"
    OPERATIONS_READ = "operations:read"
    ARTIFACTS_READ = "artifacts:read"
    ARTIFACTS_WRITE = "artifacts:write"
    CI_SESSIONS = "ci:sessions"


class CiProvider(StrEnum):
    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    JENKINS = "jenkins"
    LOCAL = "local"
    UNKNOWN = "unknown"


class CiSessionStatus(StrEnum):
    CREATED = "created"
    WAITING_FOR_BENCH = "waiting_for_bench"
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLEANUP_PENDING = "cleanup_pending"
    COMPLETED = "completed"


class CiOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class CleanupStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ArtifactOwnerType(StrEnum):
    CI_SESSION = "ci_session"
    WORKFLOW_RUN = "workflow_run"
    WORKFLOW_STEP = "workflow_step"
    OPERATION = "operation"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


TERMINAL_CI_OUTCOMES = frozenset(
    {
        CiOutcome.SUCCEEDED,
        CiOutcome.FAILED,
        CiOutcome.CANCELLED,
        CiOutcome.TIMED_OUT,
        CiOutcome.INFRASTRUCTURE_ERROR,
    }
)

CI_SESSION_TRANSITIONS: dict[CiSessionStatus, frozenset[CiSessionStatus]] = {
    CiSessionStatus.CREATED: frozenset(
        {
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CLEANUP_PENDING,
        }
    ),
    CiSessionStatus.WAITING_FOR_BENCH: frozenset(
        {
            CiSessionStatus.RESERVED,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.FAILED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }
    ),
    CiSessionStatus.RESERVED: frozenset(
        {
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.FAILED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }
    ),
    CiSessionStatus.RUNNING: frozenset(
        {
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
        }
    ),
    CiSessionStatus.CANCEL_REQUESTED: frozenset(
        {
            CiSessionStatus.CANCELLED,
            CiSessionStatus.FAILED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }
    ),
    CiSessionStatus.SUCCEEDED: frozenset({CiSessionStatus.CLEANUP_PENDING}),
    CiSessionStatus.FAILED: frozenset({CiSessionStatus.CLEANUP_PENDING}),
    CiSessionStatus.CANCELLED: frozenset({CiSessionStatus.CLEANUP_PENDING}),
    CiSessionStatus.TIMED_OUT: frozenset({CiSessionStatus.CLEANUP_PENDING}),
    CiSessionStatus.CLEANUP_PENDING: frozenset({CiSessionStatus.COMPLETED}),
    CiSessionStatus.COMPLETED: frozenset(),
}


class ApiToken(LabModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    token_hash: str = Field(min_length=32)
    owner: str = Field(min_length=1, max_length=200)
    scopes: set[ApiTokenScope] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @field_validator("created_at", "expires_at", "revoked_at", "last_used_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, model="API token")

    @model_validator(mode="after")
    def validate_timestamps(self) -> ApiToken:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class BenchRequest(LabModel):
    explicit_bench_id: str | None = Field(default=None, min_length=1, max_length=200)
    required_capabilities: set[str] = Field(default_factory=set)
    required_labels: dict[str, str] = Field(default_factory=dict)
    preferred_labels: dict[str, str] = Field(default_factory=dict)
    allow_simulated: bool = True
    allow_physical: bool = True
    maximum_wait_seconds: int = Field(default=600, ge=0, le=86_400)
    reservation_duration_seconds: int = Field(default=1800, gt=0, le=604_800)

    @field_validator("required_capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        if not isinstance(value, (list, set, tuple, frozenset)):
            return value
        normalized: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("required capabilities must be non-empty strings")
            normalized.add(item.strip().lower())
        return normalized

    @field_validator("required_labels", "preferred_labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError("label names must be non-empty strings")
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError("label values must be non-empty strings")
            normalized[raw_key.strip()] = raw_value.strip()
        return normalized

    @model_validator(mode="after")
    def require_an_allowed_backend_kind(self) -> BenchRequest:
        if not self.allow_simulated and not self.allow_physical:
            raise ValueError("at least one of allow_simulated or allow_physical must be true")
        return self


class CiSession(LabModel):
    id: UUID = Field(default_factory=uuid4)
    provider: CiProvider
    external_run_id: str = Field(min_length=1, max_length=500)
    repository: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    actor: str | None = None

    requested_by: str = Field(min_length=1, max_length=200)
    bench_id: str | None = None
    reservation_id: UUID | None = None
    workflow_run_id: UUID | None = None

    status: CiSessionStatus = CiSessionStatus.CREATED
    outcome: CiOutcome = CiOutcome.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None

    timeout_at: datetime | None = None
    cleanup_status: CleanupStatus = CleanupStatus.NOT_STARTED
    bench_request: BenchRequest | None = None

    @field_validator("created_at", "started_at", "completed_at", "heartbeat_at", "timeout_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, model="CI session")

    @model_validator(mode="after")
    def validate_timestamps(self) -> CiSession:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        return self


class CleanupResult(LabModel):
    reservation_released: bool = False
    workflow_stopped: bool = False
    locks_released: bool = False
    serial_closed: bool = False
    artifacts_finalized: bool = False
    errors: list[str] = Field(default_factory=list)


class ArtifactRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    owner_type: ArtifactOwnerType
    owner_id: UUID
    name: str = Field(min_length=1, max_length=500)
    artifact_type: str = Field(min_length=1, max_length=200)
    content_type: str | None = None
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, model="artifact")

    @model_validator(mode="after")
    def validate_expiry(self) -> ArtifactRecord:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class TestResult(LabModel):
    name: str = Field(min_length=1, max_length=500)
    status: TestStatus
    duration_ms: int = Field(ge=0)
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def _as_utc(value: datetime | None, *, model: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{model} timestamps must be timezone-aware")
    return value.astimezone(UTC)
