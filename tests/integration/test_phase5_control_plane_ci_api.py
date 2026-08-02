from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiMaintenanceResult,
    DistributedCiSessionService,
)
from lab_platform.models import ApiTokenScope


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "artifacts": {"directory": tmp_path / "artifacts"},
                "development": {"allow_insecure_agent_transport": True},
            }
        )
    )


def _bootstrap_admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/tokens",
        json={
            "name": "ci-api-test",
            "owner": "ci/test-suite",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_session(
    client: TestClient,
    headers: dict[str, str],
    *,
    external_run_id: str = "build-42",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/ci/sessions",
        headers={**headers, "Idempotency-Key": f"github:labs/fw:{external_run_id}"},
        json={
            "provider": "github_actions",
            "external_run_id": external_run_id,
            "repository": "labs/firmware",
            "ref": "refs/heads/main",
            "commit_sha": "a" * 40,
            "actor": "octocat",
            "bench_request": {
                "required_capabilities": ["reset", "flash"],
                "required_labels": {"board": "esp32"},
                "preferred_labels": {"rack": "one"},
                "required_agent_labels": {"environment": "ci"},
                "preferred_location": "jerusalem",
                "allow_simulated": False,
                "allow_physical": True,
                "maximum_wait_seconds": 300,
                "reservation_duration_seconds": 900,
            },
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def test_ci_session_api_is_durable_idempotent_and_scoped(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap_admin(client)
        assert client.get("/api/v1/ci/sessions").status_code == 401

        created = _create_session(client, headers)
        session_id = UUID(str(created["id"]))
        assert created["status"] == "waiting_for_bench"
        assert created["requested_by"] == "ci/test-suite"
        assert created["cleanup_timeout_seconds"] == 300
        assert created["distributed_workflow"] is None
        bench_request = created["bench_request"]
        assert isinstance(bench_request, dict)
        assert bench_request["required_agent_labels"] == {"environment": "ci"}

        replayed = _create_session(client, headers)
        assert replayed["id"] == str(session_id)

        listing = client.get(
            "/api/v1/ci/sessions",
            headers=headers,
            params={"provider": "github_actions", "status": "waiting_for_bench"},
        )
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [str(session_id)]

        details = client.get(f"/api/v1/ci/sessions/{session_id}", headers=headers)
        assert details.status_code == 200
        assert details.json()["distributed"] is True
        assert details.json()["errors"] == []
        assert details.json()["cleanup"] is None

        heartbeat = client.post(
            f"/api/v1/ci/sessions/{session_id}/heartbeat",
            headers=headers,
            json={},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["status"] == "waiting_for_bench"

        content = b"hardware-results"
        artifact = client.post(
            "/api/v1/artifacts",
            headers=headers,
            data={
                "owner_type": "ci_session",
                "owner_id": str(session_id),
                "artifact_type": "test-results",
                "expected_sha256": hashlib.sha256(content).hexdigest(),
            },
            files={"file": ("results.json", content, "application/json")},
        )
        assert artifact.status_code == 201, artifact.text
        artifacts = client.get(
            f"/api/v1/ci/sessions/{session_id}/artifacts",
            headers=headers,
        )
        assert artifacts.status_code == 200
        assert [item["id"] for item in artifacts.json()["items"]] == [artifact.json()["id"]]

        cancelled = client.post(
            f"/api/v1/ci/sessions/{session_id}/cancel",
            headers=headers,
            json={},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "completed"
        assert cancelled.json()["outcome"] == "cancelled"
        assert cancelled.json()["cleanup_status"] == "succeeded"
        assert cancelled.json()["cleanup"]["reservation_released"] is True

        finalized = client.post(
            f"/api/v1/ci/sessions/{session_id}/finalize",
            headers=headers,
            json={},
        )
        assert finalized.status_code == 200
        assert finalized.json()["id"] == str(session_id)
        assert finalized.json()["status"] == "completed"

        missing = client.get(f"/api/v1/ci/sessions/{uuid4()}", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "CI_SESSION_NOT_FOUND"


def test_ci_run_routes_centrally_and_reports_no_compatible_bench(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap_admin(client)
        session = _create_session(client, headers, external_run_id="build-no-agent")
        workflow = client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "esp32-ci",
                "version": 1,
                "requirements": {"capabilities": ["reset"]},
                "steps": [{"action": "reset"}],
            },
        )
        assert workflow.status_code == 201, workflow.text

        response = client.post(
            f"/api/v1/ci/sessions/{session['id']}/run",
            headers=headers,
            json={
                "workflow_name": "esp32-ci",
                "version": 1,
                "agent_labels": {"environment": "ci"},
                "location": "jerusalem",
                "lease_ttl_seconds": 120,
                "command_timeout_seconds": 600,
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "NO_COMPATIBLE_BENCH"

        current = client.get(
            f"/api/v1/ci/sessions/{session['id']}",
            headers=headers,
        )
        assert current.status_code == 200
        assert current.json()["status"] == "waiting_for_bench"
        assert current.json()["distributed_workflow"] is None


def test_runtime_recovers_and_maintains_distributed_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    recovery = AsyncMock(return_value=DistributedCiMaintenanceResult())
    maintenance = AsyncMock(return_value=DistributedCiMaintenanceResult())
    monkeypatch.setattr(runtime.ci, "recover_incomplete", recovery)
    monkeypatch.setattr(runtime.ci, "process_maintenance", maintenance)

    assert isinstance(runtime.ci, DistributedCiSessionService)
    with TestClient(create_app(runtime)) as client:
        recovery.assert_awaited_once_with()
        assert client.portal is not None
        client.portal.call(runtime.monitor_once)
        maintenance.assert_awaited_once_with()
