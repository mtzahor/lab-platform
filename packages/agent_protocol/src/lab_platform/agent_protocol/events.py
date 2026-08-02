from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from lab_platform.agent_protocol.messages import (
    AgentBenchSnapshot,
    AgentStatus,
    BenchConnectivity,
    BenchHealth,
    ProtocolModel,
    _as_utc,
    validate_bounded_json_mapping,
    validate_local_bench_id,
)
from lab_platform.models import (
    BufferedAgentEvent,
    ReconciliationReport,
    RemoteArtifactMetadata,
    RemoteCommandStatus,
)
from pydantic import Field, field_validator, model_validator


class ReservationLeaseAppliedPayload(ProtocolModel):
    reservation_id: UUID
    agent_id: UUID
    bench_id: str = Field(min_length=3, max_length=600)
    lease_version: int = Field(ge=1, strict=True)
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def normalize_confirmed_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Reservation lease confirmation timestamp")


class AgentStatusPayload(ProtocolModel):
    agent_id: UUID
    boot_id: UUID
    status: AgentStatus
    changed_at: datetime
    reason: str | None = Field(default=None, max_length=1000)
    lease_application: ReservationLeaseAppliedPayload | None = None

    @field_validator("changed_at")
    @classmethod
    def normalize_changed_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Agent status timestamp")


class BenchAddedPayload(ProtocolModel):
    boot_id: UUID
    bench: AgentBenchSnapshot
    changed_at: datetime

    @field_validator("changed_at")
    @classmethod
    def normalize_changed_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Bench change timestamp")


class BenchRemovedPayload(ProtocolModel):
    boot_id: UUID
    local_bench_id: str = Field(min_length=1, max_length=500)
    changed_at: datetime

    @field_validator("local_bench_id")
    @classmethod
    def validate_local_bench_id_field(cls, value: str) -> str:
        return validate_local_bench_id(value)

    @field_validator("changed_at")
    @classmethod
    def normalize_changed_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Bench removal timestamp")


class BenchHealthChangedPayload(ProtocolModel):
    boot_id: UUID
    local_bench_id: str = Field(min_length=1, max_length=500)
    connectivity: BenchConnectivity
    health: BenchHealth
    changed_at: datetime

    @field_validator("local_bench_id")
    @classmethod
    def validate_local_bench_id_field(cls, value: str) -> str:
        return validate_local_bench_id(value)

    @field_validator("changed_at")
    @classmethod
    def normalize_changed_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Bench health timestamp")


class CommandAcceptedPayload(ProtocolModel):
    command_id: UUID
    accepted_at: datetime
    journal_status: RemoteCommandStatus = RemoteCommandStatus.ACCEPTED
    local_operation_id: UUID | None = None

    @field_validator("accepted_at")
    @classmethod
    def normalize_accepted_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Command acceptance timestamp")

    @field_validator("journal_status")
    @classmethod
    def validate_status(cls, value: RemoteCommandStatus) -> RemoteCommandStatus:
        if value not in {
            RemoteCommandStatus.ACCEPTED,
            RemoteCommandStatus.RUNNING,
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
        }:
            raise ValueError("Accepted command replay has an invalid journal status")
        return value


class CommandRejectedPayload(ProtocolModel):
    command_id: UUID
    rejected_at: datetime
    error_code: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)

    @field_validator("rejected_at")
    @classmethod
    def normalize_rejected_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Command rejection timestamp")


class OperationEventPayload(ProtocolModel):
    command_id: UUID
    local_operation_id: UUID | None = None
    occurred_at: datetime
    progress: int | None = Field(default=None, ge=0, le=100, strict=True)
    message: str | None = Field(default=None, max_length=2000)
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)

    @field_validator("result", mode="before")
    @classmethod
    def validate_result(cls, value: object) -> object:
        if value is None:
            return value
        return validate_bounded_json_mapping(
            value,
            field="Operation event result",
            maximum_bytes=1_048_576,
        )

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Operation event timestamp")


class WorkflowProgressPayload(ProtocolModel):
    command_id: UUID
    local_workflow_run_id: UUID
    occurred_at: datetime
    step_index: int = Field(ge=0, strict=True)
    step_count: int = Field(ge=1, strict=True)
    step_name: str = Field(min_length=1, max_length=500)
    progress: int = Field(ge=0, le=100, strict=True)
    message: str | None = Field(default=None, max_length=2000)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _as_utc(value, field="Workflow progress timestamp")

    @model_validator(mode="after")
    def validate_step_index(self) -> WorkflowProgressPayload:
        if self.step_index >= self.step_count:
            raise ValueError("step_index must be smaller than step_count")
        return self


class ArtifactCreatedPayload(ProtocolModel):
    artifact: RemoteArtifactMetadata


class EventBatchPayload(ProtocolModel):
    events: tuple[BufferedAgentEvent, ...] = Field(max_length=10_000)


class ReconciliationReportPayload(ProtocolModel):
    report: ReconciliationReport
