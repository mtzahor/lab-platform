from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.models import (
    ApiTokenScope,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
)
from lab_platform.persistence import SCHEMA_VERSION


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    config = ControlPlaneConfig.model_validate(
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
    return ControlPlaneRuntime(config)


def _bootstrap_admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/tokens",
        json={
            "name": "test-admin",
            "owner": "test-suite",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _enroll_agent(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    issued = client.post(
        "/api/v1/agents/enrollment-tokens",
        headers=headers,
        json={
            "name": "home lab",
            "expires_in_seconds": 300,
            "allowed_labels": {"environment": "test"},
        },
    )
    assert issued.status_code == 201, issued.text
    enrolled = client.post(
        "/api/v1/agents/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.0",
            "location": "jerusalem-home",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    payload = enrolled.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


async def _stage_remote_artifact(
    runtime: ControlPlaneRuntime,
    agent_id: UUID,
    content: bytes,
) -> RemoteArtifactMetadata:
    created_at = datetime.now(UTC)
    agent = await runtime.presence.get_agent(agent_id)
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-01",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="bench-01",
        name="Remote bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"reset"}),
        created_at=created_at,
        updated_at=created_at,
        last_seen_at=created_at,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        agent,
        (bench,),
        observed_at=created_at,
    )
    command = RemoteCommand(
        agent_id=agent.id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
        idempotency_key="remote-artifact-command",
    )
    await runtime.command_repository.create_bundle(command, None)
    artifact = await runtime.artifacts.register_remote_artifact(
        RemoteArtifactMetadata(
            agent_id=agent_id,
            local_artifact_id=uuid4(),
            command_id=command.id,
            name="remote-results.xml",
            artifact_type="junit",
            content_type="application/xml",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=created_at,
        )
    )

    async def chunks() -> AsyncIterator[bytes]:
        yield content

    await runtime.artifact_store.write_verified(
        artifact.id,
        artifact.sha256,
        artifact.size_bytes,
        chunks(),
        maximum_size_bytes=runtime.config.artifacts.max_upload_size_mb * 1024 * 1024,
    )
    uploaded = await runtime.remote_artifacts.update_uploaded(
        artifact.id,
        datetime.now(UTC),
    )
    assert uploaded is not None
    return uploaded


async def _stage_cancellable_operation(
    runtime: ControlPlaneRuntime,
    agent_id: UUID,
    bench_id: str,
) -> DistributedOperation:
    created_at = datetime.now(UTC)
    command = RemoteCommand(
        agent_id=agent_id,
        bench_id=bench_id,
        command_type=RemoteCommandType.PROBE,
        payload={"owner": "operation-owner"},
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=5),
        idempotency_key="cancellable-operation",
    )
    operation = DistributedOperation(
        remote_command_id=command.id,
        agent_id=agent_id,
        bench_id=bench_id,
        operation_type="PROBE",
        created_at=created_at,
    )
    command = command.model_copy(update={"operation_id": operation.id})
    _, persisted = await runtime.command_repository.create_bundle(command, operation)
    assert persisted is not None
    return persisted


async def _stage_completed_workflow_operation(
    runtime: ControlPlaneRuntime,
    agent_id: UUID,
    bench_id: str,
) -> DistributedOperation:
    created_at = datetime.now(UTC)
    dispatched_at = created_at + timedelta(milliseconds=1)
    completed_at = created_at + timedelta(milliseconds=2)
    local_workflow_run_id = uuid4()
    reservation_id = uuid4()
    command = RemoteCommand(
        agent_id=agent_id,
        bench_id=bench_id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={"owner": "operation-owner"},
        status=RemoteCommandStatus.SUCCEEDED,
        created_at=created_at,
        dispatched_at=dispatched_at,
        completed_at=completed_at,
        expires_at=created_at + timedelta(minutes=5),
        idempotency_key="completed-workflow-operation",
    )
    operation = DistributedOperation(
        remote_command_id=command.id,
        agent_id=agent_id,
        bench_id=bench_id,
        operation_type="RUN_WORKFLOW",
        status=DistributedOperationStatus.SUCCEEDED,
        progress=100,
        message="Remote workflow completed",
        result={
            "workflow_run": {
                "id": str(local_workflow_run_id),
                "workflow_name": "remote-smoke",
                "workflow_version": 1,
                "bench_id": "bench-01",
                "owner": "operation-owner",
                "reservation_id": str(reservation_id),
                "status": "succeeded",
                "current_step": 0,
                "created_at": created_at.isoformat(),
                "started_at": dispatched_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            },
            "steps": [
                {
                    "workflow_run_id": str(local_workflow_run_id),
                    "step_index": 0,
                    "name": "READY assertion",
                    "action": "assert_serial",
                    "status": "succeeded",
                    "started_at": dispatched_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "output": {"pattern": "READY"},
                    "artifact_ids": [],
                }
            ],
        },
        created_at=created_at,
        dispatched_at=dispatched_at,
        completed_at=completed_at,
        last_agent_update_at=completed_at,
    )
    command = command.model_copy(update={"operation_id": operation.id})
    _, persisted = await runtime.command_repository.create_bundle(command, operation)
    assert persisted is not None
    return persisted


def test_agent_administration_bootstrap_auth_and_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        oversized = client.post(
            "/api/v1/tokens",
            content=b"{}",
            headers={"Content-Length": str(1024 * 1024 + 1)},
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
        streamed_oversized = client.post(
            "/api/v1/tokens",
            content=iter(
                (
                    b'{"name":"' + b"x" * 600_000,
                    b"y" * 600_000 + b'","owner":"o","scopes":["agents:admin"]}',
                )
            ),
            headers={"Content-Type": "application/json"},
        )
        assert streamed_oversized.status_code == 413, streamed_oversized.text
        assert streamed_oversized.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
        # An empty token store opens only the one-time token bootstrap route, not
        # the rest of the administrative API.
        assert client.get("/api/v1/agents").status_code == 401
        assert (
            client.post(
                "/api/v1/agents/enrollment-tokens",
                json={"name": "must-not-bootstrap", "expires_in_seconds": 300},
            ).status_code
            == 401
        )
        incomplete_bootstrap = client.post(
            "/api/v1/tokens",
            json={
                "name": "incomplete",
                "owner": "test-suite",
                "scopes": [ApiTokenScope.AGENTS_ADMIN.value],
            },
        )
        assert incomplete_bootstrap.status_code == 403
        headers = _bootstrap_admin(client)
        tokens = client.get("/api/v1/tokens", headers=headers)
        assert tokens.status_code == 200
        assert len(tokens.json()["items"]) == 1
        assert "token_hash" not in tokens.json()["items"][0]
        bootstrap_id = tokens.json()["items"][0]["id"]
        last_admin = client.post(
            f"/api/v1/tokens/{bootstrap_id}/revoke",
            headers=headers,
            json={},
        )
        assert last_admin.status_code == 403

        successor = client.post(
            "/api/v1/tokens",
            headers=headers,
            json={
                "name": "successor-admin",
                "owner": "test-suite",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert successor.status_code == 201
        revoked = client.post(
            f"/api/v1/tokens/{successor.json()['id']}/revoke",
            headers=headers,
            json={},
        )
        assert revoked.status_code == 200
        assert revoked.json()["revoked_at"] is not None
        assert client.portal is not None
        audit_events = client.portal.call(runtime.audit_events.list)
        event_types = {event.type for event in audit_events}
        assert {"API_TOKEN_CREATED", "API_TOKEN_USED", "API_TOKEN_REVOKED"}.issubset(event_types)
        assert successor.json()["token"] not in repr(audit_events)
        assert client.get("/api/v1/agents").status_code == 401

        enrolled = _enroll_agent(client, headers)
        agent = enrolled["agent"]
        assert isinstance(agent, dict)
        agent_id = agent["id"]
        credential = enrolled["credential"]
        gateway_url = enrolled["gateway_url"]
        assert isinstance(agent_id, str)
        assert isinstance(credential, str) and credential.startswith("lpa_")
        assert isinstance(gateway_url, str) and gateway_url.endswith(agent_id)

        listing = client.get(
            "/api/v1/agents",
            headers=headers,
            params={"location": "jerusalem-home", "label": "environment=test"},
        )
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [agent_id]
        listed_agent = listing.json()["items"][0]
        assert listed_agent["upgrade_status"] == listed_agent["upgrade"]["status"]
        assert isinstance(listed_agent["upgrade"]["work_allowed"], bool)

        details = client.get(f"/api/v1/agents/{agent_id}", headers=headers)
        assert details.status_code == 200
        assert details.json()["upgrade_status"] == details.json()["upgrade"]["status"]
        assert details.json()["connection"] is None
        assert details.json()["benches"] == []

        metrics = client.get("/metrics", headers=headers)
        assert metrics.status_code == 200
        assert "database_healthy 1" in metrics.text
        assert f"database_schema_version {SCHEMA_VERSION}" in metrics.text
        assert "agents_offline 1" in metrics.text
        assert "bench_inventory_total 0" in metrics.text
        assert "workflows_active 0" in metrics.text
        assert "workflows_failed 0" in metrics.text
        assert "reservations_active 0" in metrics.text
        assert "reservation_queue_depth 0" in metrics.text
        assert "artifact_usage_bytes 0" in metrics.text

        close_agent = AsyncMock(return_value=False)
        monkeypatch.setattr(runtime.hub, "close_agent", close_agent)
        rotated = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate",
            json={"current_credential": credential},
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["credential_version"] == 2
        assert rotated.json()["credential"] != credential
        close_agent.assert_awaited_once_with(
            UUID(agent_id),
            code=4401,
            reason="Agent credential rotated",
        )

        revoked = client.post(f"/api/v1/agents/{agent_id}/revoke", headers=headers)
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "REVOKED"


def test_control_plane_artifact_upload_and_scoped_agent_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    content = b"phase-five-firmware"
    checksum = hashlib.sha256(content).hexdigest()
    owner_id = uuid4()

    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap_admin(client)
        enrolled = _enroll_agent(client, headers)
        agent = enrolled["agent"]
        assert isinstance(agent, dict)
        agent_id = UUID(agent["id"])

        uploaded = client.post(
            "/api/v1/artifacts",
            headers=headers,
            data={
                "owner_type": "operation",
                "owner_id": str(owner_id),
                "artifact_type": "firmware",
                "expected_sha256": checksum,
                "idempotency_key": "firmware-v1",
            },
            files={"file": ("firmware.bin", content, "application/octet-stream")},
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact = uploaded.json()
        assert artifact["sha256"] == checksum
        assert artifact["size_bytes"] == len(content)

        listed = client.get(
            "/api/v1/artifacts",
            headers=headers,
            params={"owner_type": "operation", "owner_id": str(owner_id)},
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [artifact["id"]]
        generic_content = client.get(
            f"/api/v1/artifacts/{artifact['id']}/content",
            headers=headers,
        )
        assert generic_content.status_code == 200
        assert generic_content.content == content

        assert client.portal is not None
        remote_content = b"<testsuite tests='1'/>"
        remote = client.portal.call(
            _stage_remote_artifact,
            runtime,
            agent_id,
            remote_content,
        )
        remote_listing = client.get(
            "/api/v1/artifacts",
            headers=headers,
            params={"agent_id": str(agent_id)},
        )
        assert remote_listing.status_code == 200
        assert [item["id"] for item in remote_listing.json()["items"]] == [str(remote.id)]
        remote_metadata = client.get(f"/api/v1/artifacts/{remote.id}", headers=headers)
        assert remote_metadata.status_code == 200
        assert remote_metadata.json()["local_artifact_id"] == str(remote.local_artifact_id)
        remote_download = client.get(
            f"/api/v1/artifacts/{remote.id}/content",
            headers=headers,
        )
        assert remote_download.status_code == 200
        assert remote_download.content == remote_content

        operation = client.portal.call(
            _stage_cancellable_operation,
            runtime,
            agent_id,
            f"{agent['slug']}/bench-01",
        )
        wrong_owner = client.post(
            f"/api/v1/operations/{operation.id}/cancel",
            headers=headers,
            json={"owner": "somebody-else"},
        )
        assert wrong_owner.status_code == 403
        cancelled = client.post(
            f"/api/v1/operations/{operation.id}/cancel",
            headers=headers,
            json={"owner": "operation-owner", "reason": "test cancellation"},
        )
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == DistributedOperationStatus.CANCELLED.value

        completed_workflow = client.portal.call(
            _stage_completed_workflow_operation,
            runtime,
            agent_id,
            f"{agent['slug']}/bench-01",
        )
        workflow_run = client.get(
            f"/api/v1/workflow-runs/{completed_workflow.id}",
            headers=headers,
        )
        assert workflow_run.status_code == 200, workflow_run.text
        assert workflow_run.json()["id"] == str(completed_workflow.id)
        assert workflow_run.json()["status"] == "succeeded"
        assert workflow_run.json()["local_workflow_run_id"] is not None
        workflow_results = client.get(
            f"/api/v1/workflow-runs/{completed_workflow.id}/results",
            headers=headers,
        )
        assert workflow_results.status_code == 200, workflow_results.text
        assert workflow_results.json()["workflow_name"] == "remote-smoke"
        assert workflow_results.json()["results"][0]["status"] == "passed"
        junit = client.get(
            f"/api/v1/workflow-runs/{completed_workflow.id}/results/junit",
            headers=headers,
        )
        assert junit.status_code == 200
        assert 'tests="1"' in junit.text
        terminal_cancel = client.post(
            f"/api/v1/workflow-runs/{completed_workflow.id}/cancel",
            headers=headers,
            json={"owner": "operation-owner"},
        )
        assert terminal_cancel.status_code == 409
        assert terminal_cancel.json()["error"]["code"] == "OPERATION_NOT_CANCELLABLE"

        invalid_filter = client.get(
            "/api/v1/artifacts",
            headers=headers,
            params={"owner_type": "operation"},
        )
        assert invalid_filter.status_code == 422
        missing = client.get(f"/api/v1/artifacts/{uuid4()}", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"

        issued = client.post(
            f"/api/v1/artifacts/{artifact['id']}/transfers",
            headers=headers,
            json={"agent_id": str(agent_id)},
        )
        assert issued.status_code == 201, issued.text
        body = issued.json()
        transfer_id = body["transfer"]["id"]
        transfer_token = body["token"]

        legacy_path_download = AsyncMock(
            side_effect=AssertionError("HTTP transfer downloads must use storage-neutral streams")
        )
        monkeypatch.setattr(runtime.artifacts, "download_path", legacy_path_download)

        # Capabilities stay out of URLs and access logs; transfers accept only
        # the Authorization header.
        query_token = client.get(
            f"/api/v1/artifact-transfers/{transfer_id}/content",
            params={"token": transfer_token},
        )
        assert query_token.status_code == 401

        downloaded = client.get(
            f"/api/v1/artifact-transfers/{transfer_id}/content",
            headers={"Authorization": f"Bearer {transfer_token}"},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == content
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert transfer_id in downloaded.headers["content-disposition"]
        legacy_path_download.assert_not_awaited()

        wrong = client.get(
            f"/api/v1/artifact-transfers/{transfer_id}/content",
            headers={"Authorization": "Bearer lpt_" + "x" * 43},
        )
        assert wrong.status_code == 400
