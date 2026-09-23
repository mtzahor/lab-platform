from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from lab_platform.models.domain import LabModel, utc_now
from pydantic import Field

Diagnosis = Literal[
    "dut_failure",
    "communication_failure",
    "configuration_failure",
    "instrument_failure",
    "infrastructure_failure",
    "expected_test_failure",
    "unknown",
]
Action = Literal[
    "retry_step",
    "power_cycle_dut",
    "reset_interface",
    "restart_test",
    "mark_failed",
    "request_human_review",
    "no_action",
]
Severity = Literal["informational", "low", "medium", "high", "critical"]
PolicyResult = Literal["recommend", "auto_retry", "human_review", "reject"]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class DecisionConfidence(LabModel):
    diagnosis: Probability
    recommended_action: Probability
    severity: Probability


class DiagnosticDecision(LabModel):
    classification: Diagnosis
    recommended_action: Action
    severity: Severity
    retry_safe_probability: Probability
    confidence: DecisionConfidence
    probability_distribution: dict[str, dict[str, Probability]]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class DiagnosticState(LabModel):
    text: str
    sha256: str
    incomplete: bool = False


class DecisionRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    organisation_id: UUID
    timestamp: datetime = Field(default_factory=utc_now)
    schema_version: str
    questions_hash: str
    state: DiagnosticState | None = None
    decision: DiagnosticDecision | None = None
    policy: PolicyResult | None = None
    policy_reason: str | None = None
    policy_config: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["shadow", "recommend"]
    status: Literal["available", "unavailable"] = "unavailable"
    provider: str = "jev"
    requested_model: str
    latency_ms: float = 0
    error_code: str | None = None


class OperatorFeedback(LabModel):
    outcome: Literal["accepted", "rejected", "different_action_taken"]
    action_taken: Action | None = None
    actual_root_cause: Diagnosis | None = None
    notes: str = Field(default="", max_length=2000)


class FeedbackRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    run_id: UUID
    organisation_id: UUID
    timestamp: datetime = Field(default_factory=utc_now)
    actor_id: str = Field(max_length=200)
    feedback: OperatorFeedback
