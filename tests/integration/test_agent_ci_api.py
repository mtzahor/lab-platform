from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from httpx2 import Response
from lab_platform.agent import LabAgent, create_agent, create_app

JsonObject = dict[str, object]

FULL_SCOPES = [
    "benches:read",
    "reservations:write",
    "workflows:run",
    "operations:read",
    "artifacts:read",
    "artifacts:write",
    "ci:sessions",
]


def _write_config(root: Path) -> None:
    workflows = root / "workflows"
    workflows.mkdir()
    (workflows / "esp32-ci.yaml").write_text(
        """name: esp32-ci
version: 2
description: Phase 4 API integration workflow
inputs:
  firmware:
    type: artifact
    required: true
  expected_version:
    type: string
    required: true
requirements:
  capabilities: [firmware, serial, reset, probe]
  labels:
    board: esp32
steps:
  - name: Probe target
    action: probe
  - name: Flash firmware
    action: flash
    firmware: "${{ inputs.firmware }}"
    version: "${{ inputs.expected_version }}"
  - name: Reset target
    action: reset
  - name: Capture boot log
    action: read_serial
    until_pattern: "^READY$"
    timeout_seconds: 2
    max_lines: 50
  - name: Verify self-test
    action: assert_serial
    pattern: "^SELF_TEST=PASS$"
  - name: Verify version
    action: assert_serial
    pattern: "^FIRMWARE_VERSION=${{ inputs.expected_version }}$"
""",
        encoding="utf-8",
    )
    (workflows / "cancellable.yaml").write_text(
        """name: cancellable
version: 2
requirements:
  capabilities: []
  labels:
    board: esp32
steps:
  - name: Long wait
    action: wait
    seconds: 30
""",
        encoding="utf-8",
    )
    (root / "agent.yaml").write_text(
        """agent:
  name: phase4-ci-api-agent
  log_level: ERROR
plugins: []
backends:
  - id: simlab-ci
    type: simlab
    config:
      benches: 2
      bench_prefix: esp32-ci
      speed_multiplier: 1000
      flash_duration_seconds: 0.01
      labels:
        board: esp32
        location: simulation
        purpose: hardware-ci
database:
  url: sqlite:///./lab.db
artifacts:
  directory: ./artifacts
  max_firmware_size_mb: 1
  max_upload_size_mb: 1
ci:
  default_reservation_minutes: 1
  maximum_reservation_minutes: 60
  heartbeat_interval_seconds: 1
  heartbeat_timeout_seconds: 2
  bench_wait_timeout_seconds: 10
  session_timeout_seconds: 60
  workflow_timeout_seconds: 30
  step_timeout_seconds: 10
  cleanup_timeout_seconds: 5
  reaper_poll_interval_seconds: 3600
reservations:
  default_duration_minutes: 1
  maximum_duration_minutes: 60
  expiry_grace_seconds: 0
  queue_enabled: true
  scheduled_protection_window_minutes: 0
scheduler:
  poll_interval_seconds: 3600
  automatic_assignment: true
workflows:
  definitions_directory: ./workflows
""",
        encoding="utf-8",
    )


@contextmanager
def _phase4_client(root: Path) -> Iterator[tuple[LabAgent, TestClient]]:
    _write_config(root)
    agent = create_agent(root)
    try:
        with TestClient(create_app(agent), raise_server_exceptions=False) as client:
            yield agent, client
    finally:
        asyncio.run(agent.shutdown())


def _json(response: Response) -> JsonObject:
    return cast(JsonObject, response.json())


def _items(response: Response) -> list[JsonObject]:
    return cast(list[JsonObject], _json(response)["items"])


