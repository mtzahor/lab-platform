from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from lab_platform.core.decision_engine.interface import DiagnosisUnavailable
from lab_platform.core.decision_engine.schema import SCHEMA_VERSION
from lab_platform.logging.structured import redact_log_text
from lab_platform.models.decisions import DiagnosticState
from lab_platform.models.distributed import DistributedOperation
from lab_platform.models.workflows import WorkflowRun, WorkflowStepResult

# Free-form logs, command payloads, firmware paths, owner/bench IDs and artifact
# contents are deliberately excluded. Only recorded evidence is used, never a probe.
_SECRET = re.compile(
    r"(?i)(api[_ -]?key|password|secret|credential|authorization|token|private[_ -]?key)"
)
_PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://\S+|(?:\d{1,3}\.){3}\d{1,3}")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_. -]{0,99}$")
_STATES = frozenset(
    {
        "healthy",
        "unhealthy",
        "warning",
        "online",
        "offline",
        "unknown",
        "disconnected",
        "malfunction",
        "ready",
        "not_ready",
        "on",
        "off",
        "booted",
        "not_booted",
        "timeout",
        "passed",
        "failed",
        "connected",
        "degraded",
        "unsafe",
        "safe",
    }
)
_EVENTS = (
    "timeout",
    "disconnect",
    "overcurrent",
    "overvoltage",
    "overheat",
    "boot",
    "serial",
    "network",
    "configuration",
    "assertion",
)
_UNITS = frozenset({"V", "mV", "A", "mA", "uA", "C", "ms", "s", "Hz", "ohm", "%"})
_NUMBERS = frozenset(
    {
        "current_ma",
        "voltage_v",
        "temperature_c",
        "timeout_seconds",
        "duration_ms",
        "seconds",
        "retry_count",
    }
)
_BOOLS = frozenset(
    {"matched", "uart_boot_detected", "connected", "powered", "online", "safety_checks_passed"}
)
_TEXT_EVIDENCE = frozenset(
    {"expected_result", "actual_result", "failed_assertion", "test_step", "communication_error"}
)


def safe_label(value: object) -> str:
    if not isinstance(value, str) or _SECRET.search(value) or _PII.search(value):
        return "[omitted]"
    redacted = redact_log_text(value)
    return redacted if _LABEL.fullmatch(redacted) else "[omitted]"


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and abs(value) <= 1e15
    )


