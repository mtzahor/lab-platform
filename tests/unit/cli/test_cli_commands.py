from __future__ import annotations

import importlib
import json
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent import create_agent, create_app
from lab_platform.cli import product
from lab_platform.cli.client import AgentApiError, AgentClient
from lab_platform.cli.main import main, parse_duration
from lab_platform.core import VERSION

cli = importlib.import_module("lab_platform.cli.main")


agent_cli = importlib.import_module("lab_platform.agent.cli")


class RecordingClient(AgentClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.response: object = {}

    def get(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        self.calls.append(("GET", path, query))
        return self.response

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append(
            (
                "POST",
                path,
                {"payload": payload, "idempotency_key": idempotency_key},
            )
        )
        return self.response

    def delete(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> object | None:
        self.calls.append(("DELETE", path, payload))
        return None


def test_agent_admin_commands_use_phase5_routes(capsys: pytest.CaptureFixture[str]) -> None:
    client = RecordingClient()
    client.response = {"items": []}
    args = cli._build_parser().parse_args(
        [
            "agent",
            "list",
            "--status",
            "online",
            "--location",
            "office",
            "--label",
            "environment=development",
            "--version",
            "0.6.0-alpha",
        ]
    )
    assert cli._agent_command(client, args) == 0
    assert client.calls == [
        (
            "GET",
            "/api/v1/agents",
            {
                "status": "ONLINE",
                "location": "office",
                "label": ["environment=development"],
                "version": "0.6.0-alpha",
            },
        )
    ]

    client.calls.clear()
    client.response = {"request_id": "refresh-1"}
    refresh = cli._build_parser().parse_args(["agent", "refresh", "agent-1"])
    assert cli._agent_command(client, refresh) == 0
    assert client.calls == [
        (
            "POST",
            "/api/v1/agents/agent-1/actions/refresh-inventory",
            {"payload": {}, "idempotency_key": None},
        )
    ]
    assert "refresh-1" in capsys.readouterr().out


def test_distributed_ci_run_routes_before_assignment_and_uses_central_artifact_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_path = tmp_path / "firmware.bin"
    artifact_path.write_bytes(b"phase-five-firmware")

    class DistributedCiClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.upload_fields: Mapping[str, str | None] | None = None
            self.run_attempts = 0
            self.finalize_attempts = 0

        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            headers: Mapping[str, str] | None = None,
            idempotency_key: str | None = None,
            timeout: float | None = None,
        ) -> object:
            del headers, timeout
            self.calls.append(
                ("POST", path, {"payload": payload, "idempotency_key": idempotency_key})
            )
            if path == "/api/v1/ci/sessions":
                return {
                    "id": "session-1",
                    "status": "waiting_for_bench",
                    "outcome": "pending",
                    "distributed_workflow": None,
                    "heartbeat_interval_seconds": 3600,
                }
            if path.endswith("/run"):
                self.run_attempts += 1
                return {
                    "id": "session-1",
                    "status": "running",
                    "outcome": "pending",
                    "bench_id": "home-lab/bench-01",
                    "operation_id": "operation-1",
                    "distributed_workflow": {"remote_command_id": "command-1"},
                }
            if path.endswith("/finalize"):
                self.finalize_attempts += 1
                if self.finalize_attempts == 1:
                    return {
                        "id": "session-1",
                        "status": "cleanup_pending",
                        "outcome": "succeeded",
                        "cleanup_status": "pending",
                        "distributed_workflow": {"remote_command_id": "command-1"},
                    }
                return {
                    "id": "session-1",
                    "status": "completed",
                    "outcome": "succeeded",
                    "bench_id": "home-lab/bench-01",
                    "cleanup_status": "succeeded",
                    "distributed_workflow": {"remote_command_id": "command-1"},
                }
            return {"id": "session-1", "status": "running"}

        def get(
            self,
            path: str,
            query: dict[str, object] | None = None,
            *,
            headers: Mapping[str, str] | None = None,
        ) -> object:
            del query, headers
            self.calls.append(("GET", path, None))
            if path == "/api/v1/operations/operation-1":
                return {"id": "operation-1", "progress": 100, "message": "passed"}
            return {
                "id": "session-1",
                "status": "succeeded",
                "outcome": "succeeded",
                "bench_id": "home-lab/bench-01",
                "operation_id": "operation-1",
                "distributed_workflow": {"remote_command_id": "command-1"},
            }

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
            del name, artifact_type, ci_session_id, checksum, headers, idempotency_key
            assert path == "/api/v1/artifacts"
            assert artifact_path.is_file()
            self.upload_fields = fields
            return {"id": "artifact-1"}

    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    for name in ("GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "CI"):
        monkeypatch.delenv(name, raising=False)
    args = cli._build_parser().parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "esp32-ci",
            "--artifact",
            f"firmware={artifact_path}",
            "--interval",
            "0.001",
            "--output",
            "json",
        ]
    )
    client = DistributedCiClient()

    assert cli._ci_run(client, args) == 0
    assert client.run_attempts == 1
    assert client.finalize_attempts == 2
    assert client.upload_fields is not None
    assert client.upload_fields["owner_type"] == "ci_session"
    assert client.upload_fields["owner_id"] == "session-1"
    assert client.upload_fields["expected_sha256"]
    assert "completed" in capsys.readouterr().out


def test_distributed_ci_start_retries_only_central_routing_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            headers: Mapping[str, str] | None = None,
            idempotency_key: str | None = None,
            timeout: float | None = None,
        ) -> object:
            del headers, idempotency_key, timeout
            if path.endswith("/run"):
                self.attempts += 1
                if self.attempts == 1:
                    raise AgentApiError(409, "NO_COMPATIBLE_BENCH", "none online")
                return {"id": "session-1", "status": "running", "operation_id": "op-1"}
            return {"id": "session-1", "status": "waiting_for_bench"}

        def get(
            self,
            path: str,
            query: dict[str, object] | None = None,
            *,
            headers: Mapping[str, str] | None = None,
        ) -> object:
            del path, query, headers
            return {"id": "session-1", "status": "waiting_for_bench"}

    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    args = cli._build_parser().parse_args(
        ["ci", "run", "--workflow", "workflow", "--interval", "0.001"]
    )
    client = RetryClient()

    result = cli._start_distributed_ci_workflow(
        client,
        "session-1",
        {"workflow_name": "workflow", "inputs": {}},
        idempotency_key="workflow-key",
        args=args,
    )

    assert result["operation_id"] == "op-1"
    assert client.attempts == 2


