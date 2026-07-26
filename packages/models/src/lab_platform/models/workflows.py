from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias, cast
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
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
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


class WorkflowInputType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ARTIFACT = "artifact"


class ArtifactReference(WorkflowModel):
    artifact_id: UUID


class WorkflowInputModel(WorkflowModel):
    required: bool = False

    @model_validator(mode="after")
    def required_input_cannot_have_a_default(self) -> WorkflowInputModel:
        if self.required and getattr(self, "default", None) is not None:
            raise ValueError("a required workflow input cannot declare a default")
        return self


class StringWorkflowInput(WorkflowInputModel):
    type: Literal["string"]
    default: str | None = None


class IntegerWorkflowInput(WorkflowInputModel):
    type: Literal["integer"]
    default: int | None = None


class BooleanWorkflowInput(WorkflowInputModel):
    type: Literal["boolean"]
    default: bool | None = None


class ArtifactWorkflowInput(WorkflowInputModel):
    type: Literal["artifact"]
    default: ArtifactReference | None = None


WorkflowInput: TypeAlias = Annotated[
    StringWorkflowInput | IntegerWorkflowInput | BooleanWorkflowInput | ArtifactWorkflowInput,
    Field(discriminator="type"),
]
WorkflowInputDefinition = WorkflowInput


class WorkflowRequirements(WorkflowModel):
    capabilities: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)

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

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError("workflow label names must be non-empty strings")
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError("workflow label values must be non-empty strings")
            normalized[raw_key.strip()] = raw_value.strip()
        return normalized


class WorkflowStepModel(WorkflowModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)


class FlashWorkflowStep(WorkflowStepModel):
    action: Literal["flash"]
    firmware: Path
    version: str | None = None


class ResetWorkflowStep(WorkflowStepModel):
    action: Literal["reset"]


class ReadSerialWorkflowStep(WorkflowStepModel):
    action: Literal["read_serial"]
    until_pattern: str | None = None
    timeout_seconds: float | str = 10.0
    max_lines: int | str | None = 500

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float | str) -> float | str:
        return _validate_templated_number(
            value,
            field="timeout_seconds",
            minimum=0,
            maximum=3600,
            integer=False,
        )

    @field_validator("max_lines")
    @classmethod
    def validate_max_lines(cls, value: int | str | None) -> int | str | None:
        if value is None:
            return None
        return cast(
            int | str,
            _validate_templated_number(
                value,
                field="max_lines",
                minimum=0,
                maximum=100_000,
                integer=True,
            ),
        )

    @field_validator("until_pattern")
    @classmethod
    def validate_until_pattern(cls, value: str | None) -> str | None:
        if value is not None and "${" not in value:
            _compile_pattern(value)
        return value


class AssertSerialWorkflowStep(WorkflowStepModel):
    action: Literal["assert_serial"]
    pattern: str = Field(min_length=1)

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        if "${" not in value:
            _compile_pattern(value)
        return value


class WaitWorkflowStep(WorkflowStepModel):
    action: Literal["wait"]
    seconds: float | str

    @field_validator("seconds")
    @classmethod
    def validate_seconds(cls, value: float | str) -> float | str:
        return _validate_templated_number(
            value,
            field="seconds",
            minimum=0,
            maximum=3600,
            integer=False,
        )


class ProbeWorkflowStep(WorkflowStepModel):
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
    inputs: dict[str, WorkflowInput] = Field(default_factory=dict)
    requirements: WorkflowRequirements
    steps: list[WorkflowStep] = Field(min_length=1)

    @field_validator("inputs", mode="before")
    @classmethod
    def validate_input_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        for name in value:
            if not isinstance(name, str) or _INPUT_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid workflow input name: {name!r}")
        return value

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
    name: str = Field(default="", max_length=500)
    action: WorkflowAction
    status: WorkflowStepStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    artifact_ids: list[UUID] = Field(default_factory=list)

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


def _validate_templated_number(
    value: float | int | str,
    *,
    field: str,
    minimum: float,
    maximum: float,
    integer: bool,
) -> float | int | str:
    if isinstance(value, str):
        if "${" in value:
            return value
        expected = "an integer" if integer else "a number"
        raise ValueError(f"{field} must be {expected} or an input placeholder")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    if integer and not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or an input placeholder")
    if value <= minimum or value > maximum:
        raise ValueError(f"{field} must be greater than {minimum:g} and at most {maximum:g}")
    return value


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Workflow timestamps must be timezone-aware")
    return value.astimezone(UTC)


_INPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
