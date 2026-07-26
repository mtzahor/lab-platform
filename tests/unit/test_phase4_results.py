from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lab_platform.core.results import build_test_results, render_junit_xml
from lab_platform.models import TestResult as Result
from lab_platform.models import TestStatus as ResultStatus
from lab_platform.models.workflows import (
    WorkflowAction,
    WorkflowStepResult,
    WorkflowStepStatus,
)


def _step(
    index: int,
    action: WorkflowAction,
    status: WorkflowStepStatus,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    output: dict[str, object] | None = None,
) -> WorkflowStepResult:
    started = datetime(2026, 7, 23, 10, tzinfo=UTC)
    return WorkflowStepResult(
        workflow_run_id=uuid4(),
        step_index=index,
        name=f"Step {index} <check>",
        action=action,
        status=status,
        started_at=started,
        completed_at=started + timedelta(milliseconds=125),
        output=output or {},
        error_code=error_code,
        error_message=error_message,
    )


def test_results_map_assertions_errors_cancellation_and_serial_waits() -> None:
    steps = [
        _step(0, WorkflowAction.PROBE, WorkflowStepStatus.SUCCEEDED),
        _step(1, WorkflowAction.ASSERT_SERIAL, WorkflowStepStatus.SUCCEEDED),
        _step(
            2,
            WorkflowAction.ASSERT_SERIAL,
            WorkflowStepStatus.FAILED,
            error_code="WORKFLOW_ASSERTION_FAILED",
            error_message="not found",
        ),
        _step(
            3,
            WorkflowAction.FLASH,
            WorkflowStepStatus.FAILED,
            error_code="BACKEND_FAILURE",
        ),
        _step(4, WorkflowAction.READ_SERIAL, WorkflowStepStatus.CANCELLED),
        _step(
            5,
            WorkflowAction.READ_SERIAL,
            WorkflowStepStatus.SUCCEEDED,
            output={"until_pattern": "READY"},
        ),
    ]

    results = build_test_results(reversed(steps))

    assert [item.status for item in results] == [
        ResultStatus.PASSED,
        ResultStatus.FAILED,
        ResultStatus.ERROR,
        ResultStatus.SKIPPED,
        ResultStatus.PASSED,
    ]
    assert all(item.duration_ms == 125 for item in results)
    assert results[1].message == "not found"


def test_junit_is_deterministic_escaped_and_counts_outcomes() -> None:
    results = [
        Result(name="pass", status=ResultStatus.PASSED, duration_ms=100, details={}),
        Result(
            name="bad <value>",
            status=ResultStatus.FAILED,
            duration_ms=200,
            message="wrong & value\x01",
            details={"line": "A&B"},
        ),
        Result(name="infra", status=ResultStatus.ERROR, duration_ms=0, details={}),
        Result(name="skip", status=ResultStatus.SKIPPED, duration_ms=0, details={}),
    ]

    xml = render_junit_xml("ESP32 & smoke", results)
    root = ET.fromstring(xml)

    assert root.attrib == {
        "name": "ESP32 & smoke",
        "tests": "4",
        "failures": "1",
        "errors": "1",
        "skipped": "1",
        "time": "0.300",
    }
    cases = root.findall("testcase")
    assert cases[1].attrib["name"] == "bad <value>"
    assert cases[1].find("failure") is not None
    assert cases[2].find("error") is not None
    assert cases[3].find("skipped") is not None
    assert "�" in xml


def test_result_duration_is_zero_when_timestamps_are_missing_or_reversed() -> None:
    missing = _step(0, WorkflowAction.ASSERT_SERIAL, WorkflowStepStatus.SUCCEEDED).model_copy(
        update={"started_at": None}
    )
    reversed_time = _step(1, WorkflowAction.ASSERT_SERIAL, WorkflowStepStatus.SUCCEEDED).model_copy(
        update={
            "completed_at": datetime(2026, 7, 23, 9, tzinfo=UTC),
        }
    )

    assert [item.duration_ms for item in build_test_results([missing, reversed_time])] == [0, 0]
