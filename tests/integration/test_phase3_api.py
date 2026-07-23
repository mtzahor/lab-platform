from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from httpx2 import Response
from lab_platform.agent import LabAgent, create_agent, create_app
from lab_platform.core import FakeClock
from lab_platform.simlab_adapter import SimLabBackend

JsonObject = dict[str, object]


def _write_phase3_config(root: Path, *, workflows: bool = False) -> None:
    workflow_settings = ""
    if workflows:
        definitions = root / "workflows"
        definitions.mkdir()
        (definitions / "quick-check.yaml").write_text(
            """name: quick-check
version: 1
description: Fast API integration workflow
requirements:
  capabilities: [probe]
steps:
  - action: probe
  - action: wait
    seconds: 0.001
""",
            encoding="utf-8",
        )
        (definitions / "cancellable.yaml").write_text(
            """name: cancellable
version: 1
requirements:
  capabilities: []
steps:
  - action: wait
    seconds: 30
""",
            encoding="utf-8",
        )
        workflow_settings = "workflows:\n  definitions_directory: ./workflows\n"

    (root / "agent.yaml").write_text(
        """agent:
  name: phase3-api-agent
  log_level: ERROR
plugins: []
backends:
  - id: virtual-alpha
    type: simlab
    config:
      benches: 2
      bench_prefix: alpha
      speed_multiplier: 1000
      flash_duration_seconds: 0.01
  - id: virtual-beta
    type: simlab
    config:
      benches: 2
      bench_prefix: beta
      speed_multiplier: 1000
      flash_duration_seconds: 0.01
reservations:
  default_duration_minutes: 1
  maximum_duration_minutes: 20
  expiry_grace_seconds: 0
  queue_enabled: true
  scheduled_protection_window_minutes: 0
scheduler:
  poll_interval_seconds: 3600
  automatic_assignment: false
artifacts:
  directory: ./artifacts
"""
        + workflow_settings,
        encoding="utf-8",
    )


@contextmanager
def _phase3_client(
    root: Path,
    *,
    workflows: bool = False,
    clock: FakeClock | None = None,
) -> Iterator[tuple[LabAgent, TestClient]]:
    _write_phase3_config(root, workflows=workflows)
    agent = create_agent(root)
    if clock is not None:
        # The public runtime owns these injected Clock ports. Replacing the
        # concrete UTC clock keeps API-level scheduling tests deterministic.
        agent.catalog._clock = clock
        agent.reservation_service._clock = clock
        agent.scheduling_service._clock = clock
    asyncio.run(agent.start())
    try:
        with TestClient(create_app(agent), raise_server_exceptions=False) as client:
            yield agent, client
    finally:
        asyncio.run(agent.shutdown())


def _json(response: Response) -> JsonObject:
    return cast(JsonObject, response.json())


def _items(response: Response) -> list[JsonObject]:
    payload = _json(response)
    return cast(list[JsonObject], payload["items"])


def _assert_error(response: Response, status_code: int, code: str) -> JsonObject:
    assert response.status_code == status_code
    payload = _json(response)
    error = cast(JsonObject, payload["error"])
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert isinstance(error["details"], dict)
    assert error["request_id"] == response.headers["x-request-id"]
    return error


def _wait_for_operation(client: TestClient, operation_id: str) -> JsonObject:
    for _ in range(100):
        operation = _json(client.get(f"/api/v1/operations/{operation_id}"))
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.002)
    raise AssertionError(f"operation {operation_id} did not finish")


def _wait_for_workflow_status(
    client: TestClient,
    workflow_run_id: str,
    statuses: set[str],
) -> JsonObject:
    for _ in range(100):
        run = _json(client.get(f"/api/v1/workflow-runs/{workflow_run_id}"))
        if cast(str, run["status"]) in statuses:
            return run
        time.sleep(0.002)
    raise AssertionError(f"workflow run {workflow_run_id} did not reach {sorted(statuses)}")


