from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import lab_platform.control_plane.api as control_plane_api
import lab_platform.control_plane.dashboard_api as dashboard_api_module
import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane.dashboard_api import (
    OperationSummary,
    OverviewCounts,
    OverviewResponse,
    ReservationActionPermissions,
    _last_event_sequence,
    _refresh_sse_actor,
    _resource_event_payload,
    _resource_views,
    _sse_frame,
    reservation_action_permissions,
)
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.models import (
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ApiToken,
    ApiTokenScope,
    AuthenticationContext,
    CiProvider,
    CiSession,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Principal,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationStatus,
    WorkflowDefinition,
)
from starlette.requests import Request


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


def _bootstrap(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/tokens",
        json={
            "name": "phase-7-admin",
            "owner": "dashboard-operator",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _stage_bench(
    runtime: ControlPlaneRuntime,
    agent_id: UUID,
) -> GlobalBenchRecord:
    agent = await runtime.presence.get_agent(agent_id)
    now = datetime.now(UTC)
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-01",
        organisation_id=agent.organisation_id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="bench-01",
        name="Dashboard bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        target_type="esp32",
        status=GlobalBenchStatus.OFFLINE,
        health=HealthStatus.UNHEALTHY,
        capabilities=frozenset({"probe", "reset", "serial", "firmware"}),
        labels={"rack": "test"},
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        agent,
        (bench,),
        observed_at=now,
    )
    return bench


async def _stage_unknown_workflow(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
) -> DistributedOperation:
    created_at = datetime.now(UTC)
    dispatched_at = created_at + timedelta(milliseconds=1)
    command = RemoteCommand(
        organisation_id=bench.organisation_id,
        agent_id=bench.agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={"owner": "dashboard-operator"},
        status=RemoteCommandStatus.UNKNOWN,
        created_at=created_at,
        dispatched_at=dispatched_at,
        expires_at=created_at + timedelta(minutes=5),
        idempotency_key="phase7-dashboard-workflow",
    )
    operation = DistributedOperation(
        organisation_id=bench.organisation_id,
        remote_command_id=command.id,
        agent_id=bench.agent_id,
        bench_id=bench.id,
        operation_type=RemoteCommandType.RUN_WORKFLOW.value,
        status=DistributedOperationStatus.UNKNOWN,
        progress=42,
        message="Agent connection interrupted",
        result={
            "workflow_run": {"workflow_name": "dashboard-smoke"},
            "line_offset": 2_000,
            "line_count": 2_002,
            "truncated": True,
            "lines": [
                {"timestamp": created_at.isoformat(), "text": "booting"},
                {"timestamp": dispatched_at.isoformat(), "text": "READY"},
            ],
        },
        created_at=created_at,
        dispatched_at=dispatched_at,
        last_agent_update_at=dispatched_at,
    )
    command = command.model_copy(update={"operation_id": operation.id})
    _, persisted = await runtime.command_repository.create_bundle(command, operation)
    assert persisted is not None
    local_artifact_id = uuid4()
    remote_artifact = RemoteArtifactMetadata(
        id=uuid4(),
        agent_id=command.agent_id,
        local_artifact_id=local_artifact_id,
        command_id=command.id,
        operation_id=persisted.id,
        name="serial.log",
        artifact_type="serial_log",
        content_type="text/plain; charset=utf-8",
        size_bytes=128,
        sha256="a" * 64,
        created_at=dispatched_at,
        uploaded_at=dispatched_at,
    )
    await runtime.remote_artifacts.create(remote_artifact)
    return persisted


async def _stage_pending_workflow(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
) -> tuple[DistributedOperation, WorkflowDefinition]:
    created_at = datetime.now(UTC)
    definition = WorkflowDefinition.model_validate(
        {
            "organisation_id": bench.organisation_id,
            "name": "dashboard-pending",
            "version": 7,
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        }
    )
    command = RemoteCommand(
        organisation_id=bench.organisation_id,
        agent_id=bench.agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={"definition": definition.model_dump(mode="json"), "inputs": {}},
        expires_at=created_at + timedelta(minutes=5),
        idempotency_key="phase7-dashboard-pending-workflow",
        created_at=created_at,
    )
    operation = DistributedOperation(
        organisation_id=bench.organisation_id,
        remote_command_id=command.id,
        agent_id=bench.agent_id,
        bench_id=bench.id,
        operation_type=RemoteCommandType.RUN_WORKFLOW.value,
        created_at=created_at,
    )
    command = command.model_copy(update={"operation_id": operation.id})
    _, persisted = await runtime.command_repository.create_bundle(command, operation)
    assert persisted is not None
    return persisted, definition


async def _stage_ci_sessions(runtime: ControlPlaneRuntime) -> None:
    now = datetime.now(UTC)
    for status in (CiSessionStatus.RUNNING, CiSessionStatus.SUCCEEDED):
        await runtime.ci_repository.create(
            CiSession(
                provider=CiProvider.LOCAL,
                external_run_id=f"dashboard-{status.value}",
                requested_by="dashboard-operator",
                status=status,
                created_at=now,
            )
        )


async def _stage_agent_bench_timeline(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
) -> None:
    now = datetime.now(UTC)
    for event in (
        AgentTimelineRecord(
            agent_id=bench.agent_id,
            timestamp=now,
            event_type="BENCH_HEALTH_CHANGED",
            severity=AgentTimelineSeverity.WARNING,
            message="Bench health changed to warning (degraded).",
            metadata={
                "bench_id": bench.id,
                "connectivity": "degraded",
                "health": "warning",
            },
        ),
        AgentTimelineRecord(
            agent_id=bench.agent_id,
            timestamp=now + timedelta(milliseconds=1),
            event_type="AGENT_DISCONNECTED",
            severity=AgentTimelineSeverity.WARNING,
            message="Agent connection closed.",
        ),
        AgentTimelineRecord(
            agent_id=bench.agent_id,
            timestamp=now + timedelta(milliseconds=2),
            event_type="BENCH_HEALTH_CHANGED",
            severity=AgentTimelineSeverity.ERROR,
            message="Another bench became unhealthy.",
            metadata={"bench_id": f"{bench.agent_slug}/other-bench"},
        ),
    ):
        await runtime.timeline.append(event)


def test_dashboard_overview_composite_benches_queue_and_live_payloads(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        issued = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "dashboard-agent", "expires_in_seconds": 300},
        )
        assert issued.status_code == 201, issued.text
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": issued.json()["token"],
                "agent_version": "0.8.0-alpha",
                "protocol_version": "1.0",
                "location": "test-lab",
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        agent_payload = cast(dict[str, object], enrolled.json()["agent"])
        agent_id = UUID(cast(str, agent_payload["id"]))
        assert client.portal is not None
        bench = client.portal.call(_stage_bench, runtime, agent_id)
        operation = client.portal.call(_stage_unknown_workflow, runtime, bench)
        client.portal.call(_stage_ci_sessions, runtime)
        client.portal.call(_stage_agent_bench_timeline, runtime, bench)

        benches = client.get("/api/v1/benches", headers=headers)
        assert benches.status_code == 200, benches.text
        row = benches.json()["items"][0]
        assert row["id"] == bench.id
        assert row["availability"] == "OFFLINE"
        assert row["agent"]["name"]
        assert row["permissions"]["reserve"] is True
        assert row["permissions"]["release"] is False
        assert row["permissions"]["extend"] is False
        assert row["permissions"]["probe"] is True
        assert row["permissions"]["read_serial"] is True
        assert row["active_operation"]["id"] == str(operation.id)
        assert "result" not in row["active_operation"]

        queue_url = f"/api/v1/benches/{bench.id}/queue"
        queued = client.post(
            queue_url,
            headers=headers,
            json={
                "duration_seconds": 900,
                "idempotency_key": "dashboard-queue-once",
                "description": "Interactive debugging",
            },
        )
        assert queued.status_code == 201, queued.text
        queue_entry = queued.json()
        assert queue_entry["position"] == 1
        assert queue_entry["requester"] == "dashboard-operator"
        repeated = client.post(
            queue_url,
            headers=headers,
            json={
                "duration_seconds": 900,
                "idempotency_key": "dashboard-queue-once",
                "description": "Interactive debugging",
            },
        )
        assert repeated.status_code == 201
        assert repeated.json()["id"] == queue_entry["id"]

        other_token = client.post(
            "/api/v1/tokens",
            headers=headers,
            json={
                "name": "phase-7-other",
                "owner": "other-operator",
                "scopes": [
                    ApiTokenScope.BENCHES_READ.value,
                    ApiTokenScope.RESERVATIONS_WRITE.value,
                ],
            },
        )
        assert other_token.status_code == 201, other_token.text
        other_headers = {"Authorization": f"Bearer {other_token.json()['token']}"}
        other_entry = client.post(
            queue_url,
            headers=other_headers,
            json={
                "duration_seconds": 600,
                "idempotency_key": "dashboard-other-queue",
            },
        )
        assert other_entry.status_code == 201, other_entry.text

        queue = client.get(queue_url, headers=headers)
        assert queue.status_code == 200, queue.text
        assert queue.json()["total"] == 2
        other_row = next(
            item for item in queue.json()["items"] if item["id"] == other_entry.json()["id"]
        )
        assert other_row["requester"] == "other-operator"
        assert other_row["cancellable"] is True
        deleted_other = client.delete(
            f"/api/v1/queue/{other_entry.json()['id']}",
            headers=headers,
        )
        assert deleted_other.status_code == 204, deleted_other.text

        overview = client.get("/api/v1/overview", headers=headers)
        assert overview.status_code == 200, overview.text
        counts = overview.json()["counts"]
        assert counts["benches_total"] == 1
        assert counts["benches_offline"] == 1
        assert counts["active_operations"] == 1
        assert counts["queued_reservations"] == 1
        assert counts["active_ci_sessions"] == 1
        assert overview.json()["degraded_benches"][0]["id"] == bench.id

        runs = client.get("/api/v1/workflow-runs", headers=headers)
        assert runs.status_code == 200, runs.text
        assert runs.json()["items"][0]["status"] == "unknown"
        assert runs.json()["items"][0]["operation_status"] == "UNKNOWN"

        serial = client.get(
            f"/api/v1/operations/{operation.id}/serial",
            headers=headers,
            params={"cursor": 2_001, "limit": 1},
        )
        assert serial.status_code == 200, serial.text
        assert serial.json()["connection_state"] == "RECONNECTING"
        assert serial.json()["lines"][0]["text"] == "READY"
        assert serial.json()["lines"][0]["cursor"] == 2_001
        assert serial.json()["next_cursor"] == 2_002
        assert serial.json()["total"] == 2_002
        assert serial.json()["retained_from"] == 2_000
        assert serial.json()["tail_truncated"] is True
        assert serial.json()["truncated"] is False
        assert serial.json()["artifact_truncated"] is True
        assert serial.json()["artifact"]["ready"] is True
        assert serial.json()["artifact"]["local_artifact_id"] != serial.json()["artifact"]["id"]
        assert serial.json()["artifact"]["download_url"].endswith("/content")

        missed_serial = client.get(
            f"/api/v1/operations/{operation.id}/serial",
            headers=headers,
            params={"cursor": 0, "limit": 1},
        )
        assert missed_serial.status_code == 200, missed_serial.text
        assert missed_serial.json()["truncated"] is True
        assert missed_serial.json()["lines"][0]["cursor"] == 2_000

        timeline = client.get(f"/api/v1/benches/{bench.id}/timeline", headers=headers)
        assert timeline.status_code == 200, timeline.text
        assert {item["event_type"] for item in timeline.json()["items"]} >= {
            "AGENT_DISCONNECTED",
            "BENCH_HEALTH_CHANGED",
            "BENCH_STATUS",
            "OPERATION_STATUS",
        }
        timeline_titles = {item["title"] for item in timeline.json()["items"]}
        assert "Bench health changed to warning (degraded)." in timeline_titles
        assert "Agent connection closed." in timeline_titles
        assert "Another bench became unhealthy." not in timeline_titles

        events = client.get(
            "/api/v1/events",
            headers={**headers, "Last-Event-ID": "informational-only"},
            params={"once": True},
        )
        assert events.status_code == 200, events.text
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "event: overview.snapshot" in events.text
        assert "last_event_id" not in events.text

        deleted = client.delete(
            f"/api/v1/queue/{queue_entry['id']}",
            headers=headers,
        )
        assert deleted.status_code == 204, deleted.text
        assert client.get(queue_url, headers=headers).json()["total"] == 0


