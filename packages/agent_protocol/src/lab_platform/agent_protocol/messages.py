from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from lab_platform.agent_protocol.versioning import ProtocolVersion
from lab_platform.models.agents import AgentStatus as AgentStatus
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProtocolModel(BaseModel):
    # Same-major protocol peers may add optional fields in later minor versions.
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


class MessageType(StrEnum):
    AGENT_HELLO = "AGENT_HELLO"
    AGENT_HEARTBEAT = "AGENT_HEARTBEAT"
    AGENT_STATUS = "AGENT_STATUS"
    BENCH_SNAPSHOT = "BENCH_SNAPSHOT"
    BENCH_ADDED = "BENCH_ADDED"
    BENCH_REMOVED = "BENCH_REMOVED"
    BENCH_HEALTH_CHANGED = "BENCH_HEALTH_CHANGED"
    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    OPERATION_STARTED = "OPERATION_STARTED"
    OPERATION_PROGRESS = "OPERATION_PROGRESS"
    OPERATION_SUCCEEDED = "OPERATION_SUCCEEDED"
    OPERATION_FAILED = "OPERATION_FAILED"
    OPERATION_CANCELLED = "OPERATION_CANCELLED"
    WORKFLOW_PROGRESS = "WORKFLOW_PROGRESS"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    EVENT_BATCH = "EVENT_BATCH"
    RECONCILIATION_REPORT = "RECONCILIATION_REPORT"

    WELCOME = "WELCOME"
    EVENT_ACK = "EVENT_ACK"
    COMMAND_REQUEST = "COMMAND_REQUEST"
    COMMAND_CANCEL = "COMMAND_CANCEL"
    INVENTORY_REFRESH_REQUEST = "INVENTORY_REFRESH_REQUEST"
    RECONCILIATION_REQUEST = "RECONCILIATION_REQUEST"
    ARTIFACT_UPLOAD_REQUEST = "ARTIFACT_UPLOAD_REQUEST"
    CONFIG_REFRESH_REQUEST = "CONFIG_REFRESH_REQUEST"
    DRAIN_AGENT = "DRAIN_AGENT"
    RESERVATION_ACTIVATED = "RESERVATION_ACTIVATED"
    RESERVATION_RELEASED = "RESERVATION_RELEASED"


AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES = frozenset(
    {
        MessageType.AGENT_HELLO,
        MessageType.AGENT_HEARTBEAT,
        MessageType.AGENT_STATUS,
        MessageType.BENCH_SNAPSHOT,
        MessageType.BENCH_ADDED,
        MessageType.BENCH_REMOVED,
        MessageType.BENCH_HEALTH_CHANGED,
        MessageType.COMMAND_ACCEPTED,
        MessageType.COMMAND_REJECTED,
        MessageType.OPERATION_STARTED,
        MessageType.OPERATION_PROGRESS,
        MessageType.OPERATION_SUCCEEDED,
        MessageType.OPERATION_FAILED,
        MessageType.OPERATION_CANCELLED,
        MessageType.WORKFLOW_PROGRESS,
        MessageType.ARTIFACT_CREATED,
        MessageType.EVENT_BATCH,
        MessageType.RECONCILIATION_REPORT,
    }
)

CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES = frozenset(
    {
        MessageType.WELCOME,
        MessageType.EVENT_ACK,
        MessageType.COMMAND_REQUEST,
        MessageType.COMMAND_CANCEL,
        MessageType.INVENTORY_REFRESH_REQUEST,
        MessageType.RECONCILIATION_REQUEST,
        MessageType.ARTIFACT_UPLOAD_REQUEST,
        MessageType.CONFIG_REFRESH_REQUEST,
        MessageType.DRAIN_AGENT,
        MessageType.RESERVATION_ACTIVATED,
        MessageType.RESERVATION_RELEASED,
    }
)


