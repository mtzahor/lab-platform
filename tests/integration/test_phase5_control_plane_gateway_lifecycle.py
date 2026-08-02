from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent_protocol import MessageType
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane.gateway import WS_PROTOCOL_ERROR
from lab_platform.models import ApiTokenScope
from starlette.websockets import WebSocketDisconnect


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
            "name": "gateway-test",
            "owner": "test-suite",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _enroll(
    client: TestClient,
    admin_headers: dict[str, str],
) -> tuple[UUID, str, UUID]:
    token = client.post(
        "/api/v1/agents/enrollment-tokens",
        headers=admin_headers,
        json={
            "name": "gateway-agent",
            "expires_in_seconds": 300,
            "allowed_labels": {"environment": "test"},
        },
    )
    assert token.status_code == 201, token.text
    enrolled = client.post(
        "/api/v1/agents/enroll",
        json={
            "enrollment_token": token.json()["token"],
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.0",
            "location": "jerusalem",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    body = cast(dict[str, object], enrolled.json())
    agent = cast(dict[str, object], body["agent"])
    return UUID(str(agent["id"])), str(body["credential"]), UUID(str(token.json()["id"]))


def _agent_message(
    agent_id: UUID,
    message_type: MessageType,
    sequence_number: int,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "message_id": str(uuid4()),
        "message_type": message_type.value,
        "agent_id": str(agent_id),
        "sent_at": datetime.now(UTC).isoformat(),
        "correlation_id": None,
        "sequence_number": sequence_number,
        "payload": payload,
    }


def _wait_for(
    client: TestClient,
    path: str,
    headers: dict[str, str],
    predicate: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    for _ in range(100):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = cast(dict[str, object], response.json())
        if predicate(body):
            return body
        time.sleep(0.005)
    raise AssertionError(f"Timed out waiting for {path}")


def test_authenticated_gateway_lifecycle_inventory_admin_and_disconnect(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        admin = _bootstrap_admin(client)
        agent_id, credential, enrollment_token_id = _enroll(client, admin)
        boot_id = uuid4()

        assert client.get("/api/v1/version").json()["protocol_version"] == "1.0"
        assert client.get("/api/v1/health").json()["status"] == "healthy"
        assert (
            client.get(
                "/api/v1/agents",
                headers={"Authorization": "Bearer invalid"},
            ).status_code
            == 401
        )

        with client.websocket_connect(
            f"/api/v1/agent-gateway/{agent_id}",
            headers={"Authorization": f"Bearer {credential}"},
        ) as websocket:
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.AGENT_HELLO,
                    1,
                    {
                        "agent_version": "0.6.0-alpha",
                        "protocol_version": "1.0",
                        "agent_name": "gateway-agent",
                        "boot_id": str(boot_id),
                        "capabilities": ["remote_operations", "artifact_upload"],
                        "last_acknowledged_command_sequence": 0,
                    },
                )
            )
            welcome = websocket.receive_json()
            assert welcome["message_type"] == MessageType.WELCOME.value
            assert welcome["sequence_number"] == 1
            reconciliation = websocket.receive_json()
            assert reconciliation["message_type"] == MessageType.RECONCILIATION_REQUEST.value
            assert reconciliation["sequence_number"] == 2

            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.AGENT_HEARTBEAT,
                    2,
                    {
                        "agent_id": str(agent_id),
                        "boot_id": str(boot_id),
                        "uptime_seconds": 10,
                        "active_operations": 0,
                        "connected_benches": 1,
                        "degraded_benches": 0,
                        "event_buffer_size": 0,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            )
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.BENCH_SNAPSHOT,
                    3,
                    {
                        "boot_id": str(boot_id),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "benches": [
                            {
                                "local_bench_id": "bench-01",
                                "name": "Gateway bench",
                                "backend_id": "simlab",
                                "kind": "simulated",
                                "target_type": "esp32",
                                "connectivity": "online",
                                "health": "healthy",
                                "capabilities": ["firmware", "probe", "reset", "serial"],
                                "labels": {"board": "esp32"},
                            }
                        ],
                    },
                )
            )
            details = _wait_for(
                client,
                f"/api/v1/agents/{agent_id}",
                admin,
                lambda body: len(cast(list[object], body["benches"])) == 1,
            )
            assert details["status"] == "ONLINE"
            bench_id = cast(list[dict[str, object]], details["benches"])[0]["id"]

            filtered = client.get(
                "/api/v1/benches",
                headers=admin,
                params={
                    "location": "jerusalem",
                    "agent_label": "environment=test",
                    "label": "board=esp32",
                    "online": "true",
                },
            )
            assert filtered.status_code == 200, filtered.text
            assert [item["id"] for item in filtered.json()["items"]] == [bench_id]
            assert client.get(f"/api/v1/benches/{bench_id}", headers=admin).status_code == 200

            unfenced_reset = client.post(
                f"/api/v1/benches/{bench_id}/actions/reset",
                headers=admin,
                json={"owner": "test-suite"},
            )
            assert unfenced_reset.status_code == 409
            assert unfenced_reset.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"

            probe = client.post(
                f"/api/v1/benches/{bench_id}/actions/probe",
                headers=admin,
                json={"owner": "test-suite"},
            )
            assert probe.status_code == 202, probe.text
            operation_id = UUID(probe.json()["operation_id"])
            command_request = websocket.receive_json()
            assert command_request["message_type"] == MessageType.COMMAND_REQUEST.value
            command_id = UUID(command_request["payload"]["command"]["id"])
            assert command_request["payload"]["command"]["command_type"] == "PROBE"
            assert command_request["payload"]["reservation_lease"] is None

            accepted_at = datetime.now(UTC)
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.COMMAND_ACCEPTED,
                    4,
                    {
                        "command_id": str(command_id),
                        "accepted_at": accepted_at.isoformat(),
                        "journal_status": "ACCEPTED",
                        "local_operation_id": str(uuid4()),
                    },
                )
            )
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.OPERATION_STARTED,
                    5,
                    {
                        "command_id": str(command_id),
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "progress": 20,
                        "message": "probing",
                    },
                )
            )
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.OPERATION_SUCCEEDED,
                    6,
                    {
                        "command_id": str(command_id),
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "progress": 100,
                        "message": "healthy",
                        "result": {
                            "bench_id": "bench-01",
                            "status": "healthy",
                            "details": {"transport": "distributed"},
                        },
                    },
                )
            )
            completed = _wait_for(
                client,
                f"/api/v1/operations/{operation_id}",
                admin,
                lambda body: body["status"] == "SUCCEEDED",
            )
            assert completed["result"] == {
                "bench_id": "bench-01",
                "status": "healthy",
                "details": {"transport": "distributed"},
            }

            refreshed = client.post(
                f"/api/v1/agents/{agent_id}/actions/refresh-inventory",
                headers=admin,
            )
            assert refreshed.status_code == 202, refreshed.text
            refresh_message = websocket.receive_json()
            assert refresh_message["message_type"] == MessageType.INVENTORY_REFRESH_REQUEST.value

            assert client.portal is not None
            request_id = client.portal.call(runtime.request_reconciliation, agent_id)
            requested = websocket.receive_json()
            assert requested["message_type"] == MessageType.RECONCILIATION_REQUEST.value
            assert requested["correlation_id"] == str(request_id)

            drained = client.post(
                f"/api/v1/agents/{agent_id}/drain",
                headers=admin,
                json={"cancel_queued_work": True},
            )
            assert drained.status_code == 200, drained.text
            assert drained.json()["agent"]["status"] == "DRAINED"
            assert websocket.receive_json()["payload"]["drain"] is True

            undrained = client.post(f"/api/v1/agents/{agent_id}/undrain", headers=admin)
            assert undrained.status_code == 200, undrained.text
            assert undrained.json()["status"] == "ONLINE"
            assert websocket.receive_json()["payload"]["drain"] is False

            # End from the server side so TestClient observes the same fenced teardown
            # path as a protocol-invalid peer instead of cancelling the ASGI task.
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.AGENT_HELLO,
                    7,
                    {
                        "agent_version": "0.6.0-alpha",
                        "protocol_version": "1.0",
                        "agent_name": "gateway-agent",
                        "boot_id": str(boot_id),
                        "capabilities": [],
                        "last_acknowledged_command_sequence": 0,
                    },
                )
            )
            with pytest.raises(WebSocketDisconnect) as disconnected_by_server:
                websocket.receive_json()
            assert disconnected_by_server.value.code == WS_PROTOCOL_ERROR

        disconnected = _wait_for(
            client,
            f"/api/v1/agents/{agent_id}",
            admin,
            lambda body: body["status"] == "OFFLINE",
        )
        assert disconnected["connection"] is None
        timeline = client.get(f"/api/v1/agents/{agent_id}/timeline", headers=admin)
        assert timeline.status_code == 200
        assert {item["event_type"] for item in timeline.json()["items"]} >= {
            "AGENT_CONNECTED",
            "AGENT_DISCONNECTED",
            "AGENT_DRAIN_REQUESTED",
            "AGENT_UNDRAINED",
        }

        offline_refresh = client.post(
            f"/api/v1/agents/{agent_id}/actions/refresh-inventory",
            headers=admin,
        )
        assert offline_refresh.status_code == 409
        assert offline_refresh.json()["error"]["code"] == "AGENT_OFFLINE"

        tokens = client.get("/api/v1/agents/enrollment-tokens", headers=admin)
        assert tokens.status_code == 200
        assert [item["id"] for item in tokens.json()["items"]] == [str(enrollment_token_id)]
        revoked = client.delete(
            f"/api/v1/agents/enrollment-tokens/{enrollment_token_id}",
            headers=admin,
        )
        assert revoked.status_code == 204

        missing_bench = client.get("/api/v1/benches/missing/bench", headers=admin)
        assert missing_bench.status_code == 404
        missing_operation = client.get(f"/api/v1/operations/{uuid4()}", headers=admin)
        assert missing_operation.status_code == 404
        unknown_route = client.get("/api/v1/not-a-route")
        assert unknown_route.status_code == 404
        assert unknown_route.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        invalid_request = client.post("/api/v1/agents/enroll", json={})
        assert invalid_request.status_code == 422
        assert invalid_request.json()["error"]["code"] == "VALIDATION_ERROR"


def test_authenticated_gateway_rejects_non_hello_first_message(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        admin = _bootstrap_admin(client)
        agent_id, credential, _token_id = _enroll(client, admin)
        boot_id = uuid4()
        with client.websocket_connect(
            f"/api/v1/agent-gateway/{agent_id}",
            headers={"Authorization": f"Bearer {credential}"},
        ) as websocket:
            websocket.send_json(
                _agent_message(
                    agent_id,
                    MessageType.AGENT_HEARTBEAT,
                    1,
                    {
                        "agent_id": str(agent_id),
                        "boot_id": str(boot_id),
                        "uptime_seconds": 0,
                        "active_operations": 0,
                        "connected_benches": 0,
                        "degraded_benches": 0,
                        "event_buffer_size": 0,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
            )
            with pytest.raises(WebSocketDisconnect) as disconnected:
                websocket.receive_json()
            assert disconnected.value.code == WS_PROTOCOL_ERROR
