from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from lab_platform.agent import create_agent, create_app


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

        probe = client.post("/api/v1/benches/bench-01/actions/probe", json={})
        assert probe.status_code == 200
        assert probe.json()["status"] == "online"
        assert probe.json()["serial_port"] == "sim://bench-01"

        unreserved = client.post(
            "/api/v1/benches/bench-01/actions/read-serial",
            json={"owner": "alice", "until_pattern": "READY"},
        )
        assert unreserved.status_code == 404
        client.post("/api/v1/benches/bench-01/reservation", json={"owner": "alice"})

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