def test_agent_enrollment_token_create_and_revoke_are_explicit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = RecordingClient()
    client.response = {
        "id": "token-1",
        "expires_at": "2026-07-28T12:30:00Z",
        "token": "secret-once",
    }
    create = cli._build_parser().parse_args(
        [
            "agent",
            "enrollment-token",
            "create",
            "--name",
            "home-lab",
            "--expires-in",
            "30m",
            "--allowed-label",
            "environment=development",
        ]
    )
    assert cli._agent_command(client, create) == 0
    assert client.calls[0] == (
        "POST",
        "/api/v1/agents/enrollment-tokens",
        {
            "payload": {
                "name": "home-lab",
                "expires_at": None,
                "expires_in_seconds": 1800,
                "allowed_labels": {"environment": "development"},
            },
            "idempotency_key": None,
        },
    )
    assert "secret-once" in capsys.readouterr().out

    client.calls.clear()
    revoke = cli._build_parser().parse_args(
        ["agent", "enrollment-token", "revoke", "token-1", "--output", "json"]
    )
    assert cli._agent_command(client, revoke) == 0
    assert client.calls == [("DELETE", "/api/v1/agents/enrollment-tokens/token-1", {})]
    assert json.loads(capsys.readouterr().out) == {"revoked": True, "token_id": "token-1"}


