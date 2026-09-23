from __future__ import annotations

# Fixture builders intentionally use flexible synthetic payloads.
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from lab_platform.control_plane.decision_api import create_decision_router
from lab_platform.control_plane.decision_worker import shadow_once
from lab_platform.control_plane.jev_provider import JevDecisionEngine, parse_response
from lab_platform.core.decision_engine.evaluation import evaluate
from lab_platform.core.decision_engine.interface import DiagnosisUnavailable
from lab_platform.core.decision_engine.policy import apply_policy
from lab_platform.core.decision_engine.schema import SCHEMA_VERSION, questions, questions_hash
from lab_platform.core.decision_engine.serializer import serialize_operation, serialize_run
from lab_platform.core.decision_engine.service import DecisionService
from lab_platform.core.decision_engine.settings import DecisionSettings
from lab_platform.core.results import build_test_results
from lab_platform.models.decisions import FeedbackRecord, OperatorFeedback
from lab_platform.models.distributed import (
    DistributedOperation,
    DistributedOperationStatus,
    RemoteCommandType,
)
from lab_platform.models.workflows import WorkflowRun, WorkflowStepResult
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.decisions import SQLiteDecisionRepository

if TYPE_CHECKING:
    from lab_platform.control_plane.runtime import ControlPlaneRuntime

FIXTURES = Path(__file__).parents[1] / "fixtures" / "decision_engine"
CASES = json.loads((FIXTURES / "cases.json").read_text())
RESPONSES = json.loads((FIXTURES / "synthetic_responses.json").read_text())
SETTINGS = DecisionSettings(enabled=True)


def sample(index=2):
    case = CASES[index]
    return WorkflowRun.model_validate(case["run"]), [
        WorkflowStepResult.model_validate(s) for s in case["steps"]
    ]


def wire(index=2):
    return copy.deepcopy(RESPONSES[CASES[index]["run"]["id"]])


def operation(index=2):
    run, steps = sample(index)
    return DistributedOperation(
        id=run.id,
        organisation_id=run.organisation_id,
        remote_command_id=uuid4(),
        agent_id=uuid4(),
        bench_id="synthetic-bench",
        operation_type="RUN_WORKFLOW",
        status=DistributedOperationStatus.FAILED,
        created_at=run.created_at,
        completed_at=run.completed_at,
        result={
            "workflow_run": run.model_dump(mode="json"),
            "steps": [s.model_dump(mode="json") for s in steps],
        },
    )


@pytest.fixture
def repository(tmp_path):
    database = SQLiteDatabase(tmp_path / "test.db")
    database.initialize()
    yield SQLiteDecisionRepository(database)
    database.close()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_state(case):
    expected = json.loads((FIXTURES / "golden_states.json").read_text())[case["id"]]
    state = serialize_run(
        WorkflowRun.model_validate(case["run"]),
        [WorkflowStepResult.model_validate(s) for s in case["steps"]],
    )
    assert state.model_dump() == expected


def test_schema_golden():
    expected = json.loads((FIXTURES / "schema.json").read_text())
    assert expected == {
        "schema_version": SCHEMA_VERSION,
        "questions_hash": questions_hash(),
        "questions": questions(),
    }


def test_serializer_excludes_secrets_and_personal_data():
    run, steps = sample()
    poisoned = {
        **steps[0].output,
        "api_key": "top-secret",
        "owner": "person@example.com",
        "logs": ["password=1234", "Bearer abcdefghijklmnop"],
        "measurements": [{"name": "supply", "value": 412, "unit": "mA", "password": "secret123"}],
        "health": {"password": "secret123", "status": "healthy"},
    }
    state = serialize_run(
        run.model_copy(
            update={"owner": "person@example.com", "error_message": "password=1234 timeout"}
        ),
        [steps[0].model_copy(update={"output": poisoned})],
    )
    for value in [
        "secret123",
        "top-secret",
        "password",
        "person@example.com",
        "Bearer",
        "abcdefghijklmnop",
    ]:
        assert value not in state.text
    assert "412" in state.text
    assert "timeout" in state.text
    assert state == serialize_run(
        run.model_copy(update={"error_message": "timeout"}),
        [steps[0].model_copy(update={"output": poisoned})],
    )