def test_enriched_bench_filters_and_two_simlab_backends_route_independently(
    tmp_path: Path,
) -> None:
    with _phase3_client(tmp_path) as (agent, client):
        beta = cast(SimLabBackend, agent.backend_registry.get_backend("virtual-beta"))
        beta.simulator.set_online("beta-02", False)

        benches = _items(client.get("/api/v1/benches"))
        by_id = {cast(str, item["id"]): item for item in benches}
        assert list(by_id) == ["alpha-01", "alpha-02", "beta-01", "beta-02"]
        assert by_id["alpha-01"]["backend_id"] == "virtual-alpha"
        assert by_id["beta-01"]["backend_id"] == "virtual-beta"
        assert by_id["alpha-01"]["target_type"] == "simlab"
        assert by_id["alpha-01"]["labels"] == {
            "board": "virtual",
            "location": "simulation",
            "purpose": "testing",
        }
        assert by_id["beta-02"]["online"] is False
        assert by_id["beta-02"]["health"] == "unhealthy"

        agent.catalog.mark_probe_required("alpha-02")
        gated = {cast(str, item["id"]): item for item in _items(client.get("/api/v1/benches"))}
        assert gated["alpha-02"]["online"] is False
        assert gated["alpha-02"]["status"] == "offline"
        assert _json(client.get("/api/v1/health"))["benches"] == {"total": 4, "online": 2}
        assert [
            item["id"]
            for item in _items(client.get("/api/v1/benches", params={"available": "true"}))
        ] == ["alpha-01", "beta-01"]
        agent.catalog.clear_probe_required("alpha-02")
        client.get("/api/v1/benches")

        firmware = _items(client.get("/api/v1/benches", params={"capability": "firmware"}))
        assert [item["id"] for item in firmware] == ["alpha-01", "beta-01"]
        simulated = _items(
            client.get(
                "/api/v1/benches",
                params=[("label", "board:virtual"), ("label", "location=simulation")],
            )
        )
        assert [item["id"] for item in simulated] == list(by_id)
        offline = _items(client.get("/api/v1/benches", params={"online": "false"}))
        assert [item["id"] for item in offline] == ["beta-02"]
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "beta-02", "owner": "dana", "duration_seconds": 60},
            ),
            409,
            "BENCH_OFFLINE",
        )
        offline_queue = client.post(
            "/api/v1/reservations",
            json={
                "bench_id": "beta-02",
                "owner": "dana",
                "duration_seconds": 60,
                "queue_if_busy": True,
            },
        )
        assert offline_queue.status_code == 201
        assert _json(offline_queue)["status"] == "waiting"
        assert client.portal is not None
        assert client.portal.call(agent.scheduling_service.promote_queues) == 0
        beta.simulator.set_online("beta-02", True)
        recovered_probe = client.post(
            "/api/v1/benches/beta-02/actions/probe", json={"owner": "dana"}
        )
        assert recovered_probe.status_code == 200
        assert _json(recovered_probe)["status"] == "online"
        assert agent.catalog.is_online("beta-02")
        assert client.portal.call(agent.scheduling_service.promote_queues) == 1

        for bench_id, owner in (("alpha-01", "alice"), ("beta-01", "bob")):
            response = client.post(
                "/api/v1/reservations",
                json={"bench_id": bench_id, "owner": owner, "duration_seconds": 60},
            )
            assert response.status_code == 201

        available = _items(client.get("/api/v1/benches", params={"available": "true"}))
        unavailable = _items(client.get("/api/v1/benches", params={"available": "false"}))
        assert [item["id"] for item in available] == ["alpha-02"]
        assert [item["id"] for item in unavailable] == [
            "alpha-01",
            "beta-01",
            "beta-02",
        ]

        alpha_power = client.post(
            "/api/v1/benches/alpha-01/actions/power-off", json={"owner": "alice"}
        )
        assert alpha_power.status_code == 202
        assert (
            _wait_for_operation(client, cast(str, _json(alpha_power)["operation_id"]))["status"]
            == "succeeded"
        )
        assert _json(client.get("/api/v1/benches/alpha-01"))["powered"] is False
        assert _json(client.get("/api/v1/benches/beta-01"))["powered"] is True

        beta_power = client.post("/api/v1/benches/beta-01/actions/power-off", json={"owner": "bob"})
        assert beta_power.status_code == 202
        assert (
            _wait_for_operation(client, cast(str, _json(beta_power)["operation_id"]))["status"]
            == "succeeded"
        )
        assert _json(client.get("/api/v1/benches/beta-01"))["powered"] is False