def test_bench_filters_and_operation_reconcile_use_control_plane_query_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = RecordingClient()
    client.response = {"items": []}
    args = cli._build_parser().parse_args(
        [
            "bench",
            "list",
            "--agent",
            "agent-1",
            "--location",
            "jerusalem-home",
            "--agent-label",
            "environment=development",
        ]
    )
    cli._bench_list(client, args)
    query = client.calls[0][2]
    assert isinstance(query, dict)
    assert query["agent_id"] == "agent-1"
    assert query["location"] == "jerusalem-home"
    assert query["agent_label"] == ["environment=development"]

    client.calls.clear()
    client.response = {"request_id": "reconcile-1"}
    reconcile = cli._build_parser().parse_args(["operation", "reconcile", "operation-1"])
    assert cli._operation_command(client, reconcile) == 0
    assert client.calls[0][:2] == (
        "POST",
        "/api/v1/operations/operation-1/reconcile",
    )
    assert "reconcile-1" in capsys.readouterr().out


def test_distributed_reservation_payload_is_fenced_but_local_payload_is_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = RecordingClient()
    client.response = {
        "reservation_id": "reservation-1",
        "bench_id": "home-lab/esp32-01",
        "owner": "ci",
        "state": "ACTIVE",
    }
    distributed = cli._build_parser().parse_args(
        [
            "reservation",
            "create",
            "home-lab/esp32-01",
            "--owner",
            "ci",
            "--duration",
            "30m",
            "--lease-ttl",
            "2m",
            "--metadata",
            "run=42",
            "--idempotency-key",
            "reservation:42",
        ]
    )
    assert cli._reservation_command(client, distributed) == 0
    body = client.calls[0][2]
    assert isinstance(body, dict)
    assert body["payload"] == {
        "bench_id": "home-lab/esp32-01",
        "owner": "ci",
        "reservation_duration_seconds": 1800,
        "lease_ttl_seconds": 120,
        "idempotency_key": "reservation:42",
        "metadata": {"run": "42"},
    }

    client.calls.clear()
    client.response = {
        "id": "reservation-local",
        "bench_id": "bench-01",
        "owner": "alice",
        "status": "active",
    }
    local = cli._build_parser().parse_args(
        [
            "reservation",
            "create",
            "bench-01",
            "--owner",
            "alice",
            "--duration",
            "10m",
        ]
    )
    assert cli._reservation_command(client, local) == 0
    local_body = client.calls[0][2]
    assert isinstance(local_body, dict)
    assert local_body["payload"] == {
        "bench_id": "bench-01",
        "owner": "alice",
        "starts_at": None,
        "duration_seconds": 600,
        "queue_if_busy": False,
        "idempotency_key": None,
    }
    capsys.readouterr()


def test_distributed_workflow_selectors_and_ci_agent_preferences(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = RecordingClient()
    client.response = {"id": "command-1"}
    run = cli._build_parser().parse_args(
        [
            "workflow",
            "run",
            "smoke",
            "--owner",
            "ci",
            "--version",
            "2",
            "--kind",
            "simulated",
            "--location",
            "simulation",
            "--bench-label",
            "board=esp32",
            "--agent-label",
            "environment=development",
            "--reservation-duration",
            "20m",
            "--lease-ttl",
            "90s",
            "--command-timeout",
            "15m",
            "--idempotency-key",
            "workflow:42",
            "--input",
            "expected_version=0.6.0",
        ]
    )
    assert cli._workflow_command(client, run) == 0
    call = client.calls[0]
    assert call[:2] == ("POST", "/api/v1/workflows/smoke/runs")
    details = call[2]
    assert isinstance(details, dict)
    assert details["idempotency_key"] == "workflow:42"
    payload = details["payload"]
    assert isinstance(payload, dict)
    assert payload["bench_id"] is None
    assert payload["kind"] == "SIMULATED"
    assert payload["bench_labels"] == {"board": "esp32"}
    assert payload["agent_labels"] == {"environment": "development"}
    assert payload["reservation_duration_seconds"] == 1200
    assert payload["lease_ttl_seconds"] == 90
    assert payload["command_timeout_seconds"] == 900

    ci_run = cli._build_parser().parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "smoke",
            "--agent-label",
            "environment=development",
            "--preferred-location",
            "simulation",
        ]
    )
    request = cli._ci_bench_request(ci_run)
    assert request["required_agent_labels"] == {"environment": "development"}
    assert request["preferred_location"] == "simulation"
    assert request["allow_physical"] is False
    capsys.readouterr()


