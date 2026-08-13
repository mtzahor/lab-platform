from __future__ import annotations

import importlib
import json
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lab_platform.cli.ci_exit_codes import CiExitCode
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError

cli = importlib.import_module("lab_platform.cli.main")


class ScriptedClient(AgentClient):
    """Small strict fake that keeps command coverage tests readable."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], deque[object]] = {}
        self.calls: list[tuple[str, str, object, dict[str, object]]] = []

    def queue(self, method: str, path: str, *responses: object) -> None:
        self.responses.setdefault((method, path), deque()).extend(responses)

    def _take(self, method: str, path: str) -> object:
        key = (method, path)
        if key not in self.responses or not self.responses[key]:
            raise AssertionError(f"unexpected {method} {path}")
        response = self.responses[key].popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    def get(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        self.calls.append(("GET", path, query, {"headers": headers}))
        return self._take("GET", path)

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        options: dict[str, object] = {
            "headers": headers,
            "idempotency_key": idempotency_key,
            "timeout": timeout,
        }
        self.calls.append(("POST", path, payload, options))
        return self._take("POST", path)

    def delete(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object | None:
        self.calls.append(
            (
                "DELETE",
                path,
                payload,
                {"headers": headers, "idempotency_key": idempotency_key},
            )
        )
        return self._take("DELETE", path)

    def upload(
        self,
        path: str,
        firmware_path: Path,
        *,
        owner: str,
        version: str | None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(
            (
                "UPLOAD",
                path,
                firmware_path,
                {
                    "owner": owner,
                    "version": version,
                    "headers": headers,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return self._take("UPLOAD", path)

    def upload_artifact(
        self,
        path: str,
        artifact_path: Path,
        *,
        name: str | None = None,
        artifact_type: str | None = None,
        ci_session_id: str | None = None,
        checksum: str | None = None,
        fields: Mapping[str, str | None] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append(
            (
                "UPLOAD_ARTIFACT",
                path,
                artifact_path,
                {
                    "name": name,
                    "artifact_type": artifact_type,
                    "ci_session_id": ci_session_id,
                    "checksum": checksum,
                    "fields": fields,
                    "headers": headers,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return self._take("UPLOAD_ARTIFACT", path)

    def download(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        self.calls.append(("DOWNLOAD", path, query, {"headers": headers}))
        response = self._take("DOWNLOAD", path)
        assert isinstance(response, bytes)
        return response

    def get_text(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> str:
        self.calls.append(("TEXT", path, query, {"headers": headers, "encoding": encoding}))
        response = self._take("TEXT", path)
        assert isinstance(response, str)
        return response


def _args(*values: str) -> Any:
    return cli._build_parser().parse_args(list(values))


def _distributed_reservation() -> dict[str, object]:
    return {
        "reservation": {
            "reservation_id": "reservation-1",
            "bench_id": "home-lab/bench-01",
            "owner": "alice",
        },
        "state": "ACTIVE",
        "lease": {"lease_version": 3, "valid_until": "2026-08-01T00:00:00Z"},
        "revision": 4,
    }


def test_agent_commands_cover_tables_json_and_all_mutations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    client.queue(
        "GET",
        "/api/v1/agents/agent-1",
        {
            "id": "agent-1",
            "name": "Home Agent",
            "status": "ONLINE",
            "location": "office",
            "version": "0.6.0",
            "protocol_version": "1",
            "connection": {"connected_at": "now"},
            "benches": [{"id": "bench-1"}],
            "labels": {"zone": "home"},
        },
    )
    assert cli._agent_command(client, _args("agent", "show", "agent-1")) == 0

    client.queue(
        "GET",
        "/api/v1/agents/agent-1/timeline",
        {
            "items": [
                {
                    "timestamp": "now",
                    "severity": "warning",
                    "event_type": "AGENT_OFFLINE",
                    "message": "missed heartbeat",
                }
            ]
        },
    )
    timeline = _args(
        "agent",
        "timeline",
        "agent-1",
        "--severity",
        "warning",
        "--event-type",
        "AGENT_OFFLINE",
    )
    assert cli._agent_command(client, timeline) == 0

    mutation = {"agent": {"id": "agent-1", "name": "Home Agent", "status": "DRAINING"}}
    for command, suffix in (
        ("drain", "/drain"),
        ("undrain", "/undrain"),
        ("revoke", "/revoke"),
    ):
        client.queue("POST", f"/api/v1/agents/agent-1{suffix}", mutation)
        values = ["agent", command, "agent-1"]
        if command == "drain":
            values.append("--cancel-queued-work")
        if command == "undrain":
            values.extend(("--output", "json"))
        assert cli._agent_command(client, _args(*values)) == 0

    client.queue(
        "GET",
        "/api/v1/agents/enrollment-tokens",
        {
            "items": [
                {"id": "one", "name": "available", "expires_at": "later"},
                {"id": "two", "name": "used", "used_at": "now"},
                {"id": "three", "name": "revoked", "revoked_at": "now"},
            ]
        },
    )
    assert cli._agent_command(client, _args("agent", "enrollment-token", "list")) == 0

    client.queue(
        "POST",
        "/api/v1/agents/enrollment-tokens",
        {"id": "token-4", "token": "shown-once", "expires_at": "tomorrow"},
    )
    assert (
        cli._agent_command(
            client,
            _args(
                "agent",
                "enrollment-token",
                "create",
                "--name",
                "scheduled",
                "--expires-at",
                "2026-08-01T00:00:00Z",
                "--output",
                "json",
            ),
        )
        == 0
    )

    client.queue("DELETE", "/api/v1/agents/enrollment-tokens/token-4", None)
    assert cli._agent_command(client, _args("agent", "enrollment-token", "revoke", "token-4")) == 0
    output = capsys.readouterr().out
    assert "Home Agent" in output
    assert "AGENT_OFFLINE" in output
    assert "shown-once" in output
    assert "Revoked enrollment token token-4" in output


def test_bench_actions_cover_json_success_and_remote_failure_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    client.queue("POST", "/api/v1/benches/bench-01/reservation", {"owner": "alice"})
    assert (
        cli._bench_command(
            client,
            _args("bench", "reserve", "bench-01", "--owner", "alice", "--output", "json"),
        )
        == 0
    )
    client.queue("DELETE", "/api/v1/benches/bench-01/reservation", None)
    assert (
        cli._bench_command(
            client,
            _args("bench", "release", "bench-01", "--owner", "alice", "--output", "json"),
        )
        == 0
    )

    client.queue(
        "POST",
        "/api/v1/benches/home-lab/bench-01/actions/probe",
        {"operation_id": "probe-op"},
    )
    client.queue(
        "GET",
        "/api/v1/operations/probe-op",
        {
            "id": "probe-op",
            "status": "FAILED",
            "error_code": "PROBE_FAILED",
            "error_message": "offline",
        },
    )
    probe = _args("bench", "probe", "home-lab/bench-01", "--owner", "alice")
    assert cli._bench_command(client, probe) == 7

    client.queue(
        "POST",
        "/api/v1/benches/home-lab/bench-01/actions/read-serial",
        {"operation_id": "serial-failed"},
    )
    client.queue(
        "GET",
        "/api/v1/operations/serial-failed",
        {
            "id": "serial-failed",
            "status": "FAILED",
            "error_code": "SERIAL_TIMEOUT",
            "error_message": "no output",
        },
    )
    failed_serial = _args("bench", "serial", "read", "home-lab/bench-01", "--owner", "alice")
    assert cli._bench_command(client, failed_serial) == 7

    client.queue(
        "POST",
        "/api/v1/benches/home-lab/bench-01/actions/read-serial",
        {"operation_id": "serial-ok"},
    )
    client.queue(
        "GET",
        "/api/v1/operations/serial-ok",
        {"id": "serial-ok", "status": "SUCCEEDED"},
    )
    client.queue(
        "GET",
        "/api/v1/operations/serial-ok/artifacts/serial",
        {"text": "READY\n"},
    )
    successful_serial = _args(
        "bench",
        "serial",
        "read",
        "home-lab/bench-01",
        "--owner",
        "alice",
        "--output",
        "json",
    )
    assert cli._bench_command(client, successful_serial) == 0

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    client.queue(
        "UPLOAD",
        "/api/v1/benches/home-lab/bench-01/actions/flash",
        {"operation_id": "flash-op", "status": "DISPATCHED"},
    )
    flash = _args(
        "bench",
        "flash",
        "home-lab/bench-01",
        str(firmware),
        "--owner",
        "alice",
        "--version",
        "abc123",
        "--output",
        "json",
    )
    assert cli._bench_command(client, flash) == 0
    captured = capsys.readouterr()
    assert "PROBE_FAILED" in captured.err
    assert "SERIAL_TIMEOUT" in captured.err
    assert '"serial_output": "READY\\n"' in captured.out
    assert '"filename": "firmware.bin"' in captured.out


def test_distributed_reservation_commands_cover_fencing_and_queue_outputs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    client.queue("GET", "/api/v1/reservations", {"items": [_distributed_reservation()]})
    listing = _args(
        "reservation",
        "list",
        "--state",
        "active",
        "--agent",
        "agent-1",
    )
    assert cli._reservation_command(client, listing) == 0
    assert client.calls[-1][2] == {
        "bench_id": None,
        "owner": None,
        "status": None,
        "starts_after": None,
        "starts_before": None,
        "limit": 50,
        "state": ["ACTIVE"],
        "agent_id": "agent-1",
    }

    client.queue("GET", "/api/v1/reservations/reservation-1", _distributed_reservation())
    assert cli._reservation_command(client, _args("reservation", "show", "reservation-1")) == 0

    invalid_create = _args(
        "reservation",
        "create",
        "home-lab/bench-01",
        "--owner",
        "alice",
        "--duration",
        "10m",
        "--start",
        "tomorrow",
    )
    with pytest.raises(ValueError, match="do not support --start"):
        cli._reservation_command(client, invalid_create)

    for method, path, values, output in (
        (
            "POST",
            "/api/v1/reservations/reservation-1/renew",
            (
                "reservation",
                "renew",
                "reservation-1",
                "--owner",
                "alice",
                "--expected-lease-version",
                "3",
                "--lease-ttl",
                "45s",
            ),
            "table",
        ),
        (
            "POST",
            "/api/v1/reservations/reservation-1/release",
            (
                "reservation",
                "release",
                "reservation-1",
                "--owner",
                "alice",
                "--expected-lease-version",
                "3",
                "--output",
                "json",
            ),
            "json",
        ),
        (
            "POST",
            "/api/v1/reservations/reservation-1/cancel",
            ("reservation", "cancel", "reservation-1", "--owner", "alice"),
            "table",
        ),
        (
            "POST",
            "/api/v1/reservations/reservation-1/revoke",
            (
                "reservation",
                "revoke",
                "reservation-1",
                "--expected-lease-version",
                "3",
                "--idempotency-key",
                "admin-revoke-1",
            ),
            "table",
        ),
        (
            "POST",
            "/api/v1/reservations/reservation-1/extend",
            (
                "reservation",
                "extend",
                "reservation-1",
                "--owner",
                "alice",
                "--duration",
                "5m",
            ),
            "table",
        ),
    ):
        client.queue(method, path, _distributed_reservation())
        assert cli._reservation_command(client, _args(*values)) == 0
        assert output in {"table", "json"}

    client.queue(
        "POST",
        "/api/v1/benches/home-lab/bench-01/queue",
        {"id": "queue-1", "position": 2},
    )
    queue = _args(
        "reservation",
        "queue",
        "home-lab/bench-01",
        "--owner",
        "alice",
        "--duration",
        "5m",
        "--output",
        "json",
    )
    assert cli._reservation_command(client, queue) == 0

    client.queue(
        "GET",
        "/api/v1/benches/home-lab/bench-01/queue",
        {
            "items": [
                {
                    "id": "queue-1",
                    "position": 2,
                    "owner": "alice",
                    "requested_duration_seconds": 300,
                    "status": "queued",
                }
            ]
        },
    )
    assert (
        cli._reservation_command(client, _args("reservation", "queue-list", "home-lab/bench-01"))
        == 0
    )
    client.queue("DELETE", "/api/v1/queue/queue-1", None)
    queue_cancel = _args(
        "reservation",
        "queue-cancel",
        "queue-1",
        "--owner",
        "alice",
        "--output",
        "json",
    )
    assert cli._reservation_command(client, queue_cancel) == 0
    output = capsys.readouterr().out
    assert "Lease Version" in output
    assert '"cancelled": true' in output


def test_workflow_commands_cover_registration_runs_results_and_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    definition = {
        "name": "smoke",
        "version": 2,
        "description": "Smoke test",
        "requirements": {"capabilities": ["serial"]},
        "steps": [{"name": "Probe", "action": "probe"}],
    }
    client.queue("GET", "/api/v1/workflows", {"items": [definition]})
    assert cli._workflow_command(client, _args("workflow", "list")) == 0
    client.queue("GET", "/api/v1/workflows/smoke", definition)
    assert cli._workflow_command(client, _args("workflow", "show", "smoke")) == 0

    definition_path = tmp_path / "workflow.yaml"
    definition_path.write_text(
        "name: smoke\nversion: 2\nsteps:\n  - action: probe\n", encoding="utf-8"
    )
    client.queue("POST", "/api/v1/workflows", definition)
    register = _args("workflow", "register", str(definition_path), "--output", "json")
    assert cli._workflow_command(client, register) == 0
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML or JSON mapping"):
        cli._workflow_command(client, _args("workflow", "register", str(invalid_path)))

    client.queue(
        "POST",
        "/api/v1/workflows/smoke/runs",
        {
            "id": "workflow-run-1",
            "agent": {"id": "agent-1", "name": "Home Agent"},
            "bench": {"id": "bench-01"},
        },
    )
    local_run = _args(
        "workflow",
        "run",
        "smoke",
        "--bench",
        "bench-01",
        "--owner",
        "alice",
        "--input",
        "expected=ready",
    )
    assert cli._workflow_command(client, local_run) == 0

    invalid_distributed = _args(
        "workflow",
        "run",
        "smoke",
        "--owner",
        "alice",
        "--reservation-id",
        "reservation-1",
    )
    with pytest.raises(ValueError, match="own their lease"):
        cli._workflow_command(client, invalid_distributed)

    client.queue(
        "POST",
        "/api/v1/workflows/smoke/runs",
        {"operation": {"id": "operation-1"}, "status": "CREATED"},
    )
    distributed_run = _args(
        "workflow",
        "run",
        "smoke",
        "--owner",
        "alice",
        "--kind",
        "simulated",
        "--output",
        "json",
    )
    assert cli._workflow_command(client, distributed_run) == 0

    client.queue(
        "POST",
        "/api/v1/workflow-runs/operation-1/cancel",
        {"operation_id": "operation-1", "cancellation_requested": True},
    )
    cancel = _args(
        "workflow",
        "cancel",
        "operation-1",
        "--owner",
        "alice",
        "--output",
        "json",
    )
    assert cli._workflow_command(client, cancel) == 0

    junit_path = tmp_path / "results" / "junit.xml"
    client.queue(
        "TEXT",
        "/api/v1/workflow-runs/operation-1/results/junit",
        "<testsuite tests='1'/>\n",
        "<testsuite tests='1'/>\n",
    )
    junit_file = _args(
        "workflow",
        "results",
        "operation-1",
        "--format",
        "junit",
        "--output",
        str(junit_path),
    )
    assert cli._workflow_command(client, junit_file) == 0
    assert junit_path.read_text(encoding="utf-8") == "<testsuite tests='1'/>\n"
    junit_stdout = _args("workflow", "results", "operation-1", "--format", "junit")
    assert cli._workflow_command(client, junit_stdout) == 0

    results = {"operation_id": "operation-1", "results": [{"status": "passed"}]}
    result_path = tmp_path / "results.json"
    client.queue(
        "GET",
        "/api/v1/workflow-runs/operation-1/results",
        results,
        results,
    )
    json_file = _args("workflow", "results", "operation-1", "--output", str(result_path))
    assert cli._workflow_command(client, json_file) == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == results
    assert cli._workflow_command(client, _args("workflow", "results", "operation-1")) == 0

    client.queue(
        "GET",
        "/api/v1/workflow-runs/operation-1",
        {"id": "operation-1", "status": "RUNNING", "current_step": 0},
        {"id": "operation-1", "status": "FAILED", "current_step": 1},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    watch = _args("workflow", "watch", "operation-1", "--interval", "0")
    assert cli._workflow_command(client, watch) == 7
    output = capsys.readouterr().out
    assert "Home Agent" in output
    assert "Bench: bench-01" in output
    assert "Workflow status: Failed" in output


def test_token_and_operation_commands_cover_table_json_and_terminal_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    token = {
        "id": "token-1",
        "name": "ci",
        "owner": "automation",
        "token": "secret-once",
    }
    client.queue("POST", "/api/v1/tokens", token, token)
    create = _args(
        "token",
        "create",
        "--name",
        "ci",
        "--owner",
        "automation",
        "--scope",
        "operations:write",
    )
    assert cli._token_command(client, create) == 0
    create_json = _args(
        "token",
        "create",
        "--name",
        "ci",
        "--owner",
        "automation",
        "--scope",
        "operations:write",
        "--output",
        "json",
    )
    assert cli._token_command(client, create_json) == 0
    client.queue(
        "GET",
        "/api/v1/tokens",
        {
            "items": [
                {"id": "token-1", "name": "ci", "owner": "automation"},
                {
                    "id": "token-2",
                    "name": "old",
                    "owner": "automation",
                    "revoked_at": "now",
                },
            ]
        },
    )
    assert cli._token_command(client, _args("token", "list")) == 0
    client.queue("POST", "/api/v1/tokens/token-1/revoke", {"id": "token-1"})
    assert cli._token_command(client, _args("token", "revoke", "token-1")) == 0

    operation = {
        "id": "operation-1",
        "bench_id": "home-lab/bench-01",
        "type": "PROBE",
        "status": "SUCCEEDED",
        "progress": 100,
        "message": "done",
    }
    client.queue("GET", "/api/v1/operations/operation-1", operation)
    assert cli._operation_command(client, _args("operation", "show", "operation-1")) == 0
    client.queue("GET", "/api/v1/operations", {"items": [operation]})
    operation_list = _args(
        "operation",
        "list",
        "--bench-id",
        "home-lab/bench-01",
        "--status",
        "SUCCEEDED",
        "--type",
        "PROBE",
    )
    assert cli._operation_command(client, operation_list) == 0
    client.queue(
        "POST",
        "/api/v1/operations/operation-1/reconcile",
        {"request_id": "reconcile-1"},
    )
    reconcile = _args("operation", "reconcile", "operation-1", "--output", "json")
    assert cli._operation_command(client, reconcile) == 0
    client.queue(
        "POST",
        "/api/v1/operations/operation-1/cancel",
        {"operation_id": "operation-1", "cancellation_requested": True},
    )
    cancel = _args("operation", "cancel", "operation-1", "--owner", "alice")
    assert cli._operation_command(client, cancel) == 0
    client.queue(
        "GET",
        "/api/v1/operations/operation-1",
        {**operation, "status": "RUNNING", "progress": 50, "message": "probing"},
        {**operation, "status": "CANCELLED", "message": "cancelled"},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    watch = _args("operation", "watch", "operation-1", "--interval", "0")
    assert cli._operation_command(client, watch) == 7
    output = capsys.readouterr().out
    assert "secret-once" in output
    assert "Operation-1" not in output
    assert "Status: Cancelled" in output


def test_ci_artifact_and_session_commands_cover_upload_list_download_and_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    client.queue(
        "UPLOAD_ARTIFACT",
        "/api/v1/artifacts",
        {
            "id": "artifact-1",
            "name": "firmware.bin",
            "artifact_type": "firmware",
            "size_bytes": 8,
            "sha256": "abc",
        },
    )
    upload = _args("ci", "upload", "session-1", str(firmware))
    assert cli._ci_command(client, upload) == 0

    artifacts = {
        "items": [
            {
                "id": "artifact-1",
                "name": "firmware.bin",
                "artifact_type": "firmware",
                "size_bytes": 8,
                "sha256": "abc",
            }
        ]
    }
    client.queue("GET", "/api/v1/ci/sessions/session-1/artifacts", artifacts, artifacts)
    assert cli._ci_command(client, _args("ci", "artifacts", "session-1")) == 0
    assert cli._ci_command(client, _args("ci", "artifacts", "session-1", "--output", "json")) == 0
    destination = tmp_path / "downloads" / "firmware.bin"
    client.queue("DOWNLOAD", "/api/v1/artifacts/artifact-1/content", b"downloaded")
    download = _args("ci", "download", "artifact-1", "--output", str(destination))
    assert cli._ci_command(client, download) == 0
    assert destination.read_bytes() == b"downloaded"

    session = {
        "id": "session-1",
        "status": "completed",
        "outcome": "succeeded",
        "bench_id": "bench-01",
        "workflow_run_id": "workflow-1",
        "cleanup_status": "succeeded",
    }
    client.queue("POST", "/api/v1/ci/sessions", session)
    create = _args(
        "ci",
        "session",
        "create",
        "--external-run-id",
        "run-1",
        "--repository",
        "owner/repo",
        "--ref",
        "main",
        "--commit-sha",
        "abc",
        "--actor",
        "bot",
        "--output",
        "json",
    )
    assert cli._ci_session_command(client, create) == 0
    client.queue("GET", "/api/v1/ci/sessions/session-1", session)
    assert cli._ci_session_command(client, _args("ci", "session", "show", "session-1")) == 0

    current = {**session, "status": "running", "cleanup_timeout_seconds": 2}
    cancelled = {**session, "status": "cancelled", "outcome": "cancelled"}
    client.queue("GET", "/api/v1/ci/sessions/session-1", current)
    client.queue("POST", "/api/v1/ci/sessions/session-1/cancel", cancelled)
    client.queue("POST", "/api/v1/ci/sessions/session-1/finalize", session)
    cancel = _args("ci", "session", "cancel", "session-1", "--output", "json")
    assert cli._ci_session_command(client, cancel) == int(CiExitCode.SUCCESS)

    client.queue("GET", "/api/v1/ci/sessions/session-1", current)
    client.queue("POST", "/api/v1/ci/sessions/session-1/finalize", session)
    finalize = _args("ci", "session", "finalize", "session-1")
    assert cli._ci_session_command(client, finalize) == int(CiExitCode.SUCCESS)

    monkeypatch.setattr(cli, "_cancel_and_finalize_ci_session", lambda *_args, **_kwargs: None)
    client.queue("GET", "/api/v1/ci/sessions/session-2", current)
    no_response = _args("ci", "session", "cancel", "session-2")
    assert cli._ci_session_command(client, no_response) == int(CiExitCode.WORKFLOW_CANCELLED)
    output = capsys.readouterr().out
    assert "Artifact uploaded: artifact-1" in output
    assert "Downloaded artifact" in output


def test_ci_assignment_and_distributed_watch_validate_every_server_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _args(
        "ci",
        "run",
        "--workflow",
        "smoke",
        "--interval",
        "0.01",
        "--wait-timeout",
        "1s",
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    completed_client = ScriptedClient()
    completed = {"id": "session-1", "status": "completed", "outcome": "succeeded"}
    completed_client.queue("GET", "/api/v1/ci/sessions/session-1", completed)
    assert cli._wait_for_ci_assignment(completed_client, "session-1", args) == completed

    failed_client = ScriptedClient()
    failed_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-1",
        {"id": "session-1", "status": "failed"},
    )
    failed_client.queue("POST", "/api/v1/ci/sessions/session-1/finalize", completed)
    assert cli._wait_for_ci_assignment(failed_client, "session-1", args) == completed

    unexpected_client = ScriptedClient()
    unexpected_client.queue(
        "GET", "/api/v1/ci/sessions/session-1", {"id": "session-1", "status": "mystery"}
    )
    with pytest.raises(ValueError, match="unexpected CI session status"):
        cli._wait_for_ci_assignment(unexpected_client, "session-1", args)

    timeout_client = ScriptedClient()
    timeout_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-1",
        {"id": "session-1", "status": "waiting_for_bench"},
    )
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    with pytest.raises(cli._CiBenchWaitTimeout, match="compatible bench"):
        cli._wait_for_ci_assignment(timeout_client, "session-1", args)

    nonretry_client = ScriptedClient()
    nonretry_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/run",
        AgentApiError(409, "BAD_REQUEST", "bad"),
    )
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
    with pytest.raises(AgentApiError, match="bad"):
        cli._start_distributed_ci_workflow(
            nonretry_client,
            "session-1",
            {"workflow_name": "smoke"},
            idempotency_key="run-1",
            args=args,
        )

    terminal_client = ScriptedClient()
    terminal_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/run",
        AgentApiError(409, "AGENT_OFFLINE", "offline"),
    )
    terminal_client.queue("GET", "/api/v1/ci/sessions/session-1", completed)
    assert (
        cli._start_distributed_ci_workflow(
            terminal_client,
            "session-1",
            {"workflow_name": "smoke"},
            idempotency_key="run-1",
            args=args,
        )
        == completed
    )

    expired_client = ScriptedClient()
    expired_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/run",
        AgentApiError(409, "AGENT_OFFLINE", "offline"),
    )
    expired_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-1",
        {"id": "session-1", "status": "waiting_for_bench"},
    )
    expired_clock = iter((0.0, 2.0))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(expired_clock))
    with pytest.raises(cli._CiBenchWaitTimeout, match="distributed bench"):
        cli._start_distributed_ci_workflow(
            expired_client,
            "session-1",
            {"workflow_name": "smoke"},
            idempotency_key="run-1",
            args=args,
        )

    missing_client = ScriptedClient()
    missing_client.queue("GET", "/api/v1/ci/sessions/session-1", {"id": "session-1"})
    with pytest.raises(ValueError, match="without a status"):
        cli._watch_distributed_ci_workflow(missing_client, "session-1", args)

    watch_client = ScriptedClient()
    watch_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-1",
        {"id": "session-1", "status": "running", "operation_id": "operation-1"},
        completed,
    )
    watch_client.queue(
        "GET",
        "/api/v1/operations/operation-1",
        {"id": "operation-1", "progress": 50, "message": "probing"},
    )
    watch_client.queue("POST", "/api/v1/ci/sessions/session-1/heartbeat", {"status": "running"})
    assert cli._watch_distributed_ci_workflow(watch_client, "session-1", args) == completed

    mystery_client = ScriptedClient()
    mystery_client.queue(
        "GET", "/api/v1/ci/sessions/session-1", {"id": "session-1", "status": "mystery"}
    )
    with pytest.raises(ValueError, match="unexpected CI status"):
        cli._watch_distributed_ci_workflow(mystery_client, "session-1", args)
    output = capsys.readouterr().out
    assert "Waiting For Bench" in output
    assert "Remote workflow: Running (50%) — probing" in output


def test_ci_cleanup_workflow_watch_progress_and_cancellation_error_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _args("ci", "run", "--workflow", "smoke", "--interval", "0.01", "--output", "table")
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)

    cleanup_client = ScriptedClient()
    cleanup_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/finalize",
        {"id": "session-1", "status": "failed"},
    )
    with pytest.raises(ValueError, match="unexpected cleanup status"):
        cli._finalize_distributed_ci_session(
            cleanup_client,
            "session-1",
            session={},
            idempotency_key="finalize-1",
            poll_interval=0.01,
        )

    timed_out_client = ScriptedClient()
    timed_out_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/finalize",
        {"id": "session-1", "status": "cleanup_pending"},
    )
    timeout_clock = iter((0.0, 71.0))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(timeout_clock))
    with pytest.raises(AgentConnectionError, match="artifact finalization"):
        cli._finalize_distributed_ci_session(
            timed_out_client,
            "session-1",
            session={},
            idempotency_key="finalize-1",
            poll_interval=0.01,
        )

    missing_run_client = ScriptedClient()
    missing_run_client.queue(
        "GET", "/api/v1/workflow-runs/workflow-1", {"id": "workflow-1", "steps": []}
    )
    with pytest.raises(ValueError, match="without a status"):
        cli._watch_ci_workflow(missing_run_client, "session-1", "workflow-1", args)

    success_client = ScriptedClient()
    running_session = {"id": "session-1", "status": "running"}
    terminal_session = {"id": "session-1", "status": "succeeded", "outcome": "succeeded"}
    success_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-1",
        {"id": "workflow-1", "status": "running", "steps": []},
        {"id": "workflow-1", "status": "succeeded", "steps": []},
    )
    success_client.queue("GET", "/api/v1/ci/sessions/session-1", running_session, terminal_session)
    success_client.queue("POST", "/api/v1/ci/sessions/session-1/heartbeat", {"status": "running"})
    assert (
        cli._watch_ci_workflow(success_client, "session-1", "workflow-1", args) == terminal_session
    )

    session_terminal_client = ScriptedClient()
    session_terminal_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-1",
        {"id": "workflow-1", "status": "running", "steps": []},
    )
    session_terminal_client.queue("GET", "/api/v1/ci/sessions/session-1", terminal_session)
    assert (
        cli._watch_ci_workflow(session_terminal_client, "session-1", "workflow-1", args)
        == terminal_session
    )

    invalid_run_client = ScriptedClient()
    invalid_run_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-1",
        {"id": "workflow-1", "status": "mystery", "steps": []},
    )
    invalid_run_client.queue("GET", "/api/v1/ci/sessions/session-1", running_session)
    with pytest.raises(ValueError, match="unexpected workflow status"):
        cli._watch_ci_workflow(invalid_run_client, "session-1", "workflow-1", args)

    previous: dict[int, str] = {3: "running"}
    cli._print_ci_progress(
        [
            {"step_index": 0, "name": "Pending", "status": "pending"},
            {"step_index": 1, "action": "probe", "status": "running"},
            {"name": "Unnamed", "status": ""},
            {"step_index": 3, "name": "Duplicate", "status": "running"},
        ],
        previous,
    )
    assert previous == {0: "pending", 1: "running", 3: "running"}

    monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
    cancel_error_client = ScriptedClient()
    cancel_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/cancel",
        AgentConnectionError("cancel offline"),
    )
    cancel_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/finalize",
        {"id": "session-1", "status": "completed"},
    )
    assert cli._cancel_and_finalize_ci_session(
        cancel_error_client, "session-1", best_effort=True
    ) == {"id": "session-1", "status": "completed"}

    poll_error_client = ScriptedClient()
    poll_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/cancel",
        {"id": "session-1", "status": "cancel_requested"},
    )
    poll_error_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-1",
        AgentConnectionError("poll offline"),
    )
    poll_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/finalize",
        {"id": "session-1", "status": "completed"},
    )
    ticking = iter((0.0, 0.1))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticking))
    assert cli._cancel_and_finalize_ci_session(poll_error_client, "session-1") == {
        "id": "session-1",
        "status": "completed",
    }

    finalize_error_client = ScriptedClient()
    finalize_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/cancel",
        {"id": "session-1", "status": "cancelled"},
    )
    finalize_error_client.queue(
        "POST",
        "/api/v1/ci/sessions/session-1/finalize",
        AgentApiError(500, "FINALIZE_FAILED", "cleanup failed"),
    )
    monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
    with pytest.raises(AgentApiError, match="cleanup failed"):
        cli._cancel_and_finalize_ci_session(finalize_error_client, "session-1")
    assert "Workflow: Running" in capsys.readouterr().out


def test_ci_output_downloads_cover_junit_duplicates_and_best_effort_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    artifacts = [
        {"id": "junit-1", "name": "results.xml", "artifact_type": "junit"},
        {"id": "abcdefgh-1", "name": "serial.log", "artifact_type": "serial"},
        {"id": "abcdefgh-2", "name": "serial.log", "artifact_type": "serial"},
        {"id": "abcdefgh-3", "name": "serial.log", "artifact_type": "serial"},
    ]
    client.queue("GET", "/api/v1/ci/sessions/session-1/artifacts", {"items": artifacts})
    client.queue("DOWNLOAD", "/api/v1/artifacts/junit-1/content", b"<testsuite/>\n", b"junit")
    client.queue("DOWNLOAD", "/api/v1/artifacts/abcdefgh-1/content", b"one")
    client.queue("DOWNLOAD", "/api/v1/artifacts/abcdefgh-2/content", b"two")
    client.queue("DOWNLOAD", "/api/v1/artifacts/abcdefgh-3/content", b"three")
    junit = tmp_path / "junit.xml"
    directory = tmp_path / "artifacts"
    downloaded = cli._download_ci_outputs(
        client,
        {"id": "session-1"},
        workflow_run_id=None,
        junit_output=junit,
        artifacts_directory=directory,
        output="table",
    )
    assert junit.read_bytes() == b"<testsuite/>\n"
    assert downloaded == [
        "results.xml",
        "serial.log",
        "serial-abcdefgh.log",
        "serial-abcdefgh-2.log",
    ]
    assert (directory / "serial-abcdefgh-2.log").read_bytes() == b"three"

    missing_session = cli._download_ci_outputs(
        ScriptedClient(),
        {},
        workflow_run_id=None,
        junit_output=tmp_path / "missing.xml",
        artifacts_directory=None,
        output="json",
        best_effort=True,
    )
    assert missing_session == []

    listing_error_client = ScriptedClient()
    listing_error_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-2/artifacts",
        AgentConnectionError("listing offline"),
    )
    assert (
        cli._download_ci_outputs(
            listing_error_client,
            {"id": "session-2"},
            workflow_run_id=None,
            junit_output=tmp_path / "missing-2.xml",
            artifacts_directory=None,
            output="json",
            best_effort=True,
        )
        == []
    )

    no_junit_client = ScriptedClient()
    no_junit_client.queue("GET", "/api/v1/ci/sessions/session-3/artifacts", {"items": []})
    with pytest.raises(ValueError, match="did not provide a JUnit artifact"):
        cli._download_ci_outputs(
            no_junit_client,
            {"id": "session-3"},
            workflow_run_id=None,
            junit_output=tmp_path / "missing-3.xml",
            artifacts_directory=None,
            output="json",
        )

    bad_junit_client = ScriptedClient()
    bad_junit_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-4/artifacts",
        {"items": [{"name": "junit.xml", "artifact_type": "junit"}]},
    )
    assert (
        cli._download_ci_outputs(
            bad_junit_client,
            {"id": "session-4"},
            workflow_run_id=None,
            junit_output=tmp_path / "missing-4.xml",
            artifacts_directory=None,
            output="json",
            best_effort=True,
        )
        == []
    )

    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("file", encoding="utf-8")
    blocked_client = ScriptedClient()
    blocked_client.queue("GET", "/api/v1/ci/sessions/session-5/artifacts", {"items": []})
    assert (
        cli._download_ci_outputs(
            blocked_client,
            {"id": "session-5"},
            workflow_run_id=None,
            junit_output=None,
            artifacts_directory=blocked_directory,
            output="json",
            best_effort=True,
        )
        == []
    )

    bad_artifacts_client = ScriptedClient()
    bad_artifacts_client.queue(
        "GET",
        "/api/v1/ci/sessions/session-6/artifacts",
        {
            "items": [
                {"name": "no-id.log"},
                {"id": "unsafe", "name": "../unsafe.log"},
                {"id": "offline", "name": "offline.log"},
            ]
        },
    )
    bad_artifacts_client.queue(
        "DOWNLOAD",
        "/api/v1/artifacts/offline/content",
        AgentConnectionError("download offline"),
    )
    assert (
        cli._download_ci_outputs(
            bad_artifacts_client,
            {"id": "session-6"},
            workflow_run_id=None,
            junit_output=None,
            artifacts_directory=tmp_path / "bad-artifacts",
            output="table",
            best_effort=True,
        )
        == []
    )
    captured = capsys.readouterr()
    assert "serial-abcdefgh-2.log" in captured.out
    assert "warning: diagnostics download incomplete" in captured.err


def test_ci_exit_mapping_and_result_diagnostics_cover_failure_precedence() -> None:
    assert cli._ci_exit_for_session({"status": "cancelled", "outcome": "pending"}) == (
        CiExitCode.WORKFLOW_CANCELLED
    )
    assert cli._ci_exit_for_session({"status": "failed", "outcome": "infrastructure_error"}) == (
        CiExitCode.BACKEND_UNAVAILABLE
    )
    assert cli._ci_exit_for_session({"status": "mystery", "outcome": "mystery"}) == (
        CiExitCode.CLIENT_OR_PROTOCOL_ERROR
    )
    assert (
        cli._ci_exit_for_session({"status": "failed", "error_code": "NO_COMPATIBLE_BENCH"})
        == CiExitCode.NO_COMPATIBLE_BENCH
    )

    coded_client = ScriptedClient()
    coded_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-coded",
        {"error_code": "HARDWARE_TEST_FAILED"},
    )
    assert cli._workflow_failure_exit_code(coded_client, "workflow-coded") == (
        CiExitCode.HARDWARE_TEST_FAILED
    )

    error_result_client = ScriptedClient()
    error_result_client.queue(
        "GET", "/api/v1/workflow-runs/workflow-error", {"error_code": "UNKNOWN"}
    )
    error_result_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-error/results",
        {"results": [{"status": "error"}]},
    )
    assert cli._workflow_failure_exit_code(error_result_client, "workflow-error") == (
        CiExitCode.WORKFLOW_FAILED
    )

    passing_client = ScriptedClient()
    passing_client.queue("GET", "/api/v1/workflow-runs/workflow-pass", {})
    passing_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-pass/results",
        {"results": [{"status": "passed"}]},
    )
    assert cli._workflow_failure_exit_code(passing_client, "workflow-pass") is None

    unavailable_client = ScriptedClient()
    unavailable_client.queue(
        "GET",
        "/api/v1/workflow-runs/workflow-offline",
        AgentConnectionError("offline"),
    )
    assert cli._workflow_failure_exit_code(unavailable_client, "workflow-offline") is None

    assert cli._ci_session_error_code({"error_code": "EXPLICIT"}) == "EXPLICIT"
    assert cli._ci_session_error_code({"errors": [42, "not a code", "VALID_CODE: details"]}) == (
        "VALID_CODE"
    )
    assert cli._ci_session_error_code({"errors": [42, "not a code"]}) is None


def test_cli_error_dispatch_server_and_formatting_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch = cli._dispatch

    def interrupt(_args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", interrupt)
    assert cli.main(["version"]) == int(CiExitCode.WORKFLOW_CANCELLED)

    def invalid(_args: object) -> int:
        raise ValueError("bad input")

    monkeypatch.setattr(cli, "_dispatch", invalid)
    assert cli.main(["ci", "artifacts", "session-1"]) == int(CiExitCode.CLIENT_OR_PROTOCOL_ERROR)
    captured = capsys.readouterr()
    assert "Hardware CI cancelled" in captured.err
    assert "bad input" in captured.err

    client = ScriptedClient()
    monkeypatch.setattr(cli, "_client", lambda _args: client)
    client.queue("GET", "/api/v1/version", {"version": "0.6.0"})
    assert dispatch(_args("version", "--output", "json")) == 0
    client.queue("GET", "/plugins", {"plugins": ["simlab"]})
    assert dispatch(_args("plugins")) == 0
    client.queue("GET", "/api/v1/benches", {"items": []})
    assert dispatch(_args("benches")) == 0
    client.queue("GET", "/api/v1/agents", {"items": []})
    assert dispatch(_args("agent", "list", "--output", "json")) == 0
    with pytest.raises(AssertionError, match="unreachable command"):
        dispatch(SimpleNamespace(command="unknown"))

    monkeypatch.setenv("LAB_PLATFORM_SERVER", "https://environment.example")
    assert cli._resolve_server(SimpleNamespace(server=None, config=tmp_path / "none")) == (
        "https://environment.example"
    )
    monkeypatch.delenv("LAB_PLATFORM_SERVER")
    direct_config = tmp_path / "direct.yaml"
    direct_config.write_text("server: https://direct.example\n", encoding="utf-8")
    assert cli._resolve_server(SimpleNamespace(server=None, config=direct_config)) == (
        "https://direct.example"
    )
    nested_config = tmp_path / "nested.yaml"
    nested_config.write_text("cli:\n  server: https://nested.example\n", encoding="utf-8")
    assert cli._resolve_server(SimpleNamespace(server=None, config=nested_config)) == (
        "https://nested.example"
    )

    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        cli._validate_firmware(empty)
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"large")
    monkeypatch.setattr(cli, "DEFAULT_MAX_FIRMWARE_BYTES", 1)
    with pytest.raises(ValueError, match="100 MB"):
        cli._validate_firmware(oversized)

    assert cli._api_exit_code(AgentApiError(418, "TEAPOT", "tea")) == 1
    with pytest.raises(ValueError, match="key=value"):
        cli._key_value("invalid", "requirement")
    with pytest.raises(ValueError, match="unsafe artifact"):
        cli._safe_artifact_filename("../secret")

    github_output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    cli._append_github_outputs({"single": "value", "multiline": "one\ntwo"})
    github_text = github_output.read_text(encoding="utf-8")
    assert "single=value" in github_text
    assert "multiline<<lab_platform_" in github_text

    assert cli._parse_ci_datetime("not-a-date") is None
    naive = cli._parse_ci_datetime("2026-07-29T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    with pytest.raises(ValueError, match="greater than zero"):
        cli._ci_poll_interval(SimpleNamespace(interval=0))
    assert cli._ci_configured_seconds({"timeout": True}, "timeout", default=4.0) == 4.0

    cli._print_collection({"items": []}, "json", cli._agent_table)
    cli._print_operation_created({"operation_id": "operation-2"}, "json")
    cli._health_table(
        {
            "status": "healthy",
            "version": "0.6.0",
            "backend": "real",
            "database": "connected",
            "benches": {"online": 1, "total": 2},
        }
    )
    cli._agent_table(
        [
            {
                "id": "agent-1",
                "name": "Home Agent",
                "status": "ONLINE",
                "location": "office",
                "version": "0.6.0",
                "bench_count": 1,
            }
        ]
    )
    cli._bench_table(
        [
            {
                "id": "home-lab/bench-01",
                "agent_id": "agent-1",
                "agent_slug": "home-lab",
                "status": "ONLINE",
                "kind": "SIMULATED",
                "health": "HEALTHY",
                "firmware_version": "abc123",
            }
        ]
    )
    assert "home-lab/bench-01" in capsys.readouterr().out


def test_operation_json_cancel_wait_and_internal_guard_branches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = ScriptedClient()
    client.queue(
        "POST",
        "/api/v1/operations/operation-1/cancel",
        {"operation_id": "operation-1", "cancellation_requested": True},
    )
    cancel = _args(
        "operation",
        "cancel",
        "operation-1",
        "--owner",
        "alice",
        "--output",
        "json",
    )
    assert cli._operation_command(client, cancel) == 0

    client.queue(
        "GET",
        "/api/v1/operations/operation-2",
        {"id": "operation-2", "status": "RUNNING"},
        {"id": "operation-2", "status": "SUCCEEDED"},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    assert cli._wait_for_terminal(client, "operation-2", interval=0) == {
        "id": "operation-2",
        "status": "SUCCEEDED",
    }

    for function, namespace in (
        (cli._agent_command, SimpleNamespace(agent_command="unknown", agent_id="agent-1")),
        (
            cli._agent_enrollment_token_command,
            SimpleNamespace(enrollment_token_command="unknown"),
        ),
        (cli._reservation_command, SimpleNamespace(reservation_command="unknown")),
        (cli._workflow_command, SimpleNamespace(workflow_command="unknown")),
        (cli._token_command, SimpleNamespace(token_command="unknown")),
        (cli._ci_command, SimpleNamespace(ci_command="unknown")),
        (cli._ci_session_command, SimpleNamespace(ci_session_command="unknown", session_id="one")),
        (cli._operation_command, SimpleNamespace(operation_command="unknown")),
    ):
        with pytest.raises(AssertionError, match="unreachable"):
            function(client, namespace)
    assert '"cancellation_requested": true' in capsys.readouterr().out