def test_reservation_api_immediate_future_filters_extension_release_cancel_and_time(
    tmp_path: Path,
) -> None:
    clock = FakeClock("2026-07-20T10:00:00Z")
    with _phase3_client(tmp_path, clock=clock) as (agent, client):
        request = {
            "bench_id": "alpha-01",
            "owner": "alice",
            "duration_seconds": 600,
            "idempotency_key": "alice-alpha",
        }
        created_response = client.post("/api/v1/reservations", json=request)
        assert created_response.status_code == 201
        created = _json(created_response)
        assert created["status"] == "active"
        assert created["source"] == "api"
        assert created["starts_at"] == "2026-07-20T10:00:00Z"
        assert created["ends_at"] == "2026-07-20T10:10:00Z"

        repeated = _json(
            client.post(
                "/api/v1/reservations",
                json=request | {"duration_seconds": 60},
            )
        )
        assert repeated["id"] == created["id"]
        assert _json(client.get(f"/api/v1/reservations/{created['id']}")) == created
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "alpha-01", "owner": "mallory", "duration_seconds": 60},
            ),
            409,
            "BENCH_ALREADY_RESERVED",
        )
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "beta-02", "owner": "dana", "duration_seconds": 1201},
            ),
            409,
            "RESERVATION_MAX_DURATION_EXCEEDED",
        )
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={
                    "bench_id": "beta-02",
                    "owner": "dana",
                    "starts_at": "2026-07-20T09:59:59Z",
                    "duration_seconds": 60,
                },
            ),
            409,
            "RESERVATION_TIME_CONFLICT",
        )

        extended_response = client.post(
            f"/api/v1/reservations/{created['id']}/extend",
            json={"owner": "alice", "duration_seconds": 30},
        )
        assert extended_response.status_code == 200
        assert _json(extended_response)["ends_at"] == "2026-07-20T10:10:30Z"
        _assert_error(
            client.post(
                f"/api/v1/reservations/{created['id']}/extend",
                json={"owner": "alice", "duration_seconds": 571},
            ),
            409,
            "RESERVATION_MAX_DURATION_EXCEEDED",
        )

        conflict = _json(
            client.post(
                "/api/v1/reservations",
                json={
                    "bench_id": "alpha-01",
                    "owner": "carol",
                    "starts_at": "2026-07-20T10:11:00+00:00",
                    "duration_seconds": 60,
                },
            )
        )
        extension_error = client.post(
            f"/api/v1/reservations/{created['id']}/extend",
            json={"owner": "alice", "duration_seconds": 60},
        )
        _assert_error(extension_error, 409, "RESERVATION_EXTENSION_CONFLICT")
        _assert_error(
            client.post(
                f"/api/v1/reservations/{created['id']}/extend",
                json={"owner": "mallory", "duration_seconds": 1},
            ),
            403,
            "RESERVATION_OWNER_MISMATCH",
        )
        assert (
            client.post(
                f"/api/v1/reservations/{conflict['id']}/cancel",
                json={"owner": "carol"},
            ).status_code
            == 200
        )

        future_start = clock.now() + timedelta(minutes=5)
        future_response = client.post(
            "/api/v1/reservations",
            json={
                "bench_id": "beta-01",
                "owner": "bob",
                "starts_at": future_start.isoformat(),
                "duration_seconds": 180,
                "idempotency_key": "bob-future",
            },
        )
        assert future_response.status_code == 201
        future = _json(future_response)
        assert future["status"] == "scheduled"
        assert future["activated_at"] is None

        filtered = _items(
            client.get(
                "/api/v1/reservations",
                params={
                    "bench_id": "beta-01",
                    "owner": "bob",
                    "status": "scheduled",
                    "starts_after": (future_start - timedelta(seconds=1)).isoformat(),
                    "starts_before": (future_start + timedelta(seconds=1)).isoformat(),
                    "limit": 1,
                },
            )
        )
        assert [item["id"] for item in filtered] == [future["id"]]

        cancelled_future = _json(
            client.post(
                "/api/v1/reservations",
                json={
                    "bench_id": "beta-02",
                    "owner": "dana",
                    "starts_at": (clock.now() + timedelta(minutes=6)).isoformat(),
                    "duration_seconds": 60,
                },
            )
        )
        cancelled = client.post(
            f"/api/v1/reservations/{cancelled_future['id']}/cancel",
            json={"owner": "dana"},
        )
        assert cancelled.status_code == 200
        assert _json(cancelled)["status"] == "cancelled"

        released = client.post(
            f"/api/v1/reservations/{created['id']}/release", json={"owner": "alice"}
        )
        assert released.status_code == 200
        assert _json(released)["status"] == "released"
        released_again = client.post(
            f"/api/v1/reservations/{created['id']}/release", json={"owner": "alice"}
        )
        assert _json(released_again)["released_at"] == _json(released)["released_at"]

        clock.advance(minutes=5)
        assert client.portal is not None
        assert client.portal.call(agent.scheduling_service.process_due_reservations) == 1
        active_future = _json(client.get(f"/api/v1/reservations/{future['id']}"))
        assert active_future["status"] == "active"
        assert active_future["activated_at"] == "2026-07-20T10:05:00Z"
        assert (
            client.post(
                f"/api/v1/reservations/{future['id']}/release", json={"owner": "bob"}
            ).status_code
            == 200
        )

        expiring = _json(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "alpha-02", "owner": "erin", "duration_seconds": 60},
            )
        )
        clock.advance(minutes=1)
        assert client.portal.call(agent.scheduling_service.expire_reservations) == 1
        assert _json(client.get(f"/api/v1/reservations/{expiring['id']}"))["status"] == "expired"
        assert client.portal.call(agent.scheduling_service.process) == (0, 0, 0)

        cancelled_items = _items(
            client.get("/api/v1/reservations", params={"status": "cancelled", "owner": "dana"})
        )
        assert [item["id"] for item in cancelled_items] == [cancelled_future["id"]]


