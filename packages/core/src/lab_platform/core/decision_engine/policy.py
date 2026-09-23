from __future__ import annotations

from lab_platform.core.decision_engine.schema import ACTIONS
from lab_platform.core.decision_engine.settings import DecisionSettings
from lab_platform.models.decisions import DiagnosticDecision, PolicyResult

_REVIEW_ACTIONS = frozenset(
    {"power_cycle_dut", "reset_interface", "restart_test", "request_human_review"}
)


def apply_policy(
    decision: DiagnosticDecision,
    settings: DecisionSettings,
    *,
    incomplete_state: bool = False,
) -> tuple[PolicyResult, str]:
    # Explicit membership check defends this boundary even for future providers/models.
    if decision.recommended_action not in ACTIONS:
        return "reject", "action_not_allowed"
    if decision.classification == "unknown":
        return "human_review", "unknown_classification"
    if incomplete_state:
        return "human_review", "incomplete_evidence"
    if (
        decision.confidence.diagnosis < settings.diagnosis_min_confidence
        or decision.confidence.recommended_action < settings.action_min_confidence
    ):
        return "human_review", "low_confidence"
    if decision.recommended_action in _REVIEW_ACTIONS or decision.severity in {"high", "critical"}:
        return "human_review", "operator_required"
    if (
        decision.recommended_action == "retry_step"
        and decision.retry_safe_probability < settings.retry_safe_min_probability
    ):
        return "human_review", "retry_safety_uncertain"
    # No executor exists here. Even high confidence never permits automatic actions.
    return "recommend", "recommendation_only"