def _write_local_config(root: Path) -> None:
    (root / "agent.yaml").write_text(
        "agent:\n  name: diagnostic-agent\n  log_level: ERROR\nplugins: []\n",
        encoding="utf-8",
    )
    (root / "simlab.yaml").write_text(
        "simlab:\n  benches: 0\n",
        encoding="utf-8",
    )


def test_lab_agent_parser_retains_legacy_server_mode_and_supports_nested_config() -> None:
    legacy = agent_cli._build_parser().parse_args(
        ["--config", "agent.yaml", "--host", "0.0.0.0", "--port", "9090", "--once"]
    )
    assert legacy.command is None
    assert legacy.config == Path("agent.yaml")
    assert legacy.host == "0.0.0.0"
    assert legacy.port == 9090
    assert legacy.once

    diagnostic = agent_cli._build_parser().parse_args(
        ["connection", "status", "--config", "agent.yaml", "--output", "json"]
    )
    assert diagnostic.config == Path("agent.yaml")
    assert diagnostic.connection_command == "status"


def test_lab_agent_connect_consumes_environment_token_without_persisting_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_local_config(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    class EnrollmentClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "https://lab.example.internal"

        def post(self, path: str, payload: dict[str, object]) -> object:
            assert path == "/api/v1/agents/enroll"
            assert payload["enrollment_token"] == "one-time-secret"
            return {
                "agent": {"id": "agent-id"},
                "credential": "persistent-secret",
                "gateway_url": "wss://lab.example.internal/api/v1/agent-gateway/agent-id",
            }

    monkeypatch.setattr(agent_cli, "AgentClient", EnrollmentClient)
    monkeypatch.setenv("LAB_AGENT_ENROLLMENT_TOKEN", "one-time-secret")
    assert (
        agent_cli.main(
            [
                "connect",
                "--config-dir",
                str(tmp_path),
                "--control-plane",
                "https://lab.example.internal",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "one-time-secret" not in output
    assert "persistent-secret" in output
    assert "were not written to disk" in output
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_lab_agent_connect_rejects_remote_plaintext_before_sending_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: secure-enrollment\n"
        "control_plane:\n  allow_insecure_loopback: true\n"
        "plugins: []\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text("simlab:\n  benches: 0\n", encoding="utf-8")
    calls = 0

    class ForbiddenClient:
        def __init__(self, _base_url: str) -> None:
            nonlocal calls
            calls += 1

    monkeypatch.setattr(agent_cli, "AgentClient", ForbiddenClient)
    monkeypatch.setenv("LAB_AGENT_ENROLLMENT_TOKEN", "must-never-leave")

    assert (
        agent_cli.main(
            [
                "connect",
                "--config-dir",
                str(tmp_path),
                "--control-plane",
                "http://control-plane.example",
            ]
        )
        == 1
    )
    assert calls == 0
    captured = capsys.readouterr()
    assert "requires HTTPS" in captured.err
    assert "must-never-leave" not in captured.err


def test_operation_watch_accepts_control_plane_uppercase_terminal_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class OperationClient:
        def get(self, path: str) -> object:
            assert path == "/api/v1/operations/operation-1"
            return {
                "id": "operation-1",
                "status": "SUCCEEDED",
                "progress": 100,
                "message": "done",
            }

    args = cli._build_parser().parse_args(
        ["operation", "watch", "operation-1", "--output", "json", "--interval", "0"]
    )
    assert cli._operation_command(OperationClient(), args) == 0
    assert '"status": "SUCCEEDED"' in capsys.readouterr().out


def test_distributed_probe_waits_for_operation_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ProbeClient:
        def post(self, path: str, payload: dict[str, object]) -> object:
            assert path == "/api/v1/benches/home-lab/bench-01/actions/probe"
            assert payload == {"owner": "alice"}
            return {"operation_id": "operation-1", "status": "DISPATCHED"}

        def get(self, path: str) -> object:
            assert path == "/api/v1/operations/operation-1"
            return {
                "id": "operation-1",
                "status": "SUCCEEDED",
                "result": {
                    "bench_id": "bench-01",
                    "status": "healthy",
                    "details": {"transport": "distributed"},
                },
            }

    args = cli._build_parser().parse_args(
        [
            "bench",
            "probe",
            "home-lab/bench-01",
            "--owner",
            "alice",
            "--output",
            "json",
        ]
    )
    assert cli._bench_command(ProbeClient(), args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "healthy"
    assert output["details"] == {"transport": "distributed"}


def test_lab_agent_status_reads_the_running_local_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_local_config(tmp_path)

    class LocalStatusClient:
        def __init__(self, base_url: str, *, token: str | None = None) -> None:
            assert base_url == "http://127.0.0.1:8080"
            assert token is None

        def get(self, path: str) -> object:
            assert path == "/api/v1/agent/status"
            return {
                "status": "healthy",
                "version": "0.6.0-alpha",
                "agent_name": "diagnostic-agent",
                "location": "office",
                "benches": {"total": 1, "online": 1},
                "control_plane": {
                    "enabled": True,
                    "connected": True,
                    "endpoint": "wss://lab.example/agent-id",
                },
            }

    monkeypatch.setattr(agent_cli, "AgentClient", LocalStatusClient)
    assert agent_cli.main(["status", "--config-dir", str(tmp_path), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["control_plane"]["connected"] is True


def test_lab_agent_doctor_and_empty_journal_work_without_distributed_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_local_config(tmp_path)
    assert agent_cli.main(["doctor", "--config-dir", str(tmp_path)]) == 0
    assert "PASS" in capsys.readouterr().out

    class JournalClient:
        def __init__(self, base_url: str, *, token: str | None = None) -> None:
            assert base_url == "http://127.0.0.1:8080"

        def get(self, path: str, query: dict[str, object]) -> object:
            assert path == "/api/v1/agent/journal"
            assert query == {"status": None, "limit": 100}
            return {"items": []}

    monkeypatch.setattr(agent_cli, "AgentClient", JournalClient)
    assert (
        agent_cli.main(
            [
                "journal",
                "list",
                "--config-dir",
                str(tmp_path),
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"items": []}


def test_local_agent_diagnostic_api_is_safe_when_distributed_mode_is_disabled(
    tmp_path: Path,
) -> None:
    import asyncio

    _write_local_config(tmp_path)
    agent = create_agent(tmp_path)
    try:
        with TestClient(create_app(agent)) as client:
            status = client.get("/api/v1/agent/status")
            assert status.status_code == 200
            assert status.json()["control_plane"] == {
                "enabled": False,
                "connected": False,
            }
            connection = client.get("/api/v1/agent/connection")
            assert connection.status_code == 200
            assert connection.json() == {"enabled": False, "connected": False}
            journal = client.get("/api/v1/agent/journal")
            assert journal.status_code == 200
            assert journal.json() == {"items": []}
            assert client.post("/api/v1/agent/reconnect").status_code == 500
    finally:
        asyncio.run(agent.shutdown())


def _reservation(status: str = "active") -> dict[str, object]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "bench_id": "bench-01",
        "owner": "alice",
        "status": status,
        "starts_at": "2026-07-20T10:00:00Z",
        "ends_at": "2026-07-20T10:30:00Z",
    }


def test_duration_parser_accepts_compound_values_and_rejects_invalid_input() -> None:
    assert parse_duration("30m") == 1800
    assert parse_duration("2H") == 7200
    assert parse_duration("1h30m15s") == 5415
    for invalid in ("", "0m", "30", "1h 30m", "tomorrow"):
        with pytest.raises(ValueError, match="duration"):
            parse_duration(invalid)


def test_reservation_queue_timeline_and_filter_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, object]] = []

    def get(self: AgentClient, path: str, query: dict[str, object] | None = None) -> object:
        calls.append(("GET", path, query))
        if path.endswith("/timeline"):
            return {
                "items": [
                    {
                        "timestamp": "2026-07-20T10:00:00Z",
                        "category": "reservation",
                        "event_type": "RESERVATION_ACTIVATED",
                        "actor": "alice",
                        "summary": "alice reserved the bench",
                    }
                ]
            }
        if path.endswith("/queue"):
            return {
                "items": [
                    {
                        "position": 1,
                        "id": "queue-1",
                        "owner": "bob",
                        "requested_duration_seconds": 600,
                        "status": "waiting",
                    }
                ]
            }
        if path == "/api/v1/reservations":
            return {"items": [_reservation()]}
        if path.startswith("/api/v1/reservations/"):
            return _reservation()
        return {"items": []}

    def post(self: AgentClient, path: str, payload: dict[str, object]) -> object:
        calls.append(("POST", path, payload))
        if path.endswith("/queue"):
            return {
                "id": "queue-1",
                "bench_id": "bench-01",
                "owner": "alice",
                "status": "waiting",
                "position": 1,
            }
        status = (
            "released"
            if path.endswith("/release")
            else "cancelled"
            if path.endswith("/cancel")
            else "active"
        )
        return _reservation(status)

    def delete(self: AgentClient, path: str, payload: dict[str, object]) -> None:
        calls.append(("DELETE", path, payload))

    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(AgentClient, "post", post)
    monkeypatch.setattr(AgentClient, "delete", delete)

    assert (
        main(
            [
                "bench",
                "list",
                "--online",
                "--available",
                "--capability",
                "firmware",
                "--label",
                "board=esp32",
            ]
        )
        == 0
    )
    assert calls[-1][2] == {
        "status": None,
        "capability": "firmware",
        "reserved": None,
        "online": True,
        "available": True,
        "label": ["board=esp32"],
    }
    assert main(["bench", "timeline", "bench-01"]) == 0
    assert "alice reserved the bench" in capsys.readouterr().out

    assert main(["reservation", "list", "--owner", "alice"]) == 0
    assert main(["reservation", "show", "00000000-0000-0000-0000-000000000001"]) == 0
    assert (
        main(
            [
                "reservation",
                "create",
                "bench-01",
                "--owner",
                "alice",
                "--duration",
                "1h30m",
                "--queue-if-busy",
                "--idempotency-key",
                "create-1",
            ]
        )
        == 0
    )
    assert calls[-1][2] == {
        "bench_id": "bench-01",
        "owner": "alice",
        "starts_at": None,
        "duration_seconds": 5400,
        "queue_if_busy": True,
        "idempotency_key": "create-1",
    }
    reservation_id = "00000000-0000-0000-0000-000000000001"
    assert (
        main(
            [
                "reservation",
                "extend",
                reservation_id,
                "--owner",
                "alice",
                "--duration",
                "15m",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert main(["reservation", "release", reservation_id, "--owner", "alice"]) == 0
    assert main(["reservation", "cancel", reservation_id, "--owner", "alice"]) == 0
    assert (
        main(
            [
                "reservation",
                "queue",
                "bench-01",
                "--owner",
                "alice",
                "--duration",
                "20m",
            ]
        )
        == 0
    )
    assert main(["reservation", "queue-list", "bench-01"]) == 0
    assert main(["reservation", "queue-cancel", "queue-1", "--owner", "alice"]) == 0
    output = capsys.readouterr().out
    assert "Reservation released" in output
    assert "Added to queue" in output
    assert "Cancelled queue entry" in output


def test_workflow_commands_and_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = {
        "name": "smoke-test",
        "version": 1,
        "description": "Smoke test",
        "requirements": {"capabilities": ["reset", "serial"]},
        "steps": [{"action": "reset"}, {"action": "probe"}],
    }
    run = {
        "id": "00000000-0000-0000-0000-000000000002",
        "workflow_name": "smoke-test",
        "status": "succeeded",
        "current_step": 1,
    }
    posted: list[tuple[str, dict[str, object]]] = []

    def get(self: AgentClient, path: str, query: dict[str, object] | None = None) -> object:
        if path == "/api/v1/workflows":
            return {"items": [workflow]}
        if path == "/api/v1/workflows/smoke-test":
            return workflow
        return run

    def post(self: AgentClient, path: str, payload: dict[str, object]) -> object:
        posted.append((path, payload))
        return run

    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(AgentClient, "post", post)

    assert main(["workflow", "list"]) == 0
    assert main(["workflow", "show", "smoke-test"]) == 0
    assert (
        main(
            [
                "workflow",
                "run",
                "smoke-test",
                "--bench",
                "bench-01",
                "--owner",
                "alice",
                "--reserve",
                "30m",
                "--release-after",
                "--input",
                "firmware=demo.bin",
            ]
        )
        == 0
    )
    assert posted[-1][1]["reserve_duration_seconds"] == 1800
    assert posted[-1][1]["inputs"] == {"firmware": "demo.bin"}
    run_id = str(run["id"])
    assert main(["workflow", "watch", run_id, "--interval", "0"]) == 0
    assert main(["workflow", "cancel", run_id, "--owner", "alice"]) == 0
    assert "Workflow status: Succeeded" in capsys.readouterr().out

    assert (
        main(
            [
                "workflow",
                "run",
                "smoke-test",
                "--bench",
                "bench-01",
                "--owner",
                "alice",
                "--input",
                "invalid",
            ]
        )
        == 1
    )
    assert "key=value" in capsys.readouterr().err


def test_workflow_watch_failure_returns_operation_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        AgentClient,
        "get",
        lambda self, path, query=None: {"status": "failed", "current_step": None},
    )
    assert main(["workflow", "watch", "run-1", "--output", "json"]) == 7


class _Entry:
    def __init__(self, name: str, content: bytes | None = None, children: tuple[_Entry, ...] = ()):
        self.name = name
        self._content = content
        self._children = children

    def is_dir(self) -> bool:
        return self._content is None

    def iterdir(self) -> tuple[_Entry, ...]:
        return self._children

    def read_bytes(self) -> bytes:
        assert self._content is not None
        return self._content


def _template() -> _Entry:
    return _Entry(
        "production",
        children=(
            _Entry("compose.yaml", b"services: {}\n"),
            _Entry(".env.example", b"LAB_RELEASE_CHANNEL=stable\n"),
            _Entry("config", children=(_Entry("control-plane.yaml", b"profile: production\n"),)),
        ),
    )


def test_init_materializes_templates_and_owner_only_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _template())
    destination = tmp_path / "deployment"

    assert product.main(["init", str(destination)]) == 0

    assert (destination / "compose.yaml").read_text() == "services: {}\n"
    assert (destination / ".env").read_text() == "LAB_RELEASE_CHANNEL=stable\n"
    for name in ("control-plane-secret-key", "database-url", "postgres-password"):
        secret = destination / "secrets" / name
        assert secret.read_text().strip()
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "secrets").stat().st_mode) == 0o700
    assert (
        (destination / "secrets" / "database-url")
        .read_text()
        .startswith("postgresql://lab_platform:")
    )
    assert "Initialized production deployment" in capsys.readouterr().out


def test_init_refuses_to_overwrite_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _template())
    destination = tmp_path / "deployment"
    destination.mkdir()
    (destination / "compose.yaml").write_text("mine\n")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        product.main(["init", str(destination)])

    assert (destination / "compose.yaml").read_text() == "mine\n"


def test_dev_uses_packaged_demo_and_structured_subprocess_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _Entry(
        "demo",
        children=(
            _Entry("compose.yaml", b"services: {}\n"),
            _Entry(".env.example", b"DEMO=true\n"),
        ),
    )
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: demo)
    observed: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def execute(command: list[str], *, cwd: Path) -> int:
        observed.update(command=command, cwd=cwd)
        return 0

    monkeypatch.setattr(product, "_execute", execute)
    destination = tmp_path / "demo"

    assert product.main(["dev", "up", "--detach", "--directory", str(destination)]) == 0
    assert observed == {
        "command": [
            "docker",
            "compose",
            "-f",
            str(destination / "compose.yaml"),
            "up",
            "--detach",
        ],
        "cwd": destination,
    }


def test_init_rejects_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _Entry("empty"))

    with pytest.raises(RuntimeError, match="template is missing"):
        product.main(["init", str(tmp_path / "deployment")])


def test_init_reports_bounded_collision_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = _Entry(
        "production",
        children=tuple(_Entry(f"file-{index}", b"value\n") for index in range(7)),
    )
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: template)
    destination = tmp_path / "deployment"
    destination.mkdir()
    for index in range(7):
        (destination / f"file-{index}").write_text("mine\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"and 2 more"):
        product.main(["init", str(destination)])


def test_demo_init_without_env_example_creates_empty_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo = _Entry("demo", children=(_Entry("compose.yaml", b"services: {}\n"),))
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: demo)
    destination = tmp_path / "demo"

    assert product.main(["init", "--demo", str(destination)]) == 0
    assert (destination / ".env").read_bytes() == b""
    assert not (destination / "secrets").exists()


@pytest.mark.parametrize(
    ("arguments", "tail"),
    [
        (["dev", "down"], ["down"]),
        (["dev", "logs", "--follow"], ["logs", "--follow"]),
        (["dev", "ps"], ["ps"]),
    ],
)
def test_dev_actions_build_expected_compose_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    tail: list[str],
) -> None:
    destination = tmp_path / "demo"
    destination.mkdir()
    (destination / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    commands: list[list[str]] = []

    def execute(command: list[str], *, cwd: Path) -> int:
        assert cwd == destination
        commands.append(command)
        return 7

    monkeypatch.setattr(product, "_execute", execute)
    assert product.main([*arguments, "--directory", str(destination)]) == 7
    assert commands[0][-len(tail) :] == tail


def test_execute_returns_process_status_and_explains_missing_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lab_platform.cli.product.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 9),
    )
    assert product._execute(["docker", "compose", "ps"], cwd=tmp_path) == 9

    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr("lab_platform.cli.product.subprocess.run", missing)
    with pytest.raises(RuntimeError, match="Docker was not found"):
        product._execute(["docker", "compose", "ps"], cwd=tmp_path)