def test_queue_list_cancel_idempotency_promotion_and_timeline(tmp_path: Path) -> None:
    with _phase3_client(tmp_path) as (agent, client):
        active = _json(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "alpha-01", "owner": "alice", "duration_seconds": 120},
            )
        )
        queued_request = {
            "owner": "bob",
            "duration_seconds": 60,
            "idempotency_key": "bob-queue",
        }
        queued_response = client.post("/api/v1/benches/alpha-01/queue", json=queued_request)
        assert queued_response.status_code == 201
        queued = _json(queued_response)
        repeated = _json(client.post("/api/v1/benches/alpha-01/queue", json=queued_request))
        assert repeated["id"] == queued["id"]
        assert repeated["position"] == 1

        second_response = client.post(
            "/api/v1/reservations",
            json={
                "bench_id": "alpha-01",
                "owner": "carol",
                "duration_seconds": 60,
                "queue_if_busy": True,
            },
        )
        assert second_response.status_code == 201
        second = _json(second_response)
        waiting = _items(client.get("/api/v1/benches/alpha-01/queue"))
        assert [(item["owner"], item["position"]) for item in waiting] == [
            ("bob", 1),
            ("carol", 2),
        ]

        _assert_error(
            client.request(
                "DELETE",
                f"/api/v1/queue/{second['id']}",
                json={"owner": "mallory"},
            ),
            403,
            "QUEUE_OWNER_MISMATCH",
        )
        cancelled = client.request(
            "DELETE", f"/api/v1/queue/{second['id']}", json={"owner": "carol"}
        )
        assert cancelled.status_code == 204
        assert (
            client.request(
                "DELETE", f"/api/v1/queue/{second['id']}", json={"owner": "carol"}
            ).status_code
            == 204
        )
        assert [item["id"] for item in _items(client.get("/api/v1/benches/alpha-01/queue"))] == [
            queued["id"]
        ]

        assert (
            client.post(
                f"/api/v1/reservations/{active['id']}/release", json={"owner": "alice"}
            ).status_code
            == 200
        )
        assert client.portal is not None
        assert client.portal.call(agent.scheduling_service.promote_queues) == 1
        assert _items(client.get("/api/v1/benches/alpha-01/queue")) == []

        promoted = _items(
            client.get(
                "/api/v1/reservations",
                params={"bench_id": "alpha-01", "owner": "bob", "status": "active"},
            )
        )
        assert len(promoted) == 1
        assert promoted[0]["source"] == "system"
        assert promoted[0]["metadata"] == {"queue_entry_id": queued["id"]}

        timeline = _items(
            client.get(
                "/api/v1/benches/alpha-01/timeline",
                params={"category": "reservation", "limit": 50},
            )
        )
        assert all(item["category"] == "reservation" for item in timeline)
        assert {item["event_type"] for item in timeline} >= {
            "RESERVATION_ACTIVATED",
            "RESERVATION_RELEASED",
            "QUEUE_ENTRY_CREATED",
            "QUEUE_ENTRY_CANCELLED",
            "QUEUE_ENTRY_PROMOTED",
        }
        timestamps = [datetime.fromisoformat(cast(str, item["timestamp"])) for item in timeline]
        bounded = _items(
            client.get(
                "/api/v1/benches/alpha-01/timeline",
                params={
                    "category": "reservation",
                    "after": (min(timestamps) - timedelta(seconds=1)).isoformat(),
                    "before": (max(timestamps) + timedelta(seconds=1)).isoformat(),
                    "limit": 3,
                },
            )
        )
        assert len(bounded) == 3