class BenchConnectivity(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class BenchHealth(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"


class BenchKind(StrEnum):
    SIMULATED = "simulated"
    PHYSICAL = "physical"


class AgentHelloPayload(ProtocolModel):
    agent_version: str = Field(min_length=1, max_length=100)
    protocol_version: str
    agent_name: str = Field(min_length=1, max_length=200)
    boot_id: UUID
    capabilities: frozenset[str] = Field(default_factory=frozenset, max_length=256)
    last_acknowledged_command_sequence: int = Field(default=0, ge=0, strict=True)

    @field_validator("protocol_version")
    @classmethod
    def validate_protocol_version(cls, value: str) -> str:
        return str(ProtocolVersion.parse(value))

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        return _normalized_string_set(value, field="capabilities")


class AgentHeartbeatPayload(ProtocolModel):
    agent_id: UUID
    boot_id: UUID
    uptime_seconds: int = Field(ge=0, strict=True)
    active_operations: int = Field(ge=0, strict=True)
    connected_benches: int = Field(ge=0, strict=True)
    degraded_benches: int = Field(ge=0, strict=True)
    event_buffer_size: int = Field(ge=0, strict=True)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value, field="heartbeat timestamp")

    @model_validator(mode="after")
    def validate_bench_counts(self) -> AgentHeartbeatPayload:
        if self.degraded_benches > self.connected_benches:
            raise ValueError("degraded_benches cannot exceed connected_benches")
        return self


class AgentBenchSnapshot(ProtocolModel):
    local_bench_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=500)
    backend_id: str = Field(min_length=1, max_length=200)
    kind: BenchKind
    target_type: str | None = Field(default=None, max_length=200)
    connectivity: BenchConnectivity
    health: BenchHealth
    capabilities: frozenset[str] = Field(default_factory=frozenset, max_length=256)
    labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    firmware_version: str | None = Field(default=None, max_length=200)

    @field_validator("local_bench_id")
    @classmethod
    def validate_local_bench_id(cls, value: str) -> str:
        return validate_local_bench_id(value)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        return _normalized_string_set(value, field="capabilities")

    @field_validator("labels", mode="before")
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
            key = raw_key.strip()
            label_value = raw_value.strip()
            if len(key) > 200:
                raise ValueError("label names cannot exceed 200 characters")
            if len(label_value) > 500:
                raise ValueError("label values cannot exceed 500 characters")
            if key in normalized:
                raise ValueError(f"duplicate label after normalization: {key}")
            normalized[key] = label_value
        return normalized


class BenchSnapshotPayload(ProtocolModel):
    boot_id: UUID
    generated_at: datetime
    benches: tuple[AgentBenchSnapshot, ...] = Field(max_length=10_000)

    @field_validator("benches", mode="before")
    @classmethod
    def enforce_bench_limit_before_parsing(cls, value: object) -> object:
        if isinstance(value, (list, tuple)) and len(value) > 10_000:
            raise ValueError("inventory snapshots cannot exceed 10,000 benches")
        return value

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="inventory snapshot timestamp")

    @model_validator(mode="after")
    def reject_duplicate_local_bench_ids(self) -> BenchSnapshotPayload:
        identifiers = [bench.local_bench_id for bench in self.benches]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("inventory snapshot contains duplicate local_bench_id values")
        return self


class WelcomePayload(ProtocolModel):
    connection_id: UUID
    accepted_protocol_version: str
    server_time: datetime
    heartbeat_interval_seconds: int = Field(gt=0, le=3600, strict=True)
    heartbeat_timeout_seconds: int = Field(gt=0, le=86_400, strict=True)
    offline_timeout_seconds: int = Field(gt=0, le=86_400, strict=True)

    @field_validator("accepted_protocol_version")
    @classmethod
    def validate_protocol_version(cls, value: str) -> str:
        return str(ProtocolVersion.parse(value))

    @field_validator("server_time")
    @classmethod
    def normalize_server_time(cls, value: datetime) -> datetime:
        return _as_utc(value, field="server timestamp")

    @model_validator(mode="after")
    def validate_timeouts(self) -> WelcomePayload:
        if self.heartbeat_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("heartbeat timeout must be longer than the heartbeat interval")
        if self.offline_timeout_seconds <= self.heartbeat_timeout_seconds:
            raise ValueError("offline timeout must be longer than the heartbeat timeout")
        return self


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _normalized_string_set(value: object, *, field: str) -> object:
    if not isinstance(value, (list, set, tuple, frozenset)):
        return value
    if len(value) > 256:
        raise ValueError(f"{field} cannot contain more than 256 values")
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} must contain non-empty strings")
        normalized_item = item.strip().lower()
        if len(normalized_item) > 200:
            raise ValueError(f"{field} values cannot exceed 200 characters")
        normalized.add(normalized_item)
    return frozenset(normalized)


def validate_local_bench_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,499}", value) is None:
        raise ValueError("local_bench_id contains unsupported characters")
    return value


def validate_bounded_json_mapping(
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
    return value
