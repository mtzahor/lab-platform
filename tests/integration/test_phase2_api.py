from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient
from lab_platform.agent import create_agent, create_app
from lab_platform.models import BenchOperationLock
from lab_platform.persistence import SQLiteDatabase, SQLiteOperationLockRepository


def _wait(client: TestClient, operation_id: str) -> dict[str, object]:
    for _ in range(200):
        operation = cast(dict[str, object], client.get(f"/api/v1/operations/{operation_id}").json())
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.005)
    raise AssertionError("operation did not complete")


def test_probe_reset_serial_operation_and_artifact_api(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  log_level: ERROR\nplugins: []\nartifacts:\n  directory: ./artifacts\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  benches: 1\n  speed_multiplier: 1000\n",
        encoding="utf-8",
    )
    agent = create_agent(tmp_path)
    asyncio.run(agent.start())
    with TestClient(create_app(agent), raise_server_exceptions=False) as client:
        schema = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/benches/{bench_id}/actions/probe" in schema
        assert "/api/v1/benches/{bench_id}/actions/read-serial" in schema
        assert "/api/v1/benches/{bench_id}/actions/reset" in schema

        blocked_probe = client.post(
            "/api/v1/benches/bench-01/actions/probe", json={"owner": "alice"}
        )
        assert blocked_probe.status_code == 404

        unreserved = client.post(
            "/api/v1/benches/bench-01/actions/read-serial",
            json={"owner": "alice", "until_pattern": "READY"},
        )
        assert unreserved.status_code == 404
        client.post("/api/v1/benches/bench-01/reservation", json={"owner": "alice"})

        competing_database = SQLiteDatabase(tmp_path / ".lab-platform" / "lab.db")
        competing_database.initialize()
        competing_locks = SQLiteOperationLockRepository(competing_database)
        competing = BenchOperationLock(
            bench_id="bench-01",
            operation_id=uuid4(),
            acquired_at=datetime.now(UTC),
        )
        asyncio.run(competing_locks.acquire(competing))
        blocked_by_operation = client.post(
            "/api/v1/benches/bench-01/actions/probe", json={"owner": "alice"}
        )
        assert blocked_by_operation.status_code == 409
        assert blocked_by_operation.json()["error"]["code"] == "BENCH_OPERATION_IN_PROGRESS"
        asyncio.run(competing_locks.release("bench-01", competing.operation_id))
        competing_database.close()

        probe = client.post("/api/v1/benches/bench-01/actions/probe", json={"owner": "alice"})
        assert probe.status_code == 200
        assert probe.json()["status"] == "online"
        assert probe.json()["serial_port"] == "sim://bench-01"

        serial = client.post(
            "/api/v1/benches/bench-01/actions/read-serial",
            json={
                "owner": "alice",
                "timeout_seconds": 1,
                "until_pattern": "READY",
                "max_lines": 20,
            },
        )
        assert serial.status_code == 202
        operation_id = serial.json()["operation_id"]
        assert _wait(client, operation_id)["status"] == "succeeded"

        artifacts = client.get(f"/api/v1/operations/{operation_id}/artifacts")
        assert artifacts.status_code == 200
        metadata = artifacts.json()["items"][0]
        assert metadata["type"] == "serial_log"
        assert metadata["size_bytes"] > 0
        serial_log = client.get(f"/api/v1/operations/{operation_id}/artifacts/serial")
        assert serial_log.status_code == 200
        assert serial_log.json()["text"].endswith("READY\n")
        assert Path(metadata["path"]).is_file()

        reset = client.post("/api/v1/benches/bench-01/actions/reset", json={"owner": "alice"})
        assert reset.status_code == 202
        assert _wait(client, reset.json()["operation_id"])["status"] == "succeeded"

        missing_artifact = client.get(
            f"/api/v1/operations/{reset.json()['operation_id']}/artifacts/serial"
        )
        assert missing_artifact.status_code == 404
        assert missing_artifact.json()["error"]["code"] == "OPERATION_ARTIFACT_NOT_FOUND"
    asyncio.run(agent.shutdown())