def test_compose_file_supports_legacy_names_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No Compose file"):
        product._compose_file(tmp_path)

    legacy = tmp_path / "docker-compose.yaml"
    legacy.write_text("services: {}\n", encoding="utf-8")
    assert product._compose_file(tmp_path) == legacy


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        product.main([])


cli_module = importlib.import_module("lab_platform.cli.main")


class VersionClient:
    def get(self, path: str) -> object:
        if path == "/api/v1/version":
            return {
                "version": "0.9.0-beta",
                "api_version": "v1",
                "protocol_version": "1.0",
                "plugin_api_version": "1.0",
                "release_channel": "preview",
                "edition": "community",
            }
        if path == "/api/v1/agents":
            return {
                "items": [
                    {
                        "id": "agent-id",
                        "name": "lab-east",
                        "version": "0.8.3",
                        "protocol_version": "1.0",
                        "upgrade": {"status": "UPGRADE_RECOMMENDED"},
                    }
                ]
            }
        raise AssertionError(path)


def test_version_all_reports_local_service_protocol_and_agent_versions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "_client", lambda _args: VersionClient())

    assert cli_module.main(["version", "--all"]) == 0

    output = capsys.readouterr().out
    assert f"CLI {VERSION}" in " ".join(output.split())
    assert "Control Plane" in output
    assert "lab-east" in output
    assert "UPGRADE_RECOMMENDED" in output


