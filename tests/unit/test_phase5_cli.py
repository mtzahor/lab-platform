from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent import create_agent, create_app
from lab_platform.cli.client import AgentApiError, AgentClient

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