def test_sse_stream_emits_scoped_serial_event_and_advances_reconnect_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    operation = OperationSummary(
        id=uuid4(),
        operation_type=RemoteCommandType.READ_SERIAL.value,
        agent_id=uuid4(),
        bench_id="agent/bench-01",
        status=DistributedOperationStatus.RUNNING,
        created_at=now,
    )
    first = OverviewResponse(
        generated_at=now,
        counts=OverviewCounts(active_operations=1),
        active_operations=[operation],
        failed_workflow_runs=[],
        degraded_benches=[],
        active_reservations=[],
        upcoming_reservations=[],
        recent_agent_disconnects=[],
        active_ci_sessions=[],
    )
    second = first.model_copy(
        update={
            "generated_at": now + timedelta(milliseconds=1),
            "counts": OverviewCounts(active_operations=0),
            "active_operations": [],
        }
    )
    calls = 0

    async def changing_overview(
        _runtime: ControlPlaneRuntime,
        _actor: object,
    ) -> OverviewResponse:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    async def fast_pause(_seconds: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(dashboard_api_module, "build_overview", changing_overview)
    monkeypatch.setattr(dashboard_api_module, "_sse_pause", fast_pause)
    monkeypatch.setattr(dashboard_api_module, "_MAX_SSE_CONNECTION_SECONDS", 0.1)

    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        response = client.get(
            "/api/v1/events",
            headers={**headers, "Last-Event-ID": "1750000000000-7"},
        )

    assert response.status_code == 200, response.text
    assert "event: overview.snapshot" in response.text
    assert "event: overview.updated" in response.text
    assert "event: operation.updated" in response.text
    assert "event: serial.updated" in response.text
    sequences = [
        int(line.rpartition("-")[2])
        for line in response.text.splitlines()
        if line.startswith("id: ")
    ]
    assert sequences[0] == 8
    assert sequences == sorted(set(sequences))


def test_sse_revalidates_and_closes_a_revoked_credential(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        issued = client.post(
            "/api/v1/tokens",
            json={
                "name": "phase-7-live-stream",
                "owner": "dashboard-operator",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert issued.status_code == 201, issued.text
        plaintext = issued.json()["token"]
        assert client.portal is not None
        actor = client.portal.call(
            runtime.token_service.authenticate,
            plaintext,
            (ApiTokenScope.BENCHES_READ,),
        )
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/events",
                "raw_path": b"/api/v1/events",
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {plaintext}".encode())],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8443),
                "root_path": "",
            }
        )
        dependency = control_plane_api._require_scopes(
            runtime,
            ApiTokenScope.BENCHES_READ,
            phase6_permissions=(),
        )

        current, refreshed = client.portal.call(
            _refresh_sse_actor,
            request,
            actor,
            dependency,
        )
        assert current is True
        assert isinstance(refreshed, ApiToken)
        assert refreshed.id == actor.id

        client.portal.call(runtime.token_service.revoke, actor.id)
        current, refreshed = client.portal.call(
            _refresh_sse_actor,
            request,
            actor,
            dependency,
        )
        assert current is False
        assert refreshed is None


def test_sse_resource_events_are_scoped_bounded_and_reconnect_monotonic() -> None:
    previous = _resource_views(
        {
            "counts": {
                "agents_online": 2,
                "benches_available": 1,
                "benches_offline": 0,
                "queued_reservations": 0,
            },
            "active_operations": [
                {
                    "id": "serial-1",
                    "operation_type": RemoteCommandType.READ_SERIAL.value,
                }
            ],
            "failed_workflow_runs": [],
            "degraded_benches": [],
            "active_reservations": [],
            "upcoming_reservations": [],
            "recent_agent_disconnects": [],
            "active_ci_sessions": [],
        }
    )
    current = _resource_views(
        {
            "counts": {
                "agents_online": 1,
                "benches_available": 0,
                "benches_offline": 1,
                "queued_reservations": 0,
            },
            "active_operations": [],
            "failed_workflow_runs": [],
            "degraded_benches": [{"id": "bench-1"}],
            "active_reservations": [],
            "upcoming_reservations": [],
            "recent_agent_disconnects": [{"id": "agent-1"}],
            "active_ci_sessions": [],
        }
    )

    assert set(current) == {
        "agent.updated",
        "bench.updated",
        "ci_session.updated",
        "operation.updated",
        "reservation.updated",
        "serial.updated",
        "workflow.updated",
    }
    serial_payload = _resource_event_payload(
        "serial.updated",
        previous["serial.updated"],
        current["serial.updated"],
    )
    assert serial_payload == {
        "resource_type": "SERIAL",
        "summary": "A serial capture finished.",
        "status": "SUCCEEDED",
        "notify": True,
        "data": {"previous_count": 1, "current_count": 0},
    }
    agent_payload = _resource_event_payload(
        "agent.updated",
        previous["agent.updated"],
        current["agent.updated"],
    )
    assert agent_payload["notify"] is True
    assert agent_payload["status"] == "WARNING"
    assert _last_event_sequence("1750000000000-41") == 41
    assert _last_event_sequence("malformed") == 0


def test_dashboard_routes_require_authentication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        for path in ("/api/v1/overview", "/api/v1/workflow-runs", "/api/v1/events"):
            response = client.get(path, params={"once": True})
            assert response.status_code == 401, response.text
            assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_workflow_detail_enriches_pending_operation_from_durable_command(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        issued = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "pending-workflow-agent", "expires_in_seconds": 300},
        )
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": issued.json()["token"],
                "agent_version": "0.8.0-alpha",
                "protocol_version": "1.0",
            },
        )
        agent_id = UUID(cast(str, enrolled.json()["agent"]["id"]))
        assert client.portal is not None
        bench = client.portal.call(_stage_bench, runtime, agent_id)
        operation, definition = client.portal.call(
            _stage_pending_workflow,
            runtime,
            bench,
        )

        response = client.get(
            f"/api/v1/workflow-runs/{operation.id}",
            headers=headers,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["workflow_name"] == definition.name
        assert payload["version"] == definition.version
        assert payload["steps"] == definition.model_dump(mode="json")["steps"]
        assert payload["status"] == "pending"
        assert payload["operation_status"] == "CREATED"
        assert payload["local_workflow_run_id"] is None


def test_future_reservation_api_lists_overviews_and_cancels_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_success = AsyncMock()
    monkeypatch.setattr(control_plane_api, "_audit_protected_success", audit_success)
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        issued = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "scheduled-agent", "expires_in_seconds": 300},
        )
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": issued.json()["token"],
                "agent_version": "0.8.0-alpha",
                "protocol_version": "1.0",
            },
        )
        agent_id = UUID(cast(str, enrolled.json()["agent"]["id"]))
        assert client.portal is not None
        bench = client.portal.call(_stage_bench, runtime, agent_id)
        starts_at = datetime.now(UTC) + timedelta(hours=1)

        created = client.post(
            "/api/v1/reservations",
            headers=headers,
            json={
                "bench_id": bench.id,
                "owner": "dashboard-operator",
                "starts_at": starts_at.isoformat(),
                "reservation_duration_seconds": 1_800,
                "description": "Scheduled hardware validation",
                "idempotency_key": "phase7-future-reservation",
            },
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        reservation_id = payload["reservation"]["id"]
        assert payload["state"] == "SCHEDULED"
        assert payload["lease"] is None
        assert payload["permissions"] == {
            "owned_by_caller": True,
            "release": False,
            "extend": False,
            "cancel": True,
            "administrator": True,
        }
        assert payload["reservation"]["metadata"]["description"] == (
            "Scheduled hardware validation"
        )

        listed = client.get("/api/v1/reservations", headers=headers)
        assert listed.status_code == 200, listed.text
        assert {item["reservation"]["id"] for item in listed.json()["items"]} == {reservation_id}
        assert listed.json()["items"][0]["permissions"]["cancel"] is True
        fetched = client.get(f"/api/v1/reservations/{reservation_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["state"] == "SCHEDULED"
        assert fetched.json()["permissions"] == payload["permissions"]

        overview = client.get("/api/v1/overview", headers=headers)
        assert overview.status_code == 200, overview.text
        assert overview.json()["upcoming_reservations"] == [
            {
                "id": reservation_id,
                "bench_id": bench.id,
                "owner": "dashboard-operator",
                "starts_at": starts_at.isoformat().replace("+00:00", "Z"),
                "ends_at": (starts_at + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                "description": "Scheduled hardware validation",
                "permissions": payload["permissions"],
            }
        ]

        cancelled = client.post(
            f"/api/v1/reservations/{reservation_id}/release",
            headers=headers,
            json={
                "owner": "dashboard-operator",
                "idempotency_key": "cancel-phase7-future",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "CANCELLED"
        assert cancelled.json()["lease"] is None
        assert any(
            call.args[2] == "BENCH_RELEASED"
            and call.kwargs["resource_id"] == bench.id
            and call.kwargs["metadata"] == {"reservation_id": reservation_id, "scheduled": True}
            for call in audit_success.await_args_list
        )
        assert client.get("/api/v1/overview", headers=headers).json()["upcoming_reservations"] == []

        second = client.post(
            "/api/v1/reservations",
            headers=headers,
            json={
                "bench_id": bench.id,
                "owner": "dashboard-operator",
                "starts_at": (starts_at + timedelta(hours=2)).isoformat(),
                "reservation_duration_seconds": 900,
                "idempotency_key": "phase7-admin-cancel-reservation",
            },
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["reservation"]["id"]
        revoked = client.post(
            f"/api/v1/reservations/{second_id}/revoke",
            headers=headers,
            json={"idempotency_key": "admin-cancel-phase7-future"},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["state"] == "CANCELLED"
        assert any(
            call.args[2] == "BENCH_RESERVATION_REVOKED"
            and call.kwargs["resource_id"] == bench.id
            and call.kwargs["metadata"] == {"reservation_id": second_id, "scheduled": True}
            for call in audit_success.await_args_list
        )


def test_sse_heartbeat_is_a_named_json_event_with_a_fresh_id() -> None:
    observed_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    first = _sse_frame("heartbeat", 4, observed_at=observed_at)
    second = _sse_frame("heartbeat", 5, observed_at=observed_at)

    assert "event: heartbeat\n" in first
    assert '"timestamp":"2026-08-20T12:00:00+00:00"' in first
    assert "id: 1787227200000-4\n" in first
    assert "id: 1787227200000-5\n" in second
    assert "last_event_id" not in first


def test_reservation_action_permissions_require_owner_or_bench_administrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    organisation_id = uuid4()
    principal_id = uuid4()
    agent_id = uuid4()
    actor = AuthenticationContext(
        principal=Principal(
            id=principal_id,
            type=PrincipalType.USER,
            organisation_id=organisation_id,
            display_name="Reservation Owner",
        )
    )
    now = datetime.now(UTC)

    def active_record(*, owner_id: UUID, owner: str) -> CoordinatedReservationLease:
        reservation_id = uuid4()
        reservation = Reservation(
            id=reservation_id,
            organisation_id=organisation_id,
            bench_id="simlab/ownership",
            owner=owner,
            owner_principal_id=owner_id,
            owner_principal_type=PrincipalType.USER.value,
            status=ReservationStatus.ACTIVE,
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        return CoordinatedReservationLease(
            reservation=reservation,
            lease=ReservationLease(
                reservation_id=reservation_id,
                agent_id=agent_id,
                bench_id=reservation.bench_id,
                owner=owner,
                valid_from=now,
                valid_until=now + timedelta(minutes=10),
                lease_version=1,
            ),
            state=ReservationLeaseState.ACTIVE,
            revision=1,
        )

    allowed = AsyncMock(
        side_effect=lambda _principal, permission, _resource, **_kwargs: (
            permission == "benches:reserve"
        )
    )
    monkeypatch.setattr(runtime.authorisation, "is_allowed", allowed)

    def presented(
        value: CoordinatedReservationLease | Reservation,
    ) -> ReservationActionPermissions:
        return asyncio.run(
            reservation_action_permissions(
                runtime,
                actor,
                value,
                parent_agent_id=agent_id,
            )
        )

    owned = presented(active_record(owner_id=principal_id, owner="Reservation Owner"))
    assert owned.model_dump() == {
        "owned_by_caller": True,
        "release": True,
        "extend": True,
        "cancel": False,
        "administrator": False,
    }

    # Matching display text must never substitute for the durable principal identity.
    other_record = active_record(owner_id=uuid4(), owner="Reservation Owner")
    denied = presented(other_record)
    assert denied.model_dump() == {
        "owned_by_caller": False,
        "release": False,
        "extend": False,
        "cancel": False,
        "administrator": False,
    }

    allowed.side_effect = lambda _principal, permission, _resource, **_kwargs: (
        permission == "benches:manage"
    )
    administrative = presented(other_record)
    assert administrative.model_dump() == {
        "owned_by_caller": False,
        "release": True,
        "extend": False,
        "cancel": False,
        "administrator": True,
    }
    scheduled = other_record.reservation.model_copy(update={"status": ReservationStatus.SCHEDULED})
    scheduled_permissions = presented(scheduled)
    assert scheduled_permissions.cancel is True
    assert scheduled_permissions.release is False
    assert scheduled_permissions.extend is False

    cross_tenant = scheduled.model_copy(update={"organisation_id": uuid4()})
    assert presented(cross_tenant).model_dump() == {
        "owned_by_caller": False,
        "release": False,
        "extend": False,
        "cancel": False,
        "administrator": False,
    }
