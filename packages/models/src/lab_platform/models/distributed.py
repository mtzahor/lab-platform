from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID, uuid4

from lab_platform.models.domain import HealthStatus, LabModel, utc_now
from pydantic import Field, field_validator, model_validator

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GLOBAL_BENCH_ID_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?/[A-Za-z0-9][A-Za-z0-9._-]{0,499}$"
)


class GlobalBenchStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"


class GlobalBenchKind(StrEnum):
    SIMULATED = "SIMULATED"
    PHYSICAL = "PHYSICAL"


class RemoteCommandType(StrEnum):
    PROBE = "PROBE"
    FLASH = "FLASH"
    RESET = "RESET"
    READ_SERIAL = "READ_SERIAL"
    RUN_WORKFLOW = "RUN_WORKFLOW"
    CANCEL_OPERATION = "CANCEL_OPERATION"
    REFRESH_INVENTORY = "REFRESH_INVENTORY"


class RemoteCommandStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_REMOTE_COMMAND_STATUSES = frozenset(
    {
        RemoteCommandStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED,
        RemoteCommandStatus.CANCELLED,
        RemoteCommandStatus.EXPIRED,
    }
)

REMOTE_COMMAND_TRANSITIONS: dict[RemoteCommandStatus, frozenset[RemoteCommandStatus]] = {
    RemoteCommandStatus.CREATED: frozenset(
        {
            RemoteCommandStatus.QUEUED,
            RemoteCommandStatus.DISPATCHED,
            RemoteCommandStatus.EXPIRED,
            RemoteCommandStatus.CANCELLED,
        }
    ),
    RemoteCommandStatus.QUEUED: frozenset(
        {
            RemoteCommandStatus.DISPATCHED,
            RemoteCommandStatus.EXPIRED,
            RemoteCommandStatus.CANCELLED,
        }
    ),
    RemoteCommandStatus.DISPATCHED: frozenset(
        {
            RemoteCommandStatus.ACCEPTED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.EXPIRED,
            RemoteCommandStatus.UNKNOWN,
        }
    ),
    RemoteCommandStatus.ACCEPTED: frozenset(
        {
            RemoteCommandStatus.RUNNING,
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.UNKNOWN,
        }
    ),
    RemoteCommandStatus.RUNNING: frozenset(
        {
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.UNKNOWN,
        }
    ),
    RemoteCommandStatus.UNKNOWN: frozenset(
        {
            RemoteCommandStatus.ACCEPTED,
            RemoteCommandStatus.RUNNING,
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.EXPIRED,
        }
    ),
}


class DistributedOperationStatus(StrEnum):
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


TERMINAL_DISTRIBUTED_OPERATION_STATUSES = frozenset(
    {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }
)

DISTRIBUTED_OPERATION_TRANSITIONS: dict[
    DistributedOperationStatus, frozenset[DistributedOperationStatus]
] = {
    DistributedOperationStatus.CREATED: frozenset(
        {
            DistributedOperationStatus.DISPATCHED,
            DistributedOperationStatus.CANCELLED,
            DistributedOperationStatus.FAILED,
        }
    ),
    DistributedOperationStatus.DISPATCHED: frozenset(
        {
            DistributedOperationStatus.ACCEPTED,
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.CANCELLED,
            DistributedOperationStatus.FAILED,
        }
    ),
    DistributedOperationStatus.ACCEPTED: frozenset(
        {
            DistributedOperationStatus.RUNNING,
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }
    ),
    DistributedOperationStatus.RUNNING: frozenset(
        {
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }
    ),
    DistributedOperationStatus.UNKNOWN: frozenset(
        {
            DistributedOperationStatus.RECONCILING,
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }
    ),
    DistributedOperationStatus.RECONCILING: frozenset(
        {
            DistributedOperationStatus.ACCEPTED,
            DistributedOperationStatus.RUNNING,
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }
    ),
}


class ArtifactTransferDirection(StrEnum):
    CONTROL_PLANE_TO_AGENT = "CONTROL_PLANE_TO_AGENT"
    AGENT_TO_CONTROL_PLANE = "AGENT_TO_CONTROL_PLANE"


class ArtifactTransferStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class BufferedEventPriority(IntEnum):
    PROGRESS = 10
    STATE = 50
    FAILURE = 80
    TERMINAL = 100


class ProtocolMessageDirection(StrEnum):
    AGENT_TO_CONTROL_PLANE = "agent_to_control_plane"
    CONTROL_PLANE_TO_AGENT = "control_plane_to_agent"


class ProtocolMessageOutcome(StrEnum):
    RECEIVED = "RECEIVED"
    SENT = "SENT"
    HANDLED = "HANDLED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class AgentTimelineSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AgentConnectionRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    boot_id: UUID
    protocol_version: str = Field(min_length=1, max_length=100)
    connected_at: datetime = Field(default_factory=utc_now)
    last_heartbeat_at: datetime
    disconnected_at: datetime | None = None
    last_sequence_number: int = Field(default=0, ge=0, strict=True)
    observed_clock_offset_seconds: float = Field(default=0, ge=-86_400, le=86_400)

    @field_validator("connected_at", "last_heartbeat_at", "disconnected_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Agent connection timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> AgentConnectionRecord:
        if self.last_heartbeat_at < self.connected_at:
            raise ValueError("last_heartbeat_at cannot be earlier than connected_at")
        if self.disconnected_at is not None and self.disconnected_at < self.last_heartbeat_at:
            raise ValueError("disconnected_at cannot be earlier than last_heartbeat_at")
        return self


class GlobalBenchRecord(LabModel):
    id: str = Field(min_length=3, max_length=600)
    agent_id: UUID
    agent_slug: str = Field(min_length=1, max_length=100)
    local_bench_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=500)
    backend_id: str = Field(min_length=1, max_length=200)
    kind: GlobalBenchKind
    target_type: str | None = Field(default=None, max_length=200)
    status: GlobalBenchStatus
    health: HealthStatus
    capabilities: frozenset[str] = Field(default_factory=frozenset, max_length=256)
    labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    firmware_version: str | None = Field(default=None, max_length=200)
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("id")
    @classmethod
    def validate_global_id(cls, value: str) -> str:
        if _GLOBAL_BENCH_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Global bench ID must use '<agent-slug>/<local-bench-id>' syntax")
        return value

    @field_validator("last_seen_at", "created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Global bench timestamp")

    @model_validator(mode="after")
    def validate_identity_and_timeline(self) -> GlobalBenchRecord:
        if self.id != f"{self.agent_slug}/{self.local_bench_id}":
            raise ValueError("Global bench ID does not match its Agent and local identity")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.last_seen_at is not None and self.last_seen_at > self.updated_at:
            raise ValueError("last_seen_at cannot be later than updated_at")
        return self

    @property
    def online(self) -> bool:
        return self.status in {GlobalBenchStatus.ONLINE, GlobalBenchStatus.DEGRADED}


