from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from lab_platform.agent import LabAgent, create_agent, create_app


def _agent(root: Path) -> LabAgent:
    (root / "agent.yaml").write_text(
        "agent:\n  log_level: ERROR\nplugins: []\n"
        "artifacts:\n  directory: ./artifacts\n  max_firmware_size_mb: 1\n",
        encoding="utf-8",
    )
    (root / "simlab.yaml").write_text(
        "simlab:\n  benches: 2\n  speed_multiplier: 20\n  flash_duration_seconds: 1\n",
        encoding="utf-8",
    )
    agent = create_agent(root)
    asyncio.run(agent.start())
    return agent


def _wait(client: TestClient, operation_id: str) -> dict[str, object]:
    for _ in range(200):
        response = client.get(f"/api/v1/operations/{operation_id}")
        operation = response.json()
        operation = cast(dict[str, object], operation)
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.005)
    raise AssertionError("operation did not complete")


def test_openapi_health_benches_reservations_and_structured_errors(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    with TestClient(create_app(agent), raise_server_exceptions=False) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.headers["x-request-id"]
        assert health.json()["database"] == "healthy"

        schema = client.get("/openapi.json").json()
        assert "/api/v1/benches/{bench_id}/actions/flash" in schema["paths"]
        assert "/api/v1/operations/{operation_id}" in schema["paths"]
        unknown_route = client.get("/api/v1/not-a-route")
        assert unknown_route.status_code == 404
        assert unknown_route.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        benches = client.get("/api/v1/benches?capability=firmware&reserved=false").json()
        assert [bench["id"] for bench in benches["items"]] == ["bench-01"]
        assert client.get("/api/v1/benches?status=available").status_code == 200

        unreserved = client.post(
            "/api/v1/benches/bench-01/actions/power-on", json={"owner": "alice"}
        )
        assert unreserved.status_code == 404
        assert unreserved.json()["error"]["code"] == "BENCH_NOT_RESERVED"
        assert unreserved.json()["error"]["request_id"]

        invalid = client.post("/api/v1/benches/bench-01/reservation", json={"owner": ""})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

        reserved = client.post("/api/v1/benches/bench-01/reservation", json={"owner": "alice"})
        assert reserved.status_code == 201
        reservation_id = reserved.json()["id"]
        same = client.post("/api/v1/benches/bench-01/reservation", json={"owner": "alice"})
        assert same.json()["id"] == reservation_id
        conflict = client.post("/api/v1/benches/bench-01/reservation", json={"owner": "bob"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "BENCH_ALREADY_RESERVED"

        assert client.get("/api/v1/benches/bench-01/reservation").status_code == 200
        mismatch = client.request(
            "DELETE",
            "/api/v1/benches/bench-01/reservation",
            json={"owner": "bob"},
        )
        assert mismatch.status_code == 403
        assert mismatch.json()["error"]["code"] == "RESERVATION_OWNER_MISMATCH"

        missing = client.get("/api/v1/benches/not-real")
        assert missing.status_code == 404
        assert missing.json()["error"]["details"] == {"bench_id": "not-real"}

        released = client.request(
            "DELETE",
            "/api/v1/benches/bench-01/reservation",
            json={"owner": "alice"},
        )
        assert released.status_code == 204
        assert client.get("/api/v1/benches/bench-01/reservation").status_code == 404
        assert (
            client.request(
                "DELETE",
                "/api/v1/benches/bench-01/reservation",
                json={"owner": "anyone"},
            ).status_code
            == 204
        )
    asyncio.run(agent.shutdown())


def test_firmware_operations_locking_cancellation_filters_and_events(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    with TestClient(create_app(agent), raise_server_exceptions=False) as client:
        for bench in ("bench-01", "bench-02"):
            assert (
                client.post(
                    f"/api/v1/benches/{bench}/reservation", json={"owner": "alice"}
                ).status_code
                == 201
            )

        unsupported = client.post(
            "/api/v1/benches/bench-02/actions/flash",
            data={"owner": "alice"},
            files={"firmware": ("demo.bin", b"abc", "application/octet-stream")},
        )
        assert unsupported.status_code == 409
        assert unsupported.json()["error"]["code"] == "CAPABILITY_NOT_SUPPORTED"

        empty = client.post(
            "/api/v1/benches/bench-01/actions/flash",
            data={"owner": "alice"},
            files={"firmware": ("empty.bin", b"", "application/octet-stream")},
        )
        assert empty.status_code == 400
        assert empty.json()["error"]["code"] == "INVALID_FIRMWARE_FILE"

        too_large = client.post(
            "/api/v1/benches/bench-01/actions/flash",
            data={"owner": "alice"},
            files={
                "firmware": (
                    "large.bin",
                    b"x" * (1024 * 1024 + 1),
                    "application/octet-stream",
                )
            },
        )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "FIRMWARE_FILE_TOO_LARGE"

        submitted = client.post(
            "/api/v1/benches/bench-01/actions/flash",
            data={"owner": "alice", "version": "4.0.0"},
            files={"firmware": ("demo.bin", b"firmware", "application/octet-stream")},
        )
        assert submitted.status_code == 202
        operation_id = submitted.json()["operation_id"]
        locked = client.post("/api/v1/benches/bench-01/actions/power-off", json={"owner": "alice"})
        assert locked.status_code == 409
        assert locked.json()["error"]["code"] == "BENCH_OPERATION_IN_PROGRESS"
        operation = _wait(client, operation_id)
        assert operation["status"] == "succeeded"
        assert operation["progress"] == 100
        assert client.get("/api/v1/benches/bench-01").json()["firmware_version"] == "4.0.0"

        second = client.post(
            "/api/v1/benches/bench-01/actions/flash",
            data={"owner": "alice"},
            files={"firmware": ("cancel.bin", b"cancel", "application/octet-stream")},
        ).json()
        wrong_owner = client.post(
            f"/api/v1/operations/{second['operation_id']}/cancel", json={"owner": "bob"}
        )
        assert wrong_owner.status_code == 403
        cancel = client.post(
            f"/api/v1/operations/{second['operation_id']}/cancel", json={"owner": "alice"}
        )
        assert cancel.status_code == 200
        assert _wait(client, second["operation_id"])["status"] == "cancelled"

        operations = client.get(
            "/api/v1/operations?bench_id=bench-01&type=flash_firmware&limit=10"
        ).json()["items"]
        assert len(operations) == 2
        assert client.get(f"/api/v1/operations/{operation_id}").status_code == 200
        assert (
            client.get("/api/v1/operations/00000000-0000-0000-0000-000000000000").status_code == 404
        )

        events = client.get(
            "/api/v1/events?bench_id=bench-01&event_type=FLASH_COMPLETED&limit=10"
        ).json()["items"]
        assert len(events) == 1
        assert events[0]["operation_id"] == operation_id
    asyncio.run(agent.shutdown())