def test_oversized_and_malformed_state():
    run, steps = sample()
    with pytest.raises(DiagnosisUnavailable, match="oversized_state"):
        serialize_run(run, steps, max_bytes=10)
    with pytest.raises(DiagnosisUnavailable, match="serialization_error"):
        serialize_run(run, [steps[0].model_copy(update={"workflow_run_id": uuid4()})])
    with pytest.raises(DiagnosisUnavailable, match="serialization_error"):
        serialize_run(run, steps + steps)
    with pytest.raises(DiagnosisUnavailable, match="serialization_error"):
        serialize_operation(operation().model_copy(update={"result": {"steps": "invalid"}}))


def test_policy_conservative():
    decision = parse_response(wire())
    assert apply_policy(decision, SETTINGS) == ("recommend", "recommendation_only")
    assert apply_policy(decision, SETTINGS, incomplete_state=True)[0] == "human_review"
    assert apply_policy(parse_response(wire(14)), SETTINGS)[0] == "human_review"
    assert apply_policy(parse_response(wire(10)), SETTINGS)[0] == "human_review"
    for action in ["power_cycle_dut", "reset_interface", "restart_test", "request_human_review"]:
        assert (
            apply_policy(decision.model_copy(update={"recommended_action": action}), SETTINGS)[0]
            == "human_review"
        )
    assert (
        apply_policy(decision.model_copy(update={"recommended_action": "set_voltage"}), SETTINGS)[0]
        == "reject"
    )
    assert (
        apply_policy(decision.model_copy(update={"retry_safe_probability": 0.5}), SETTINGS)[0]
        == "human_review"
    )
    assert (
        apply_policy(decision.model_copy(update={"severity": "critical"}), SETTINGS)[0]
        == "human_review"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["answers"].pop("retry_safe"),
        lambda r: r["answers"]["diagnosis"].update(choice="invented"),
        lambda r: r["answers"]["recommended_action"].update(choice="set_voltage"),
        lambda r: r["answers"]["retry_safe"].update(noul=float("nan")),
        lambda r: r["answers"]["diagnosis"].update(confidence=float("inf")),
        lambda r: r["answers"]["diagnosis"].update(confidence=True),
        lambda r: r["answers"]["severity"].update(score=42),
        lambda r: r["answers"]["severity"].update(legend={}),
        lambda r: r["answers"]["diagnosis"].update(probabilities={"communication_failure": 1.0}),
        lambda r: r["answers"]["severity"].update(probabilities={"9": 1.0}),
        lambda r: r["answers"]["retry_safe"].update(type="choice"),
        lambda r: r.update(model=None),
    ],
)
def test_malformed_provider_responses(mutate):
    response = wire()
    mutate(response)
    with pytest.raises(DiagnosisUnavailable, match="malformed_response"):
        parse_response(response)


def test_one_batch_and_full_probabilities(repository):
    calls = []
    run, steps = sample()

    async def request(state, batch):
        calls.append((state, batch))
        return wire()

    async def scenario():
        service = DecisionService(
            JevDecisionEngine(SETTINGS, request=request), repository, SETTINGS
        )
        before = build_test_results(steps)
        record = await service.diagnose(
            run.id, run.organisation_id, lambda: serialize_run(run, steps)
        )
        assert record.status == "available"
        assert record.policy == "recommend"
        assert record.decision is not None
        assert record.decision.retry_safe_probability == 0.995
        assert record.decision.confidence.severity == 0.95
        stored = (await repository.list_for_run(run.id, run.organisation_id))[0]
        assert stored == record
        assert (
            stored.decision.probability_distribution["diagnosis"]
            == wire()["answers"]["diagnosis"]["probabilities"]
        )
        assert build_test_results(steps) == before
        assert not await repository.list_for_run(run.id, uuid4())
        feedback = FeedbackRecord(
            decision_id=record.id,
            run_id=run.id,
            organisation_id=run.organisation_id,
            actor_id="operator",
            feedback=OperatorFeedback(outcome="accepted"),
        )
        await repository.save_feedback(feedback)
        assert await repository.list_feedback(record.id, run.id, run.organisation_id) == [feedback]

    asyncio.run(scenario())
    assert len(calls) == 1
    assert set(calls[0][1]) == {"diagnosis", "recommended_action", "severity", "retry_safe"}


