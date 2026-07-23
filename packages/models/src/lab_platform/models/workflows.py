from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class WorkflowAction(StrEnum):
    FLASH = "flash"
    RESET = "reset"
    READ_SERIAL = "read_serial"
    ASSERT_SERIAL = "assert_serial"
    WAIT = "wait"
    PROBE = "probe"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class WorkflowStepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_WORKFLOW_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.SUCCEEDED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.CANCELLED,
    }
)
ACTIVE_WORKFLOW_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.PENDING,
        WorkflowRunStatus.RUNNING,
        WorkflowRunStatus.CANCEL_REQUESTED,
    }
)


class WorkflowRequirements(WorkflowModel):
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("capability names must be non-empty strings")
            capability = item.strip().lower()
            if capability in normalized:
                raise ValueError(f"duplicate capability: {capability}")
            normalized.append(capability)
        return normalized


class FlashWorkflowStep(WorkflowModel):
    action: Literal["flash"]
    firmware: Path
    version: str | None = None


class ResetWorkflowStep(WorkflowModel):
    action: Literal["reset"]


class ReadSerialWorkflowStep(WorkflowModel):
    action: Literal["read_serial"]
    until_pattern: str | None = None
    timeout_seconds: float = Field(default=10, gt=0, le=3600)
    max_lines: int | None = Field(default=500, ge=1, le=100_000)

    @field_validator("until_pattern")
    @classmethod
    def validate_until_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            _compile_pattern(value)
        return value


class AssertSerialWorkflowStep(WorkflowModel):
    action: Literal["assert_serial"]
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        _compile_pattern(value)
        return value


class WaitWorkflowStep(WorkflowModel):
    action: Literal["wait"]
    seconds: float = Field(gt=0, le=3600)


class ProbeWorkflowStep(WorkflowModel):
    action: Literal["probe"]


WorkflowStep: TypeAlias = Annotated[
    FlashWorkflowStep
    | ResetWorkflowStep
    | ReadSerialWorkflowStep
    | AssertSerialWorkflowStep
    | WaitWorkflowStep
    | ProbeWorkflowStep,
    Field(discriminator="action"),
]


class WorkflowDefinition(WorkflowModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: int = Field(ge=1)
    description: str | None = None
    requirements: WorkflowRequirements
    steps: list[WorkflowStep] = Field(min_length=1)

    @model_validator(mode="after")
    def require_declared_step_capabilities(self) -> WorkflowDefinition:
        required = {
            capability
            for step in self.steps
            if (capability := _STEP_CAPABILITY.get(WorkflowAction(step.action))) is not None
        }
        undeclared = sorted(required.difference(self.requirements.capabilities))
        if undeclared:
            joined = ", ".join(undeclared)
            raise ValueError(f"workflow steps use undeclared capabilities: {joined}")
        return self


class WorkflowRun(WorkflowModel):
    id: UUID = Field(default_factory=uuid4)
    workflow_name: str
    workflow_version: int = Field(ge=1)
    bench_id: str
    owner: str
    reservation_id: UUID
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    current_step: int | None = Field(default=None, ge=0)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class WorkflowStepResult(WorkflowModel):
    id: UUID = Field(default_factory=uuid4)
    workflow_run_id: UUID
    step_index: int = Field(ge=0)
    action: WorkflowAction
    status: WorkflowStepStatus
    started_at: datetime
    completed_at: datetime | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


_STEP_CAPABILITY: dict[WorkflowAction, str | None] = {
    WorkflowAction.FLASH: "firmware",
    WorkflowAction.RESET: "reset",
    WorkflowAction.READ_SERIAL: "serial",
    WorkflowAction.ASSERT_SERIAL: "serial",
    WorkflowAction.WAIT: None,
    WorkflowAction.PROBE: "probe",
}


def _compile_pattern(pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Workflow timestamps must be timezone-aware")
    return value.astimezone(UTC)