def _issue_token(
    client: TestClient,
    *,
    name: str,
    owner: str,
    scopes: list[str],
    administrator: str | None = None,
) -> tuple[str, JsonObject]:
    response = client.post(
        "/api/v1/tokens",
        headers=_auth(administrator) if administrator is not None else None,
        json={"name": name, "owner": owner, "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    payload = _json(response)
    plaintext = cast(str, payload["token"])
    assert plaintext.startswith("lp_")
    assert "token_hash" not in payload
    return plaintext, payload


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_openapi_declares_bearer_security_and_download_media(tmp_path: Path) -> None:
    with _phase4_client(tmp_path) as (_agent, client):
        schema = _json(client.get("/openapi.json"))

    components = cast(JsonObject, schema["components"])
    schemes = cast(JsonObject, components["securitySchemes"])
    bearer = cast(JsonObject, schemes["BearerAuth"])
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"

    paths = cast(JsonObject, schema["paths"])
    for path, path_item_value in paths.items():
        path_item = cast(JsonObject, path_item_value)
        for method, operation_value in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation = cast(JsonObject, operation_value)
            parameters = cast(list[JsonObject], operation.get("parameters", []))
            assert all(parameter.get("name") != "Authorization" for parameter in parameters)
            if path not in {"/api/v1/health", "/api/v1/version"}:
                assert {"BearerAuth": []} in cast(list[JsonObject], operation["security"])

    artifact_content = cast(
        JsonObject,
        cast(
            JsonObject,
            cast(JsonObject, paths["/api/v1/artifacts/{artifact_id}/content"])["get"],
        )["responses"],
    )
    artifact_media = cast(JsonObject, cast(JsonObject, artifact_content["200"])["content"])
    assert set(artifact_media) == {"application/octet-stream"}
    assert cast(
        JsonObject, cast(JsonObject, artifact_media["application/octet-stream"])["schema"]
    ) == {
        "type": "string",
        "format": "binary",
    }

    junit_content = cast(
        JsonObject,
        cast(
            JsonObject,
            cast(
                JsonObject,
                paths["/api/v1/workflow-runs/{workflow_run_id}/results/junit"],
            )["get"],
        )["responses"],
    )
    junit_media = cast(JsonObject, cast(JsonObject, junit_content["200"])["content"])
    assert set(junit_media) == {"application/xml"}


def _create_session(
    client: TestClient,
    token: str,
    external_run_id: str,
    *,
    idempotency_key: str,
    required_capabilities: list[str] | None = None,
) -> JsonObject:
    response = client.post(
        "/api/v1/ci/sessions",
        headers={**_auth(token), "Idempotency-Key": idempotency_key},
        json={
            "provider": "github_actions",
            "external_run_id": external_run_id,
            "repository": "openai/lab-platform",
            "ref": "refs/pull/42/head",
            "commit_sha": "a" * 40,
            "actor": "octocat",
            "bench_request": {
                "required_capabilities": required_capabilities
                or ["firmware", "serial", "reset", "probe"],
                "required_labels": {"board": "esp32"},
                "preferred_labels": {"purpose": "hardware-ci"},
                "allow_simulated": True,
                "allow_physical": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    return _json(response)


def _wait_for_session(
    client: TestClient,
    token: str,
    session_id: str,
    statuses: set[str],
) -> JsonObject:
    for _ in range(300):
        response = client.get(f"/api/v1/ci/sessions/{session_id}", headers=_auth(token))
        assert response.status_code == 200, response.text
        session = _json(response)
        if cast(str, session["status"]) in statuses:
            return session
        time.sleep(0.005)
    raise AssertionError(f"CI session {session_id} did not reach {sorted(statuses)}")


def test_legacy_routes_require_scopes_and_bind_owner_after_token_bootstrap(
    tmp_path: Path,
) -> None:
    with _phase4_client(tmp_path) as (_agent, client):
        assert client.get("/api/v1/benches").status_code == 200
        incomplete_bootstrap = client.post(
            "/api/v1/tokens",
            json={"name": "incomplete", "owner": "github-actions", "scopes": ["ci:sessions"]},
        )
        assert incomplete_bootstrap.status_code == 403
        full_token, _record = _issue_token(
            client,
            name="legacy-full",
            owner="github-actions",
            scopes=FULL_SCOPES,
        )
        refused_last_admin = client.post(
            f"/api/v1/tokens/{_record['id']}/revoke",
            headers=_auth(full_token),
        )
        assert refused_last_admin.status_code == 403
        limited_token, _limited = _issue_token(
            client,
            name="legacy-limited",
            owner="github-actions",
            scopes=["ci:sessions"],
            administrator=full_token,
        )

        assert client.get("/api/v1/benches").status_code == 401
        assert client.get("/api/v1/benches", headers=_auth(limited_token)).status_code == 403
        assert client.get("/api/v1/benches", headers=_auth(full_token)).status_code == 200

        mismatched = client.post(
            "/api/v1/reservations",
            headers=_auth(full_token),
            json={
                "bench_id": "esp32-ci-01",
                "owner": "another-owner",
                "duration_seconds": 60,
            },
        )
        assert mismatched.status_code == 403
        assert _json(mismatched)["error"]["code"] == "PERMISSION_DENIED"  # type: ignore[index]

        created = client.post(
            "/api/v1/reservations",
            headers=_auth(full_token),
            json={
                "bench_id": "esp32-ci-01",
                "owner": "github-actions",
                "duration_seconds": 60,
            },
        )
        assert created.status_code == 201, created.text
        assert _json(created)["owner"] == "github-actions"
        assert client.get("/api/v1/reservations").status_code == 401
        assert client.get("/api/v1/reservations", headers=_auth(limited_token)).status_code == 403

        workflow_bypass = client.post(
            "/api/v1/workflows/esp32-ci/runs",
            headers=_auth(full_token),
            json={
                "bench_id": "esp32-ci-01",
                "owner": "another-owner",
                "inputs": {},
            },
        )
        assert workflow_bypass.status_code == 403


def test_token_auth_full_ci_workflow_artifacts_results_and_release(tmp_path: Path) -> None:
    with _phase4_client(tmp_path) as (_agent, client):
        full_token, full_record = _issue_token(
            client,
            name="github-full",
            owner="github-actions",
            scopes=FULL_SCOPES,
        )
        limited_token, limited_record = _issue_token(
            client,
            name="github-limited",
            owner="github-actions",
            scopes=["ci:sessions"],
            administrator=full_token,
        )
        backup_token, backup_record = _issue_token(
            client,
            name="github-backup-admin",
            owner="github-actions",
            scopes=FULL_SCOPES,
            administrator=full_token,
        )
        assert (
            client.post(
                "/api/v1/tokens",
                json={"name": "anonymous", "owner": "attacker", "scopes": FULL_SCOPES},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/tokens",
                headers=_auth(limited_token),
                json={"name": "escalated", "owner": "attacker", "scopes": FULL_SCOPES},
            ).status_code
            == 403
        )
        assert client.get("/api/v1/tokens", headers=_auth(limited_token)).status_code == 403
        listed = _items(client.get("/api/v1/tokens", headers=_auth(full_token)))
        assert {item["id"] for item in listed} == {
            full_record["id"],
            limited_record["id"],
            backup_record["id"],
        }
        assert all("token" not in item and "token_hash" not in item for item in listed)

        missing = client.post(
            "/api/v1/ci/sessions",
            json={"external_run_id": "missing-auth"},
        )
        assert missing.status_code == 401
        assert _json(missing)["error"]["code"] == "AUTHENTICATION_REQUIRED"  # type: ignore[index]
        forbidden = client.post(
            "/api/v1/ci/sessions",
            headers=_auth(limited_token),
            json={"external_run_id": "missing-scopes"},
        )
        assert forbidden.status_code == 403
        assert _json(forbidden)["error"]["code"] == "PERMISSION_DENIED"  # type: ignore[index]

        session = _create_session(
            client,
            full_token,
            "github-run-42",
            idempotency_key="session:42",
        )
        assert cast(JsonObject, session["bench_request"])["maximum_wait_seconds"] == 10
        assert cast(JsonObject, session["bench_request"])["reservation_duration_seconds"] == 60
        assert session["heartbeat_interval_seconds"] == 1
        assert session["cleanup_timeout_seconds"] == 5
        replay = _create_session(
            client,
            full_token,
            "ignored-retry-run",
            idempotency_key="session:42",
        )
        assert replay["id"] == session["id"]
        assert session["status"] == "reserved"
        assert session["bench_id"] == "esp32-ci-01"
        assert session["reservation_id"]
        session_id = cast(str, session["id"])
        reservation_id = cast(str, session["reservation_id"])
        heartbeat = client.post(
            f"/api/v1/ci/sessions/{session_id}/heartbeat",
            headers=_auth(full_token),
        )
        assert heartbeat.status_code == 200
        assert _json(heartbeat)["status"] == "reserved"
        assert _json(heartbeat)["heartbeat_at"] is not None

        benches = _items(
            client.get(
                "/api/v1/benches",
                headers=_auth(full_token),
                params={"label": "board=esp32"},
            )
        )
        assert [item["id"] for item in benches] == ["esp32-ci-01", "esp32-ci-02"]
        assert all(cast(JsonObject, item["labels"])["board"] == "esp32" for item in benches)

        firmware = b"phase-4-esp32-firmware"
        digest = hashlib.sha256(firmware).hexdigest()
        upload = client.post(
            "/api/v1/artifacts",
            headers={**_auth(full_token), "Idempotency-Key": "firmware:42"},
            data={
                "ci_session_id": session_id,
                "name": "firmware.bin",
                "artifact_type": "firmware",
                "sha256": digest,
            },
            files={"file": ("unsafe/firmware.bin", firmware, "application/octet-stream")},
        )
        assert upload.status_code == 201, upload.text
        artifact = _json(upload)
        assert artifact["name"] == "firmware.bin"
        assert artifact["sha256"] == digest
        artifact_id = cast(str, artifact["id"])
        retry = client.post(
            "/api/v1/artifacts",
            headers={**_auth(full_token), "Idempotency-Key": "firmware:42"},
            data={"ci_session_id": session_id, "artifact_type": "firmware"},
            files={"file": ("different.bin", b"different", "application/octet-stream")},
        )
        assert retry.status_code == 201
        assert _json(retry)["id"] == artifact_id
        download = client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=_auth(full_token))
        assert download.status_code == 200
        assert download.content == firmware

        run = client.post(
            f"/api/v1/ci/sessions/{session_id}/run",
            headers={**_auth(full_token), "Idempotency-Key": "workflow:42"},
            json={
                "workflow_name": "esp32-ci",
                "version": 2,
                "inputs": {
                    "firmware": {"artifact_id": artifact_id},
                    "expected_version": "2.0.0",
                },
            },
        )
        assert run.status_code == 202, run.text
        workflow_run_id = cast(str, _json(run)["workflow_run_id"])
        run_replay = client.post(
            f"/api/v1/ci/sessions/{session_id}/run",
            headers={**_auth(full_token), "Idempotency-Key": "workflow:42"},
            json={"workflow_name": "does-not-matter", "version": 2},
        )
        assert run_replay.status_code == 202
        assert _json(run_replay)["workflow_run_id"] == workflow_run_id

        finished = _wait_for_session(client, full_token, session_id, {"succeeded", "failed"})
        assert finished["status"] == "succeeded", finished
        assert finished["outcome"] == "succeeded"

        results = client.get(
            f"/api/v1/workflow-runs/{workflow_run_id}/results",
            headers=_auth(full_token),
        )
        assert results.status_code == 200, results.text
        result_payload = _json(results)
        assert result_payload["workflow_name"] == "esp32-ci"
        assert result_payload["status"] == "succeeded"
        assert all(
            cast(JsonObject, item)["status"] == "passed"
            for item in cast(list[object], result_payload["results"])
        )
        junit = client.get(
            f"/api/v1/workflow-runs/{workflow_run_id}/results/junit",
            headers=_auth(full_token),
        )
        assert junit.status_code == 200
        assert junit.headers["content-type"].startswith("application/xml")
        assert '<testsuite name="esp32-ci"' in junit.text

        finalized = client.post(
            f"/api/v1/ci/sessions/{session_id}/finalize",
            headers={**_auth(full_token), "Idempotency-Key": "finalize:42"},
        )
        assert finalized.status_code == 200, finalized.text
        final = _json(finalized)
        assert final["status"] == "completed"
        assert final["outcome"] == "succeeded"
        assert final["backend"] == "simlab"
        cleanup = cast(JsonObject, final["cleanup"])
        assert cleanup == {
            "reservation_released": True,
            "workflow_stopped": True,
            "locks_released": True,
            "serial_closed": True,
            "artifacts_finalized": True,
            "errors": [],
        }
        assert (
            client.get(f"/api/v1/reservations/{reservation_id}", headers=_auth(full_token)).json()[
                "status"
            ]
            == "released"
        )
        generated = _items(
            client.get(
                f"/api/v1/ci/sessions/{session_id}/artifacts",
                headers=_auth(full_token),
            )
        )
        assert {item["artifact_type"] for item in generated} >= {
            "firmware",
            "flash_log",
            "serial_log",
            "junit",
            "workflow_summary",
        }

        revoked = client.post(
            f"/api/v1/tokens/{full_record['id']}/revoke",
            headers=_auth(full_token),
        )
        assert revoked.status_code == 200
        assert _json(revoked)["revoked_at"] is not None
        rejected = client.get(f"/api/v1/ci/sessions/{session_id}", headers=_auth(full_token))
        assert rejected.status_code == 401
        assert _json(rejected)["error"]["code"] == "INVALID_API_TOKEN"  # type: ignore[index]
        assert client.get("/api/v1/tokens", headers=_auth(backup_token)).status_code == 200


def test_ci_cancellation_and_heartbeat_timeout_always_release_reservations(
    tmp_path: Path,
) -> None:
    with _phase4_client(tmp_path) as (agent, client):
        token, _record = _issue_token(
            client,
            name="github-cleanup",
            owner="github-actions",
            scopes=FULL_SCOPES,
        )

        cancellable = _create_session(
            client,
            token,
            "github-cancel",
            idempotency_key="session:cancel",
        )
        cancellable_id = cast(str, cancellable["id"])
        cancellable_reservation = cast(str, cancellable["reservation_id"])
        started = client.post(
            f"/api/v1/ci/sessions/{cancellable_id}/run",
            headers=_auth(token),
            json={"workflow_name": "cancellable", "version": 2},
        )
        assert started.status_code == 202, started.text
        cancelled = client.post(
            f"/api/v1/ci/sessions/{cancellable_id}/cancel", headers=_auth(token)
        )
        assert cancelled.status_code == 200, cancelled.text
        cancelled_session = _json(cancelled)
        assert cancelled_session["status"] == "completed"
        assert cancelled_session["outcome"] == "cancelled"
        assert cast(JsonObject, cancelled_session["cleanup"])["reservation_released"] is True
        assert (
            client.get(
                f"/api/v1/reservations/{cancellable_reservation}",
                headers=_auth(token),
            ).json()["status"]
            == "released"
        )

        abandoned = _create_session(
            client,
            token,
            "github-abandoned",
            idempotency_key="session:abandoned",
        )
        abandoned_id = cast(str, abandoned["id"])
        abandoned_reservation = cast(str, abandoned["reservation_id"])
        old_heartbeat = datetime.now(UTC) - timedelta(minutes=5)
        with agent._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE ci_sessions SET heartbeat_at = ?, timeout_at = ? WHERE id = ?",
                (
                    old_heartbeat.isoformat(),
                    (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    abandoned_id,
                ),
            )
        assert asyncio.run(agent.ci_session_service.process_maintenance()) >= 1
        timed_out = _json(client.get(f"/api/v1/ci/sessions/{abandoned_id}", headers=_auth(token)))
        assert timed_out["status"] == "completed"
        assert timed_out["outcome"] == "timed_out"
        assert timed_out["errors"] == ["CI_HEARTBEAT_MISSED"]
        assert cast(JsonObject, timed_out["cleanup"])["reservation_released"] is True
        assert (
            client.get(
                f"/api/v1/reservations/{abandoned_reservation}",
                headers=_auth(token),
            ).json()["status"]
            == "released"
        )


def test_agent_restart_recovers_reserved_and_terminal_ci_sessions(tmp_path: Path) -> None:
    _write_config(tmp_path)
    first = create_agent(tmp_path)
    try:
        with TestClient(create_app(first), raise_server_exceptions=False) as client:
            token, _record = _issue_token(
                client,
                name="github-recovery",
                owner="github-actions",
                scopes=FULL_SCOPES,
            )
            session = _create_session(
                client,
                token,
                "github-restart",
                idempotency_key="session:restart",
            )
            session_id = cast(str, session["id"])
            reservation_id = cast(str, session["reservation_id"])
            terminal = _create_session(
                client,
                token,
                "github-terminal-restart",
                idempotency_key="session:terminal-restart",
                required_capabilities=["serial"],
            )
            terminal_id = cast(str, terminal["id"])
            terminal_reservation_id = cast(str, terminal["reservation_id"])
            with first._database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE ci_sessions SET status = 'succeeded', outcome = 'succeeded' "
                    "WHERE id = ?",
                    (terminal_id,),
                )
    finally:
        asyncio.run(first.shutdown())

    second = create_agent(tmp_path)
    try:
        with TestClient(create_app(second), raise_server_exceptions=False) as client:
            recovered = client.get(f"/api/v1/ci/sessions/{session_id}", headers=_auth(token))
            assert recovered.status_code == 200, recovered.text
            payload = _json(recovered)
            assert payload["status"] == "completed"
            assert payload["outcome"] == "timed_out"
            assert payload["errors"] == ["AGENT_RESTARTED"]
            assert cast(JsonObject, payload["cleanup"])["reservation_released"] is True
            reservation = client.get(f"/api/v1/reservations/{reservation_id}", headers=_auth(token))
            assert reservation.status_code == 200
            assert _json(reservation)["status"] == "released"

            terminal_recovered = client.get(
                f"/api/v1/ci/sessions/{terminal_id}", headers=_auth(token)
            )
            assert terminal_recovered.status_code == 200, terminal_recovered.text
            terminal_payload = _json(terminal_recovered)
            assert terminal_payload["status"] == "completed"
            assert terminal_payload["outcome"] == "succeeded"
            assert terminal_payload["errors"] == []
            assert cast(JsonObject, terminal_payload["cleanup"])["reservation_released"] is True
            terminal_reservation = client.get(
                f"/api/v1/reservations/{terminal_reservation_id}",
                headers=_auth(token),
            )
            assert terminal_reservation.status_code == 200
            assert _json(terminal_reservation)["status"] == "released"
    finally:
        asyncio.run(second.shutdown())
