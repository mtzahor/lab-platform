from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent_protocol import CommandRequestEnvelope
from lab_platform.agent_runtime.command_journal import command_fingerprint
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiMaintenanceResult,
    DistributedCiSessionService,
)
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.models import (
    AgentConnectionRecord,
    AgentRecord,
    AgentStatus,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactTransferDirection,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
)
from lab_platform.persistence import SCHEMA_VERSION
from lab_platform.persistence.distributed import SQLiteAgentConnectionRepository
from starlette.websockets import WebSocketState

from tests.support.control_plane import make_control_plane_runtime
from tests.support.control_plane import make_control_plane_runtime as _runtime


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


def _config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "port": 8443,
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "agent_gateway": {"monitor_interval_seconds": 3_600},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "development": {"allow_insecure_agent_transport": True},
        }
    )


async def _seed_terminal_workflow(
    runtime: ControlPlaneRuntime,
    *,
    number: int,
    lifecycle: str,
    command_terminal: bool,
    release_after: bool | None = None,
) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id=UUID(int=number),
        slug=f"agent-{number}",
        name=f"Agent {number}",
        status=AgentStatus.ONLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=now - timedelta(days=1),
        last_connected_at=now,
        last_seen_at=now,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-{number}",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=f"bench-{number}",
        name=f"Bench {number}",
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"reset"}),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    with runtime.database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, location, labels_json, "
            "registered_at, last_connected_at, last_seen_at, disconnected_at, "
            "certificate_fingerprint, enrollment_status, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?, NULL, NULL, ?, NULL)",
            (
                str(agent.id),
                agent.slug,
                agent.name,
                agent.status.value,
                agent.version,
                agent.protocol_version,
                agent.registered_at.isoformat(),
                now.isoformat(),
                now.isoformat(),
                agent.enrollment_status.value,
            ),
        )
        connection.execute(
            "INSERT INTO global_benches "
            "(id, agent_id, agent_slug, local_bench_id, name, backend_id, kind, target_type, "
            "status, health, capabilities_json, labels_json, firmware_version, last_seen_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, '{}', NULL, "
            "?, ?, ?)",
            (
                bench.id,
                str(agent.id),
                agent.slug,
                bench.local_bench_id,
                bench.name,
                bench.backend_id,
                bench.kind.value,
                bench.status.value,
                bench.health.value,
                json.dumps(sorted(bench.capabilities)),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )

    reservation = Reservation(
        id=UUID(int=10_000 + number),
        bench_id=bench.id,
        owner=f"owner-{number}",
        created_at=now,
        requested_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        status=ReservationStatus.SCHEDULED,
        source=ReservationSource.API,
        metadata={"workload": "workflow", "reservation_lifecycle": lifecycle},
        idempotency_key=f"reservation-{number}",
    )
    pending = await runtime.reservation_repository.grant_if_eligible(
        ReservationGrantRequest(
            reservation=reservation,
            agent_id=agent.id,
            lease_valid_until=now + timedelta(minutes=30),
        ),
        mutation_key=f"grant-{number}",
        request_fingerprint=f"{number:x}".rjust(64, "0"),
        expected_agent_status=AgentStatus.ONLINE,
        expected_bench_status=GlobalBenchStatus.ONLINE,
    )
    assert pending is not None
    active = CoordinatedReservationLease(
        reservation=reservation.model_copy(
            update={"status": ReservationStatus.ACTIVE, "activated_at": now}
        ),
        lease=pending.record.lease,
        state=ReservationLeaseState.ACTIVE,
        revision=2,
    )
    persisted = await runtime.reservation_repository.replace_if_current(
        active,
        expected_revision=1,
        mutation_key=f"activate-{number}",
        request_fingerprint=f"{number + 100:x}".rjust(64, "0"),
    )
    assert persisted is not None

    operation_id = UUID(int=30_000 + number)
    command_status = (
        RemoteCommandStatus.SUCCEEDED if command_terminal else RemoteCommandStatus.DISPATCHED
    )
    operation_status = (
        DistributedOperationStatus.DISPATCHED
        if command_terminal
        else DistributedOperationStatus.SUCCEEDED
    )
    completed_at = now + timedelta(seconds=4)
    command = RemoteCommand(
        id=UUID(int=20_000 + number),
        agent_id=agent.id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload=(
            {}
            if release_after is None
            else {
                "reservation_lifecycle": {
                    "management": "caller",
                    "release_after": release_after,
                }
            }
        ),
        status=command_status,
        created_at=now,
        dispatched_at=now + timedelta(seconds=1),
        completed_at=completed_at if command_terminal else None,
        expires_at=now + timedelta(hours=1),
        idempotency_key=f"command-{number}",
        operation_id=operation_id,
        reservation_id=reservation.id,
        lease_version=active.lease.lease_version,
    )
    operation = DistributedOperation(
        id=operation_id,
        remote_command_id=command.id,
        agent_id=agent.id,
        bench_id=bench.id,
        reservation_id=reservation.id,
        operation_type="RUN_WORKFLOW",
        status=operation_status,
        created_at=now,
        dispatched_at=now + timedelta(seconds=1),
        completed_at=completed_at if not command_terminal else None,
    )
    await runtime.command_repository.create_bundle(command, operation)
    return active


def test_runtime_monitor_releases_persisted_workflow_after_restart_only_when_managed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        initial = ControlPlaneRuntime(config)
        initial.database.initialize()
        managed = await _seed_terminal_workflow(
            initial,
            number=1,
            lifecycle="workflow",
            command_terminal=False,
        )
        caller_managed = await _seed_terminal_workflow(
            initial,
            number=2,
            lifecycle="caller",
            command_terminal=True,
        )
        caller_release_after = await _seed_terminal_workflow(
            initial,
            number=3,
            lifecycle="caller",
            command_terminal=False,
            release_after=True,
        )
        caller_retain_override = await _seed_terminal_workflow(
            initial,
            number=4,
            lifecycle="workflow",
            command_terminal=True,
            release_after=False,
        )
        initial.database.close()

        restarted = ControlPlaneRuntime(config)
        restarted.database.initialize()
        await restarted.monitor_once()
        released = await restarted.reservations.get(managed.reservation.id)
        retained = await restarted.reservations.get(caller_managed.reservation.id)
        released_caller = await restarted.reservations.get(caller_release_after.reservation.id)
        retained_override = await restarted.reservations.get(caller_retain_override.reservation.id)
        assert released.state is ReservationLeaseState.RELEASED
        assert released.lease.lease_version == managed.lease.lease_version
        assert retained.state is ReservationLeaseState.ACTIVE
        assert released_caller.state is ReservationLeaseState.RELEASED
        assert retained_override.state is ReservationLeaseState.ACTIVE

        await restarted.monitor_once()
        with restarted.database.transaction() as connection:
            cleanup_mutations = connection.execute(
                "SELECT COUNT(*) AS count FROM reservation_lease_mutations "
                "WHERE mutation_key LIKE 'workflow-terminal-release:%'"
            ).fetchone()
        assert cleanup_mutations is not None
        assert cleanup_mutations["count"] == 2
        restarted.database.close()

    asyncio.run(scenario())


def _runtime_artifact_capability_security(tmp_path: Path) -> ControlPlaneRuntime:
    return make_control_plane_runtime(
        tmp_path,
        overrides={
            "artifacts": {"directory": tmp_path / "artifacts", "transfer_token_ttl_seconds": 300}
        },
    )


async def _attach(runtime: ControlPlaneRuntime, agent_id: UUID) -> Any:
    connection_id = uuid4()
    boot_id = uuid4()
    connected_at = datetime.now(UTC)
    await SQLiteAgentConnectionRepository(runtime.database).open(
        AgentConnectionRecord(
            id=connection_id,
            agent_id=agent_id,
            boot_id=boot_id,
            protocol_version="1.0",
            connected_at=connected_at,
            last_heartbeat_at=connected_at,
        )
    )
    websocket = SimpleNamespace(
        application_state=WebSocketState.CONNECTED,
        close=AsyncMock(),
    )
    return await runtime.hub.attach(
        agent_id=agent_id,
        connection_id=connection_id,
        boot_id=boot_id,
        websocket=cast(Any, websocket),
    )


def _database_dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


def test_download_capabilities_are_transient_and_reissued_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        runtime = _runtime_artifact_capability_security(tmp_path)
        await runtime.start()
        enrollment = await runtime.enrollment.issue_token(
            name="artifact-command-agent",
            expires_at=now + timedelta(minutes=30),
            allow_internal_authorisation=True,
        )
        enrolled = await runtime.enrollment.enroll(
            plaintext_token=enrollment.plaintext.get_secret_value(),
            request_id=uuid4(),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        agent = enrolled.agent.model_copy(
            update={
                "status": AgentStatus.ONLINE,
                "last_connected_at": now,
                "last_seen_at": now,
            }
        )
        bench = GlobalBenchRecord(
            id=f"{agent.slug}/bench-01",
            agent_id=agent.id,
            agent_slug=agent.slug,
            local_bench_id="bench-01",
            name="Capability security bench",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            capabilities=frozenset({"firmware"}),
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        await runtime.inventory_repository.reconcile_agent_snapshot(
            enrolled.agent,
            (bench,),
            observed_at=now,
        )
        monkeypatch.setattr(runtime.directory, "get_agent", AsyncMock(return_value=agent))
        monkeypatch.setattr(runtime.directory, "get_bench", AsyncMock(return_value=bench))

        content = b"signed-firmware-image"
        artifact = await runtime.platform_artifacts.store_bytes(
            content,
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="firmware.bin",
            artifact_type="firmware",
        )
        durable_descriptor = await runtime.workflow_artifacts.issue_download(
            agent_id=agent.id,
            input_name="firmware",
            artifact_id=artifact.id,
            target_path=f"artifacts/{artifact.id}",
            idempotency_key="stage-firmware",
            allow_internal_authorisation=True,
        )
        assert await runtime.artifact_transfers.list(agent_id=agent.id, limit=10) == []
        reservation = ReservationLease(
            reservation_id=uuid4(),
            agent_id=agent.id,
            bench_id=bench.id,
            owner="ci/security-test",
            valid_from=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=20),
            lease_version=1,
        )
        with pytest.raises(ValueError, match="cannot be persisted"):
            await runtime.commands.create(
                agent_id=agent.id,
                bench_id=bench.id,
                command_type=RemoteCommandType.FLASH,
                payload={
                    "artifact": {
                        **durable_descriptor.as_payload(),
                        "transfer_token": "lpt_plaintext_must_never_reach_sqlite_1234567890",
                    }
                },
                expires_at=now + timedelta(minutes=20),
                idempotency_key="reject-persisted-capability",
                reservation_lease=reservation,
                dispatch=False,
            )
        durable_payload = {
            "definition": {
                "name": "artifact-security",
                "version": 1,
                "steps": [{"action": "wait", "seconds": 1}],
            },
            "inputs": {"firmware": {"artifact_id": str(artifact.id)}},
            "owner": "ci/security-test",
            "artifact_transfers": [durable_descriptor.as_payload()],
            "reservation_lease": reservation.model_dump(mode="json"),
        }
        command = RemoteCommand(
            agent_id=agent.id,
            bench_id=bench.id,
            command_type=RemoteCommandType.RUN_WORKFLOW,
            payload=durable_payload,
            created_at=now,
            expires_at=now + timedelta(minutes=20),
            idempotency_key="artifact-capability-security",
            reservation_id=reservation.reservation_id,
            lease_version=reservation.lease_version,
        )
        await runtime.command_repository.create_bundle(command, None)
        first_session = await _attach(runtime, agent.id)

        first_command, _ = await runtime.commands.dispatch(
            command.id,
            reservation_lease=reservation,
        )
        assert first_command.status is RemoteCommandStatus.DISPATCHED
        first_envelope = cast(CommandRequestEnvelope, first_session.outgoing.get_nowait())
        first_transfer = first_envelope.payload.command.payload["artifact_transfers"][0]
        first_token = cast(str, first_transfer["transfer_token"])
        assert first_token.startswith("lpt_")
        assert first_token in first_envelope.model_dump_json()
        first_authorized = await runtime.artifacts.authorize(
            UUID(cast(str, first_transfer["transfer_id"])),
            first_token,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            agent_id=agent.id,
        )
        assert first_authorized.artifact_id == artifact.id
        assert len(await runtime.artifact_transfers.list(agent_id=agent.id, limit=10)) == 1

        with runtime.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM remote_commands WHERE id = ?",
                (str(command.id),),
            ).fetchone()
            assert row is not None
            persisted_payload = cast(str, row["payload_json"])
        assert first_token not in persisted_payload
        assert "transfer_token" not in persisted_payload
        assert "download_url" not in persisted_payload
        assert first_token not in _database_dump(tmp_path / "control-plane.db")

        assert await runtime.commands.mark_unknown(agent.id) == 1
        first_fingerprint = command_fingerprint(first_envelope.payload)
        await runtime.stop()

        restarted = _runtime_artifact_capability_security(tmp_path)
        await restarted.start()
        monkeypatch.setattr(restarted.directory, "get_agent", AsyncMock(return_value=agent))
        monkeypatch.setattr(restarted.directory, "get_bench", AsyncMock(return_value=bench))
        second_session = await _attach(restarted, agent.id)
        replayed, _ = await restarted.commands.dispatch(
            command.id,
            reservation_lease=reservation,
        )
        assert replayed.status is RemoteCommandStatus.UNKNOWN
        second_envelope = cast(CommandRequestEnvelope, second_session.outgoing.get_nowait())
        assert second_envelope.payload.command.status is RemoteCommandStatus.DISPATCHED
        second_transfer = second_envelope.payload.command.payload["artifact_transfers"][0]
        second_token = cast(str, second_transfer["transfer_token"])
        assert second_token.startswith("lpt_")
        assert second_token != first_token
        assert command_fingerprint(second_envelope.payload) == first_fingerprint
        second_authorized = await restarted.artifacts.authorize(
            UUID(cast(str, second_transfer["transfer_id"])),
            second_token,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            agent_id=agent.id,
        )
        assert second_authorized.artifact_id == artifact.id
        await restarted.stop()

        dump = _database_dump(tmp_path / "control-plane.db")
        assert first_token not in dump
        assert second_token not in dump
        for database_file in tmp_path.glob("control-plane.db*"):
            data = database_file.read_bytes()
            assert first_token.encode() not in data
            assert second_token.encode() not in data

    asyncio.run(scenario())


def _bootstrap_admin_control_plane_ci_api(client: TestClient) -> dict[str, str]:
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
        headers = _bootstrap_admin_control_plane_ci_api(client)
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
        headers = _bootstrap_admin_control_plane_ci_api(client)
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