def test_version_all_json_preserves_machine_readable_component_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "_client", lambda _args: VersionClient())

    assert cli_module.main(["version", "--all", "--output", "json"]) == 0

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["cli"]["version"] == VERSION
    assert payload["control_plane"]["release_channel"] == "preview"
    assert payload["agents"][0]["version"] == "0.8.3"
    assert payload["agents"][0]["upgrade_status"] == "UPGRADE_RECOMMENDED"


class RecordingClientOperationalCli(AgentClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append((path, payload))
        return {
            "bench_id": "esp32-validation-01",
            "status": "MAINTENANCE" if path.endswith("/start") else "HEALTHY",
        }


def test_labctl_bench_maintenance_start_and_end() -> None:
    client = RecordingClientOperationalCli()
    parser = cli._build_parser()

    start = parser.parse_args(
        [
            "bench",
            "maintenance",
            "start",
            "esp32-validation-01",
            "--reason",
            "Inspect USB cable",
        ]
    )
    end = parser.parse_args(["bench", "maintenance", "end", "esp32-validation-01"])

    assert cli._bench_command(client, start) == 0
    assert cli._bench_command(client, end) == 0
    assert client.calls == [
        (
            "/api/v1/operational/benches/esp32-validation-01/maintenance/start",
            {"reason": "Inspect USB cable"},
        ),
        (
            "/api/v1/operational/benches/esp32-validation-01/maintenance/end",
            {},
        ),
    ]
