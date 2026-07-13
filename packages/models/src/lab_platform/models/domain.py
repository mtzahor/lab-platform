from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class LabModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BenchStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    AVAILABLE = "available"
    RESERVED = "reserved"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class OperationType(StrEnum):
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    POWER_CYCLE = "power_cycle"
    FLASH_FIRMWARE = "flash_firmware"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


TERMINAL_OPERATION_STATUSES = frozenset(
    {OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.CANCELLED}
)
ACTIVE_OPERATION_STATUSES = frozenset(
    {OperationStatus.PENDING, OperationStatus.RUNNING, OperationStatus.CANCEL_REQUESTED}
)

OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PENDING: frozenset({OperationStatus.RUNNING, OperationStatus.CANCELLED}),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.CANCEL_REQUESTED: frozenset(
        {
            OperationStatus.CANCELLED,
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
        }
    ),
}


class Capability(LabModel):
    name: str
    description: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class Device(LabModel):
    name: str
    kind: str
    status: BenchStatus = BenchStatus.ONLINE


class Bench(LabModel):
    """Phase 0 compatibility model used by plugins and the simulator facade."""

    name: str
    status: BenchStatus = BenchStatus.ONLINE
    capabilities: list[str] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)


class BenchSnapshot(LabModel):
    id: str
    name: str
    status: BenchStatus
    online: bool
    powered: bool | None
    reserved_by: str | None = None
    firmware_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class Reservation(LabModel):
    id: UUID
    bench_id: str
    owner: str
    created_at: datetime
    released_at: datetime | None = None
    status: ReservationStatus = ReservationStatus.ACTIVE


class Operation(LabModel):
    id: UUID
    bench_id: str
    type: OperationType
    status: OperationStatus
    requested_by: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress: int | None = Field(default=0, ge=0, le=100)
    message: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def pending(
        cls,
        bench_id: str,
        operation_type: OperationType,
        requested_by: str,
        *,
        now: datetime | None = None,
    ) -> Operation:
        return cls(
            id=uuid4(),
            bench_id=bench_id,
            type=operation_type,
            status=OperationStatus.PENDING,
            requested_by=requested_by,
            created_at=now or utc_now(),
            message="Waiting to start",
        )

    def transition(
        self,
        target: OperationStatus,
        *,
        now: datetime | None = None,
        progress: int | None = None,
        message: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Operation:
        from lab_platform.models.errors import InvalidOperationTransition

        if target not in OPERATION_TRANSITIONS.get(self.status, frozenset()):
            raise InvalidOperationTransition(self.status, target)
        timestamp = now or utc_now()
        changes: dict[str, object] = {
            "status": target,
            "message": message if message is not None else self.message,
            "error_code": error_code,
            "error_message": error_message,
        }
        if target is OperationStatus.RUNNING:
            changes["started_at"] = timestamp
        if target in TERMINAL_OPERATION_STATUSES:
            changes["completed_at"] = timestamp
        if progress is not None:
            changes["progress"] = progress
        elif target is OperationStatus.SUCCEEDED:
            changes["progress"] = 100
        return self.model_copy(update=changes)


class FirmwareInput(LabModel):
    filename: str
    local_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    version: str | None = None


class FirmwareArtifact(LabModel):
    sha256: str
    filename: str
    local_path: Path
    size_bytes: int
    version: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class BackendProgress(LabModel):
    percent: int = Field(ge=0, le=100)
    message: str


class EventRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=utc_now)
    type: str
    source: str
    bench_id: str | None = None
    operation_id: UUID | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Firmware(LabModel):
    name: str
    version: str
    checksum: str | None = None


class Event(LabModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class PluginMetadata(LabModel):
    name: str
    version: str
    author: str
    description: str
    capabilities: list[str] = Field(default_factory=list)


class HealthReport(LabModel):
    component: str
    status: HealthStatus
    message: str = ""