class ReservationLease(LabModel):
    reservation_id: UUID
    agent_id: UUID
    bench_id: str = Field(min_length=3, max_length=600)
    owner: str = Field(min_length=1, max_length=200)
    valid_from: datetime
    valid_until: datetime
    lease_version: int = Field(ge=1, strict=True)
    released_at: datetime | None = None

    @field_validator("valid_from", "valid_until", "released_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Reservation lease timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> ReservationLease:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.released_at is not None and self.released_at < self.valid_from:
            raise ValueError("released_at cannot be earlier than valid_from")
        return self

    def is_valid_at(self, now: datetime, *, maximum_clock_skew_seconds: int = 0) -> bool:
        if maximum_clock_skew_seconds < 0:
            raise ValueError("maximum_clock_skew_seconds cannot be negative")
        observed = _as_utc(now, field="Lease validation timestamp")
        assert observed is not None
        skew = timedelta(seconds=maximum_clock_skew_seconds)
        return (
            self.released_at is None
            and self.valid_from - skew <= observed
            and observed <= self.valid_until + skew
        )


class RemoteCommand(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    bench_id: str = Field(min_length=3, max_length=600)
    command_type: RemoteCommandType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: RemoteCommandStatus = RemoteCommandStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    dispatched_at: datetime | None = None
    acknowledged_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=500)
    attempt_count: int = Field(default=0, ge=0, strict=True)
    operation_id: UUID | None = None
    reservation_id: UUID | None = None
    lease_version: int | None = Field(default=None, ge=1, strict=True)
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> object:
        return _bounded_json_mapping(value, field="Remote command payload", maximum_bytes=1_048_576)

    @field_validator(
        "created_at",
        "dispatched_at",
        "acknowledged_at",
        "started_at",
        "completed_at",
        "expires_at",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Remote command timestamp")

    @model_validator(mode="after")
    def validate_timeline_and_status(self) -> RemoteCommand:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        previous = self.created_at
        for name, value in (
            ("dispatched_at", self.dispatched_at),
            ("acknowledged_at", self.acknowledged_at),
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None:
                if value < previous:
                    raise ValueError(f"{name} is out of chronological order")
                previous = value
        if self.completed_at is not None and self.status not in TERMINAL_REMOTE_COMMAND_STATUSES:
            raise ValueError("completed_at requires a terminal command status")
        if self.status in TERMINAL_REMOTE_COMMAND_STATUSES and self.completed_at is None:
            raise ValueError("terminal command status requires completed_at")
        if (self.reservation_id is None) != (self.lease_version is None):
            raise ValueError("reservation_id and lease_version must be set together")
        if (
            self.status
            in {
                RemoteCommandStatus.DISPATCHED,
                RemoteCommandStatus.ACCEPTED,
                RemoteCommandStatus.RUNNING,
                RemoteCommandStatus.UNKNOWN,
                RemoteCommandStatus.SUCCEEDED,
                RemoteCommandStatus.FAILED,
            }
            and self.dispatched_at is None
        ):
            raise ValueError("Command status requires dispatched_at")
        if (
            self.status
            in {
                RemoteCommandStatus.ACCEPTED,
                RemoteCommandStatus.RUNNING,
            }
            and self.acknowledged_at is None
        ):
            raise ValueError("Command status requires acknowledged_at")
        if self.status is RemoteCommandStatus.RUNNING and self.started_at is None:
            raise ValueError("RUNNING command requires started_at")
        return self


class RemoteCommandAttempt(LabModel):
    id: UUID = Field(default_factory=uuid4)
    command_id: UUID
    attempt_number: int = Field(ge=1, strict=True)
    connection_id: UUID | None = None
    sequence_number: int | None = Field(default=None, ge=1, strict=True)
    dispatched_at: datetime
    acknowledged_at: datetime | None = None
    failed_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=200)

    @field_validator("dispatched_at", "acknowledged_at", "failed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Remote command attempt timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> RemoteCommandAttempt:
        for name, value in (
            ("acknowledged_at", self.acknowledged_at),
            ("failed_at", self.failed_at),
        ):
            if value is not None and value < self.dispatched_at:
                raise ValueError(f"{name} cannot be earlier than dispatched_at")
        if self.acknowledged_at is not None and self.failed_at is not None:
            raise ValueError("A command attempt cannot be both acknowledged and failed")
        return self


class DistributedOperation(LabModel):
    id: UUID = Field(default_factory=uuid4)
    remote_command_id: UUID
    agent_id: UUID
    bench_id: str = Field(min_length=3, max_length=600)
    reservation_id: UUID | None = None
    operation_type: str = Field(min_length=1, max_length=200)
    status: DistributedOperationStatus = DistributedOperationStatus.CREATED
    progress: int | None = Field(default=None, ge=0, le=100, strict=True)
    message: str | None = Field(default=None, max_length=2000)
    result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_agent_update_at: datetime | None = None
    reconciliation_deadline: datetime | None = None
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("result", mode="before")
    @classmethod
    def validate_result(cls, value: object) -> object:
        if value is None:
            return value
        return _bounded_json_mapping(
            value,
            field="Distributed operation result",
            maximum_bytes=1_048_576,
        )

    @field_validator(
        "created_at",
        "dispatched_at",
        "started_at",
        "completed_at",
        "last_agent_update_at",
        "reconciliation_deadline",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Distributed operation timestamp")

    @model_validator(mode="after")
    def validate_timeline_and_status(self) -> DistributedOperation:
        for name, value in (
            ("dispatched_at", self.dispatched_at),
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
            ("last_agent_update_at", self.last_agent_update_at),
            ("reconciliation_deadline", self.reconciliation_deadline),
        ):
            if value is not None and value < self.created_at:
                raise ValueError(f"{name} cannot be earlier than created_at")
        terminal = self.status in TERMINAL_DISTRIBUTED_OPERATION_STATUSES
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal operation status and completed_at must be set together")
        return self


class CommandJournalEntry(LabModel):
    command_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=500)
    command_type: RemoteCommandType
    bench_id: str = Field(min_length=3, max_length=600)
    status: RemoteCommandStatus
    received_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("result", mode="before")
    @classmethod
    def validate_result(cls, value: object) -> object:
        if value is None:
            return value
        return _bounded_json_mapping(value, field="Command journal result", maximum_bytes=1_048_576)

    @field_validator("received_at", "started_at", "completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Command journal timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> CommandJournalEntry:
        previous = self.received_at
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value is not None:
                if value < previous:
                    raise ValueError(f"{name} is out of chronological order")
                previous = value
        terminal = self.status in TERMINAL_REMOTE_COMMAND_STATUSES
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal journal status and completed_at must be set together")
        return self


class ReconciliationCommandState(LabModel):
    command_id: UUID
    status: RemoteCommandStatus
    updated_at: datetime
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("result", mode="before")
    @classmethod
    def validate_result(cls, value: object) -> object:
        if value is None:
            return value
        return _bounded_json_mapping(
            value,
            field="Reconciliation command result",
            maximum_bytes=1_048_576,
        )

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        normalized = _as_utc(value, field="Reconciliation command timestamp")
        assert normalized is not None
        return normalized


class ReconciliationBenchSnapshot(LabModel):
    local_bench_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=500)
    backend_id: str = Field(min_length=1, max_length=200)
    kind: GlobalBenchKind
    target_type: str | None = Field(default=None, max_length=200)
    status: GlobalBenchStatus
    health: HealthStatus
    capabilities: frozenset[str] = Field(default_factory=frozenset, max_length=256)
    labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    firmware_version: str | None = Field(default=None, max_length=200)

    @field_validator("local_bench_id")
    @classmethod
    def validate_local_bench_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,499}", value) is None:
            raise ValueError("local_bench_id contains unsupported characters")
        return value


class ReconciliationReport(LabModel):
    agent_id: UUID
    boot_id: UUID
    generated_at: datetime
    active_commands: tuple[ReconciliationCommandState, ...] = Field(max_length=10_000)
    recent_commands: tuple[ReconciliationCommandState, ...] = Field(max_length=10_000)
    local_reservation_leases: tuple[ReservationLease, ...] = Field(max_length=10_000)
    bench_snapshots: tuple[ReconciliationBenchSnapshot, ...] = Field(max_length=10_000)
    buffered_event_count: int = Field(ge=0, strict=True)

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        normalized = _as_utc(value, field="Reconciliation report timestamp")
        assert normalized is not None
        return normalized


class BufferedAgentEvent(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    sequence_number: int = Field(ge=1, strict=True)
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: BufferedEventPriority = BufferedEventPriority.STATE
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> object:
        return _bounded_json_mapping(value, field="Buffered event payload", maximum_bytes=262_144)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        normalized = _as_utc(value, field="Buffered event timestamp")
        assert normalized is not None
        return normalized


class RemoteArtifactMetadata(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    local_artifact_id: UUID
    command_id: UUID
    operation_id: UUID | None = None
    name: str = Field(min_length=1, max_length=500)
    artifact_type: str = Field(min_length=1, max_length=200)
    content_type: str | None = Field(default=None, max_length=200)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(min_length=64, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    uploaded_at: datetime | None = None

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _SHA256_PATTERN.fullmatch(normalized) is None:
            raise ValueError("sha256 must be a 64-character lowercase hexadecimal digest")
        return normalized

    @field_validator("created_at", "uploaded_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Remote artifact timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> RemoteArtifactMetadata:
        if self.uploaded_at is not None and self.uploaded_at < self.created_at:
            raise ValueError("uploaded_at cannot be earlier than created_at")
        return self


class ArtifactTransferRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    artifact_id: UUID
    direction: ArtifactTransferDirection
    status: ArtifactTransferStatus = ArtifactTransferStatus.PENDING
    token_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    completed_at: datetime | None = None
    expected_sha256: str = Field(min_length=64, max_length=64)
    expected_size_bytes: int = Field(ge=0, strict=True)
    attempt_count: int = Field(default=0, ge=0, strict=True)
    error_code: str | None = Field(default=None, max_length=200)

    @field_validator("token_hash", "expected_sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _SHA256_PATTERN.fullmatch(normalized) is None:
            raise ValueError("digest must be a 64-character lowercase hexadecimal value")
        return normalized

    @field_validator("created_at", "expires_at", "completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Artifact transfer timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> ArtifactTransferRecord:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot be earlier than created_at")
        if self.status is ArtifactTransferStatus.COMPLETED and self.completed_at is None:
            raise ValueError("completed transfer requires completed_at")
        return self


class ArtifactTransferAttempt(LabModel):
    id: UUID = Field(default_factory=uuid4)
    transfer_id: UUID
    attempt_number: int = Field(ge=1, strict=True)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    bytes_transferred: int = Field(default=0, ge=0, strict=True)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if _SHA256_PATTERN.fullmatch(normalized) is None:
            raise ValueError("sha256 must be a 64-character lowercase hexadecimal digest")
        return normalized

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Artifact transfer attempt timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> ArtifactTransferAttempt:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        return self


class ProtocolMessageJournalRecord(LabModel):
    message_id: UUID
    agent_id: UUID
    connection_id: UUID | None = None
    direction: ProtocolMessageDirection
    sequence_number: int = Field(ge=1, strict=True)
    message_type: str = Field(min_length=1, max_length=200)
    correlation_id: UUID | None = None
    payload_sha256: str = Field(min_length=64, max_length=64)
    observed_at: datetime = Field(default_factory=utc_now)
    handled_at: datetime | None = None
    outcome: ProtocolMessageOutcome

    @field_validator("payload_sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _SHA256_PATTERN.fullmatch(normalized) is None:
            raise ValueError("payload_sha256 must be a SHA-256 digest")
        return normalized

    @field_validator("observed_at", "handled_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Protocol message timestamp")

    @model_validator(mode="after")
    def validate_timeline(self) -> ProtocolMessageJournalRecord:
        if self.handled_at is not None and self.handled_at < self.observed_at:
            raise ValueError("handled_at cannot be earlier than observed_at")
        return self


class AgentTimelineRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    timestamp: datetime = Field(default_factory=utc_now)
    event_type: str = Field(min_length=1, max_length=200)
    severity: AgentTimelineSeverity = AgentTimelineSeverity.INFO
    message: str = Field(min_length=1, max_length=2000)
    correlation_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    deduplication_key: str | None = Field(default=None, max_length=500)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = _as_utc(value, field="Agent timeline timestamp")
        assert normalized is not None
        return normalized

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: object) -> object:
        return _bounded_json_mapping(value, field="Agent timeline metadata", maximum_bytes=262_144)


def _as_utc(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_json_mapping(
    value: object,
    *,
    field: str,
    maximum_bytes: int,
) -> object:
    if not isinstance(value, dict):
        return value
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} serialized bytes")

    item_count = 0

    def visit(item: object, depth: int) -> None:
        nonlocal item_count
        if depth > 12:
            raise ValueError(f"{field} cannot exceed 12 levels of nesting")
        if isinstance(item, dict):
            item_count += len(item)
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field} object keys must be strings")
                visit(child, depth + 1)
        elif isinstance(item, list):
            item_count += len(item)
            for child in item:
                visit(child, depth + 1)
        if item_count > 20_000:
            raise ValueError(f"{field} cannot exceed 20,000 nested items")

    visit(value, 1)
    return value