def test_workflow_list_show_run_watch_cancel_and_release_after(tmp_path: Path) -> None:
    with _phase3_client(tmp_path, workflows=True) as (agent, client):
        definitions = _items(client.get("/api/v1/workflows"))
        assert [item["name"] for item in definitions] == ["cancellable", "quick-check"]
        shown = _json(client.get("/api/v1/workflows/quick-check"))
        assert shown["version"] == 1
        assert [step["action"] for step in cast(list[JsonObject], shown["steps"])] == [
            "probe",
            "wait",
        ]

        _assert_error(
            client.post(
                "/api/v1/workflows/quick-check/runs",
                json={"bench_id": "alpha-01", "owner": "alice"},
            ),
            409,
            "WORKFLOW_RESERVATION_REQUIRED",
        )

        started_response = client.post(
            "/api/v1/workflows/quick-check/runs",
            json={
                "bench_id": "alpha-01",
                "owner": "alice",
                "reserve_duration_seconds": 60,
                "release_after": True,
            },
        )
        assert started_response.status_code == 201
        started = _json(started_response)
        run_id = UUID(cast(str, started["id"]))
        assert client.portal is not None
        assert client.portal.call(agent.workflow_service.wait, run_id).status.value == "succeeded"

        watched = _json(client.get(f"/api/v1/workflow-runs/{run_id}"))
        assert watched["status"] == "succeeded"
        steps = cast(list[JsonObject], watched["steps"])
        assert [(step["action"], step["status"]) for step in steps] == [
            ("probe", "succeeded"),
            ("wait", "succeeded"),
        ]
        reservation_id = cast(str, watched["reservation_id"])
        for _ in range(100):
            reservation = _json(client.get(f"/api/v1/reservations/{reservation_id}"))
            if reservation["status"] == "released":
                break
            time.sleep(0.001)
        assert reservation["status"] == "released"

        reservation = _json(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "beta-01", "owner": "bob", "duration_seconds": 60},
            )
        )
        _assert_error(
            client.post(
                "/api/v1/workflows/quick-check/runs",
                json={"bench_id": "beta-01", "owner": "alice"},
            ),
            409,
            "WORKFLOW_RESERVATION_REQUIRED",
        )
        _assert_error(
            client.post(
                "/api/v1/workflows/quick-check/runs",
                json={
                    "bench_id": "beta-01",
                    "owner": "bob",
                    "reservation_id": "00000000-0000-0000-0000-000000000000",
                },
            ),
            409,
            "WORKFLOW_RESERVATION_REQUIRED",
        )
        cancellable_response = client.post(
            "/api/v1/workflows/cancellable/runs",
            json={
                "bench_id": "beta-01",
                "owner": "bob",
                "reservation_id": reservation["id"],
            },
        )
        assert cancellable_response.status_code == 201
        cancellable_id = cast(str, _json(cancellable_response)["id"])
        _wait_for_workflow_status(client, cancellable_id, {"running"})
        _assert_error(
            client.post(f"/api/v1/workflow-runs/{cancellable_id}/cancel", json={"owner": "alice"}),
            403,
            "RESERVATION_OWNER_MISMATCH",
        )
        cancel_response = client.post(
            f"/api/v1/workflow-runs/{cancellable_id}/cancel", json={"owner": "bob"}
        )
        assert cancel_response.status_code == 200
        assert _json(cancel_response)["status"] in {"cancel_requested", "cancelled"}
        cancelled = client.portal.call(agent.workflow_service.wait, UUID(cancellable_id))
        assert cancelled.status.value == "cancelled"
        cancelled_watch = _json(client.get(f"/api/v1/workflow-runs/{cancellable_id}"))
        assert cancelled_watch["status"] == "cancelled"
        assert all(
            step["status"] == "cancelled"
            for step in cast(list[JsonObject], cancelled_watch["steps"])
        )
        released = client.post(
            f"/api/v1/reservations/{reservation['id']}/release", json={"owner": "bob"}
        )
        assert released.status_code == 200

        workflow_timeline = _items(
            client.get(
                "/api/v1/benches/alpha-01/timeline",
                params={"category": "workflow"},
            )
        )
        assert {item["event_type"] for item in workflow_timeline} >= {
            "WORKFLOW_STARTED",
            "WORKFLOW_STEP_STARTED",
            "WORKFLOW_STEP_SUCCEEDED",
            "WORKFLOW_COMPLETED",
        }