def _evidence(output: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(_NUMBERS | _BOOLS):
        value = output.get(key)
        if (key in _NUMBERS and _number(value)) or (key in _BOOLS and type(value) is bool):
            result[key] = value
    for key in sorted(_TEXT_EVIDENCE):
        value = output.get(key)
        if isinstance(value, str):
            safe = safe_label(value)
            if safe != "[omitted]":
                result[key] = safe
        elif key in {"expected_result", "actual_result"} and type(value) is bool:
            result[key] = value
    raw_assertions = output.get("failed_assertions")
    if isinstance(raw_assertions, list):
        result["failed_assertions"] = [
            safe for item in raw_assertions[:20] if (safe := safe_label(item)) != "[omitted]"
        ]
    for key in ("dut_state", "fixture_state", "communication_status", "configuration_status"):
        if output.get(key) in _STATES if isinstance(output.get(key), str) else False:
            result[key] = output[key]
    for key in ("instruments", "health"):
        raw = output.get(key)
        if isinstance(raw, dict):
            result[key] = {
                safe_label(name): value.lower()
                for name, value in sorted(raw.items())[:20]
                if safe_label(name) != "[omitted]"
                and isinstance(value, str)
                and value.lower() in _STATES
            }
    raw_measurements = output.get("measurements")
    if isinstance(raw_measurements, list):
        measurements = []
        for raw in raw_measurements[:20]:
            if not isinstance(raw, dict):
                continue
            name = safe_label(raw.get("name"))
            if name == "[omitted]":
                continue
            item = {"name": name}
            for key in ("value", "lower_limit", "upper_limit", "expected"):
                if _number(raw.get(key)):
                    item[key] = raw[key]
            if isinstance(raw.get("unit"), str) and raw["unit"] in _UNITS:
                item["unit"] = raw["unit"]
            if raw.get("passed") is True or raw.get("passed") is False:
                item["passed"] = raw["passed"]
            measurements.append(item)
        result["measurements"] = measurements
    raw_logs = output.get("logs")
    if isinstance(raw_logs, list):
        result["log_signals"] = sorted(
            {
                event
                for line in raw_logs[:20]
                if isinstance(line, str)
                for event in _EVENTS
                if event in line.lower()
            }
        )
    return result


def serialize_run(
    run: WorkflowRun,
    steps: Sequence[WorkflowStepResult],
    *,
    max_bytes: int = 32768,
    run_id: UUID | None = None,
) -> DiagnosticState:
    if any(step.workflow_run_id != run.id for step in steps):
        raise DiagnosisUnavailable("serialization_error")
    return _serialize(
        run_id or run.id,
        run.workflow_name,
        run.status.value,
        run.current_step,
        run.error_code,
        run.error_message,
        steps,
        max_bytes,
    )


def serialize_operation(
    operation: DistributedOperation, *, max_bytes: int = 32768
) -> DiagnosticState:
    result = operation.result or {}
    workflow = result.get("workflow_run") or {}
    raw_steps = result.get("steps") or []
    if not isinstance(workflow, dict) or not isinstance(raw_steps, list):
        raise DiagnosisUnavailable("serialization_error")
    if len(raw_steps) > 500:
        raise DiagnosisUnavailable("oversized_state")
    try:
        steps = [WorkflowStepResult.model_validate(step) for step in raw_steps]
    except (ValueError, TypeError) as exc:
        raise DiagnosisUnavailable("serialization_error") from exc
    return _serialize(
        operation.id,
        workflow.get("workflow_name", operation.operation_type),
        operation.status.value.lower(),
        workflow.get("current_step"),
        operation.error_code,
        operation.error_message,
        steps,
        max_bytes,
    )


def _serialize(
    run_id: UUID,
    test_name: object,
    status: str,
    current_step: object,
    error_code: object,
    error_message: object,
    steps: Sequence[WorkflowStepResult],
    max_bytes: int,
) -> DiagnosticState:
    ordered = sorted(steps, key=lambda step: step.step_index)
    if len({step.step_index for step in ordered}) != len(ordered):
        raise DiagnosisUnavailable("serialization_error")
    # Fail closed on too much evidence; silently dropping a failed step is unsafe.
    if len(ordered) > 64:
        raise DiagnosisUnavailable("oversized_state")
    rendered: list[dict[str, Any]] = []
    for step in ordered:
        evidence = _evidence(step.output)
        rendered.append(
            {
                "index": step.step_index,
                "name": safe_label(step.name),
                "action": step.action.value,
                "status": step.status.value,
                "error_code": safe_label(step.error_code) if step.error_code else None,
                "error_signals": [
                    event for event in _EVENTS if event in (step.error_message or "").lower()
                ],
                "evidence": evidence,
            }
        )
    incomplete = not bool(steps) or any(
        isinstance(step.output.get(key), (list, dict)) and len(step.output[key]) > 20
        for step in steps
        for key in ("measurements", "instruments", "health")
    )
    latest: list[dict[str, Any]] = [
        item["evidence"] for item in rendered if isinstance(item["evidence"], dict)
    ]
    retry_counts = [item["retry_count"] for item in latest if "retry_count" in item]
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "test_name": safe_label(test_name),
        "status": status,
        "current_step": current_step if type(current_step) is int else None,
        "error_code": safe_label(error_code) if error_code else None,
        "error_signals": [event for event in _EVENTS if event in str(error_message or "").lower()],
        "retry_count": max(retry_counts) if retry_counts else None,
        "dut_state": next(
            (item["dut_state"] for item in reversed(latest) if "dut_state" in item), "unknown"
        ),
        "fixture_state": next(
            (item["fixture_state"] for item in reversed(latest) if "fixture_state" in item),
            "unknown",
        ),
        "steps": rendered,
        "observations_truncated": incomplete and bool(steps),
        "evidence_note": (
            "Recorded step evidence only. Missing values are unknown. Free-form "
            "logs and identifiers are omitted."
        ),
    }
    text = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
    if len(text.encode()) > max_bytes:
        raise DiagnosisUnavailable("oversized_state")
    return DiagnosticState(
        text=text, sha256=hashlib.sha256(text.encode()).hexdigest(), incomplete=incomplete
    )