@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError(), "timeout"),
        (ConnectionError("secret"), "service_unavailable"),
        (RuntimeError("secret"), "service_unavailable"),
    ],
)
def test_provider_failure_isolated(repository, error, expected):
    run, steps = sample()

    async def request(state, questions):
        raise error

    async def scenario():
        service = DecisionService(
            JevDecisionEngine(SETTINGS, request=request), repository, SETTINGS
        )
        before = build_test_results(steps)
        record = await service.diagnose(
            run.id, run.organisation_id, lambda: serialize_run(run, steps)
        )
        assert record.error_code == expected
        assert record.status == "unavailable" and record.decision is None and record.policy is None
        assert "secret" not in record.model_dump_json()
        assert build_test_results(steps) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (503, "service_unavailable"),
    ],
)
def test_http_error_codes(status, expected):
    class ApiError(Exception):
        status: int

    error = ApiError("must not log raw provider text")
    error.status = status

    async def request(state, questions):
        raise error

    async def scenario():
        with pytest.raises(DiagnosisUnavailable, match=expected):
            await JevDecisionEngine(SETTINGS, request=request).diagnose_run(
                serialize_run(*sample())
            )

    asyncio.run(scenario())


def test_deadline_and_busy_admission(repository):
    cancelled = []

    async def request(state, questions):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    async def scenario():
        settings = DecisionSettings(enabled=True, timeout_ms=100)
        service = DecisionService(
            JevDecisionEngine(settings, request=request), repository, settings
        )
        run, steps = sample()
        first = asyncio.create_task(
            service.diagnose(run.id, run.organisation_id, lambda: serialize_run(run, steps))
        )
        await asyncio.sleep(0)
        second = await service.diagnose(
            run.id, run.organisation_id, lambda: serialize_run(run, steps)
        )
        assert second.error_code == "busy"
        result = await first
        assert result.error_code == "timeout"
        assert cancelled == [True]

    asyncio.run(scenario())