def test_phase3_api_stable_error_contracts(tmp_path: Path) -> None:
    missing_id = "00000000-0000-0000-0000-000000000000"
    with _phase3_client(tmp_path, workflows=True) as (_agent, client):
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={"bench_id": "missing", "owner": "alice", "duration_seconds": 60},
            ),
            404,
            "BENCH_NOT_FOUND",
        )
        _assert_error(
            client.get(f"/api/v1/reservations/{missing_id}"),
            404,
            "RESERVATION_NOT_FOUND",
        )
        _assert_error(
            client.request("DELETE", f"/api/v1/queue/{missing_id}", json={"owner": "alice"}),
            404,
            "QUEUE_ENTRY_NOT_FOUND",
        )
        _assert_error(client.get("/api/v1/workflows/not-defined"), 404, "WORKFLOW_NOT_FOUND")
        _assert_error(
            client.get(f"/api/v1/workflow-runs/{missing_id}"),
            404,
            "WORKFLOW_RUN_NOT_FOUND",
        )
        validation = _assert_error(
            client.get("/api/v1/reservations", params={"status": "not-a-status"}),
            422,
            "VALIDATION_ERROR",
        )
        assert cast(JsonObject, validation["details"])["errors"]
        _assert_error(
            client.post(
                "/api/v1/reservations",
                json={
                    "bench_id": "alpha-01",
                    "owner": "alice",
                    "starts_at": "2026-07-21T10:00:00",
                    "duration_seconds": 60,
                },
            ),
            422,
            "VALIDATION_ERROR",
        )
        _assert_error(
            client.get("/api/v1/benches", params={"label": "missing-separator"}),
            422,
            "VALIDATION_ERROR",
        )
