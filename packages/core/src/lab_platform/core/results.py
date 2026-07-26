from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from lab_platform.models import TestResult, TestStatus
from lab_platform.models.workflows import (
    WorkflowAction,
    WorkflowStepResult,
    WorkflowStepStatus,
)

_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")


def build_test_results(step_results: Iterable[WorkflowStepResult]) -> list[TestResult]:
    """Convert assertions and failed test-producing steps into stable CI test cases."""

    results: list[TestResult] = []
    for step in sorted(step_results, key=lambda item: item.step_index):
        produces_test = step.action is WorkflowAction.ASSERT_SERIAL or (
            step.action is WorkflowAction.READ_SERIAL and bool(step.output.get("until_pattern"))
        )
        if not produces_test and step.status not in {
            WorkflowStepStatus.FAILED,
            WorkflowStepStatus.CANCELLED,
            WorkflowStepStatus.SKIPPED,
        }:
            continue
        status = _test_status(step)
        results.append(
            TestResult(
                name=step.name or f"Step {step.step_index + 1}: {step.action.value}",
                status=status,
                duration_ms=_duration_ms(step),
                message=step.error_message,
                details={
                    "step_index": step.step_index,
                    "action": step.action.value,
                    "error_code": step.error_code,
                    "output": step.output,
                    "artifact_ids": [str(item) for item in step.artifact_ids],
                },
            )
        )
    return results


def render_junit_xml(
    suite_name: str,
    results: Iterable[TestResult],
) -> str:
    cases = list(results)
    failures = sum(item.status is TestStatus.FAILED for item in cases)
    errors = sum(item.status is TestStatus.ERROR for item in cases)
    skipped = sum(item.status is TestStatus.SKIPPED for item in cases)
    duration_seconds = sum(item.duration_ms for item in cases) / 1000
    suite = ET.Element(
        "testsuite",
        {
            "name": _xml_text(suite_name),
            "tests": str(len(cases)),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{duration_seconds:.3f}",
        },
    )
    for result in cases:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "name": _xml_text(result.name),
                "classname": _xml_text(suite_name),
                "time": f"{result.duration_ms / 1000:.3f}",
            },
        )
        message = _xml_text(result.message or result.status.value)
        if result.status is TestStatus.FAILED:
            ET.SubElement(case, "failure", {"message": message}).text = message
        elif result.status is TestStatus.ERROR:
            ET.SubElement(case, "error", {"message": message}).text = message
        elif result.status is TestStatus.SKIPPED:
            ET.SubElement(case, "skipped", {"message": message})
        if result.details:
            ET.SubElement(case, "system-out").text = _xml_text(
                json.dumps(result.details, sort_keys=True, default=str)
            )
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)


def _test_status(step: WorkflowStepResult) -> TestStatus:
    if step.status is WorkflowStepStatus.SUCCEEDED:
        return TestStatus.PASSED
    if step.status in {WorkflowStepStatus.CANCELLED, WorkflowStepStatus.SKIPPED}:
        return TestStatus.SKIPPED
    if step.error_code == "WORKFLOW_ASSERTION_FAILED":
        return TestStatus.FAILED
    return TestStatus.ERROR


def _duration_ms(step: WorkflowStepResult) -> int:
    if step.started_at is None or step.completed_at is None:
        return 0
    return max(0, round((step.completed_at - step.started_at).total_seconds() * 1000))


def _xml_text(value: str) -> str:
    return _INVALID_XML.sub("�", value)