def test_disabled_does_not_import_or_call_sdk(repository, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SDK imported while disabled")

    monkeypatch.setattr(
        "lab_platform.control_plane.jev_provider.importlib.import_module", forbidden
    )

    async def scenario():
        settings = DecisionSettings()
        service = DecisionService(JevDecisionEngine(settings), repository, settings)
        run, _ = sample()
        record = await service.diagnose(run.id, run.organisation_id, forbidden)
        assert record.error_code == "disabled"

    asyncio.run(scenario())


def test_invalid_configuration_and_missing_key():
    assert DecisionSettings().with_environment({"JEV_MODE": "auto_retry"}).configuration_error
    assert DecisionSettings().with_environment({"JEV_TIMEOUT_MS": "nan"}).configuration_error
    settings = DecisionSettings().with_environment(
        {"JEV_ENABLED": "true", "JEV_API_KEY": "test-value"}
    )
    assert settings.enabled and "test-value" not in settings.model_dump_json()

    async def scenario():
        with pytest.raises(DiagnosisUnavailable, match="invalid_configuration"):
            await JevDecisionEngine(SETTINGS).diagnose_run(serialize_run(*sample()))

    asyncio.run(scenario())


def test_audit_failure_suppresses_recommendation():
    async def request(state, questions):
        return wire()

    async def save(record):
        raise OSError("disk full")

    async def scenario():
        service = DecisionService(
            JevDecisionEngine(SETTINGS, request=request), SimpleNamespace(save=save), SETTINGS
        )
        run, steps = sample()
        record = await service.diagnose(
            run.id, run.organisation_id, lambda: serialize_run(run, steps)
        )
        assert record.error_code == "audit_unavailable" and record.decision is None

    asyncio.run(scenario())


def test_evaluation_reports_regressions():
    async def request(state, questions):
        return RESPONSES[json.loads(state)["run_id"]]

    report = asyncio.run(evaluate(CASES, JevDecisionEngine(SETTINGS, request=request), SETTINGS))
    assert report["cases"] == 24 and not report["regressions"]
    changed = copy.deepcopy(CASES)
    changed[2]["expected"]["classification"] = "dut_failure"
    report = asyncio.run(evaluate(changed, JevDecisionEngine(SETTINGS, request=request), SETTINGS))
    assert "network_timeout" in report["regressions"]


def test_api_authorization_visibility_and_feedback(repository):
    op = operation()

    async def get_operation(operation_id, **kwargs):
        if operation_id != op.id:
            raise HTTPException(404)
        return op

    async def get_command(*args, **kwargs):
        return SimpleNamespace(command_type=RemoteCommandType.RUN_WORKFLOW)

    async def request(state, questions):
        return wire()

    def read_auth(authorization: str | None = Header(default=None)):
        if authorization not in {"reader", "writer"}:
            raise HTTPException(401)

    def write_auth(authorization: str | None = Header(default=None)):
        if authorization != "writer":
            raise HTTPException(403)

    runtime = SimpleNamespace(
        operational_access=SimpleNamespace(get_operation=get_operation),
        command_records=SimpleNamespace(get=get_command),
        decision_repository=repository,
        decision_settings=SETTINGS,
        decision_service=DecisionService(
            JevDecisionEngine(SETTINGS, request=request), repository, SETTINGS
        ),
    )
    app = FastAPI()
    app.include_router(
        create_decision_router(
            cast("ControlPlaneRuntime", runtime), read_auth=read_auth, write_auth=write_auth
        )
    )
    with TestClient(app) as client:
        path = f"/api/v1/workflow-runs/{op.id}"
        assert client.post(path + "/diagnose").status_code == 403
        assert client.get(path + "/diagnoses").status_code == 401
        assert (
            client.post(path + "/diagnose", headers={"authorization": "reader"}).status_code == 403
        )
        response = client.post(path + "/diagnose", headers={"authorization": "writer"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["diagnosis"] == "communication_failure" and body["policy"] == "recommend"
        assert json.loads(body["evidence"])["run_id"] == str(op.id)
        feedback_path = path + f"/diagnoses/{body['id']}/feedback"
        assert (
            client.post(
                feedback_path, headers={"authorization": "writer"}, json={"outcome": "accepted"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                feedback_path,
                headers={"authorization": "writer"},
                json={"outcome": "accepted", "action_taken": "set_voltage"},
            ).status_code
            == 422
        )
        runtime.decision_settings = DecisionSettings(enabled=True, mode="shadow")
        assert client.get(path + "/diagnoses", headers={"authorization": "reader"}).json() == {
            "enabled": False,
            "items": [],
        }
        assert (
            client.post(path + "/diagnose", headers={"authorization": "writer"}).status_code == 404
        )
        runtime.decision_settings = SETTINGS
        op = op.model_copy(update={"status": DistributedOperationStatus.RUNNING})
        assert (
            client.post(path + "/diagnose", headers={"authorization": "writer"}).status_code == 409
        )


def test_shadow_worker_cannot_affect_test_execution(repository):
    op = operation()

    async def candidates(schema):
        return [(op.id, op.organisation_id)]

    async def get(*args, **kwargs):
        return op

    async def request(state, questions):
        return wire()

    settings = DecisionSettings(enabled=True, mode="shadow")
    runtime = SimpleNamespace(
        decision_settings=settings,
        decision_repository=repository,
        operation_records=SimpleNamespace(get=get),
        decision_service=DecisionService(
            JevDecisionEngine(settings, request=request), repository, settings
        ),
    )
    repository.shadow_candidates = candidates

    async def scenario():
        await shadow_once(cast("ControlPlaneRuntime", runtime))
        await shadow_once(cast("ControlPlaneRuntime", runtime))
        records = await repository.list_for_run(op.id, op.organisation_id)
        assert len(records) == 1 and records[0].mode == "shadow"
        assert op.status == DistributedOperationStatus.FAILED

    asyncio.run(scenario())


def test_official_sdk_request_contract(monkeypatch):
    import httpx2
    from pydantic import SecretStr

    typesafe_sdk = pytest.importorskip("typesafe_sdk")
    original = typesafe_sdk.AsyncTypeSafeClient
    requests = []

    def handler(request):
        requests.append(request)
        payload = wire()
        payload["usage"] = {"input_tokens": 100, "output_tokens": 10}
        return httpx2.Response(200, json=payload)

    def factory(**kwargs):
        assert kwargs["retry"].max_retries == 0
        assert kwargs["base_url"] == "https://api.typesafe.ai"
        return original(**kwargs, transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", factory)
    settings = DecisionSettings(enabled=True, api_key=SecretStr("synthetic-api-key"))
    decision = asyncio.run(JevDecisionEngine(settings).diagnose_run(serialize_run(*sample())))
    assert decision.classification == "communication_failure"
    assert decision.raw_metadata["sdk_version"] == "0.7.1"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/systemone"
    assert set(json.loads(requests[0].content)["questions"]) == set(questions())


def test_truncated_measurements_require_review():
    run, steps = sample()
    steps = [
        steps[0].model_copy(
            update={
                "output": {"measurements": [{"name": f"value{i}", "value": i} for i in range(30)]}
            }
        )
    ]
    state = serialize_run(run, steps)
    assert state.incomplete
    assert json.loads(state.text)["observations_truncated"]
    assert (
        apply_policy(parse_response(wire()), SETTINGS, incomplete_state=state.incomplete)[0]
        == "human_review"
    )
