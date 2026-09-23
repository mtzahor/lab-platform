from __future__ import annotations

from typing import Any

from lab_platform.core.decision_engine.interface import DecisionEngine
from lab_platform.core.decision_engine.policy import apply_policy
from lab_platform.core.decision_engine.schema import SCHEMA_VERSION, SEVERITIES
from lab_platform.core.decision_engine.serializer import serialize_run
from lab_platform.core.decision_engine.settings import DecisionSettings
from lab_platform.models.workflows import WorkflowRun, WorkflowStepResult


async def evaluate(
    cases: list[dict[str, Any]], engine: DecisionEngine, settings: DecisionSettings
) -> dict[str, Any]:
    rows = []
    for case in cases:
        try:
            state = serialize_run(
                WorkflowRun.model_validate(case["run"]),
                [WorkflowStepResult.model_validate(s) for s in case["steps"]],
            )
            decision = await engine.diagnose_run(state)
            expected = case["expected"]
            policy, _ = apply_policy(decision, settings, incomplete_state=state.incomplete)
            severity = SEVERITIES.index(decision.severity)
            rows.append(
                {
                    "id": case["id"],
                    "available": True,
                    "classification_agreement": decision.classification
                    == expected["classification"],
                    "action_agreement": decision.recommended_action
                    in expected["acceptable_actions"],
                    "severity_agreement": expected["severity_range"][0]
                    <= severity
                    <= expected["severity_range"][1],
                    "review_agreement": not expected["human_review_required"]
                    or policy == "human_review",
                    "confidence": decision.confidence.model_dump(),
                    "model": decision.model,
                }
            )
        except Exception:
            rows.append({"id": case["id"], "available": False})
    available = [row for row in rows if row["available"]]
    count = len(rows)
    metrics = {
        name: sum(bool(row.get(name)) for row in rows) / count if count else 0.0
        for name in (
            "classification_agreement",
            "action_agreement",
            "severity_agreement",
            "review_agreement",
        )
    }
    regressions = [
        row["id"]
        for row in rows
        if not row["available"] or not all(row.get(key) for key in metrics)
    ]
    histogram = {str(i / 10): 0 for i in range(10)}
    for row in available:
        bucket = min(9, int(row["confidence"]["recommended_action"] * 10))
        histogram[str(bucket / 10)] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "cases": count,
        "available": len(available),
        **metrics,
        "action_confidence_histogram": histogram,
        "regressions": regressions,
        "results": rows,
        "note": (
            "Confidence is not proof of correctness. Synthetic replay checks "
            "integration, not provider accuracy."
        ),
    }
