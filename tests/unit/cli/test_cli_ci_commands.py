from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import TracebackType
from urllib.error import HTTPError, URLError
from urllib.request import Request

import lab_platform.cli.client as client_module
import pytest
import yaml
from lab_platform.cli.ci_environment import CiEnvironment, CiProvider, detect_ci_environment
from lab_platform.cli.ci_exit_codes import (
    CiExitCode,
    exit_code_for_error,
    exit_code_for_status,
)
from lab_platform.cli.ci_summary import (
    CiSummaryStep,
    HardwareCiSummary,
    append_github_summary,
    render_github_summary,
)
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError

from tests.support.paths import REPOSITORY_ROOT

cli = importlib.import_module("lab_platform.cli.main")


def ci_environment() -> CiEnvironment:
    return CiEnvironment(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="run-42",
        repository="owner/repository",
        ref="refs/heads/main",
        commit_sha="abc123",
        actor="octocat",
        attempt="2",
    )


def test_ci_parser_keeps_download_path_separate_and_builds_bench_request() -> None:
    parser = cli._build_parser()
    download = parser.parse_args(
        ["ci", "download", "artifact-1", "--output", "downloads/result.bin"]
    )
    assert download.output == Path("downloads/result.bin")

    run = parser.parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "smoke-test",
            "--bench",
            "bench-01",
            "--require",
            "capability=SERIAL",
            "--require",
            "capability=firmware",
            "--label",
            "board=esp32",
            "--prefer-label",
            "location=lab-a",
            "--no-allow-physical",
            "--wait-timeout",
            "90s",
            "--reservation-duration",
            "30m",
        ]
    )
    assert cli._ci_bench_request(run) == {
        "explicit_bench_id": "bench-01",
        "required_capabilities": ["firmware", "serial"],
        "required_labels": {"board": "esp32"},
        "preferred_labels": {"location": "lab-a"},
        "allow_simulated": True,
        "allow_physical": False,
        "maximum_wait_seconds": 90,
        "reservation_duration_seconds": 1800,
    }

    neither = parser.parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "smoke-test",
            "--no-allow-simulated",
            "--no-allow-physical",
        ]
    )
    with pytest.raises(ValueError, match="at least one"):
        cli._ci_bench_request(neither)


class SuccessfulCiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.sessions: Iterator[dict[str, object]] = iter(
            [
                {"id": "session-1", "status": "waiting_for_bench"},
                {"id": "session-1", "status": "reserved", "bench_id": "bench-01"},
                {"id": "session-1", "status": "running", "bench_id": "bench-01"},
                {"id": "session-1", "status": "succeeded", "outcome": "succeeded"},
            ]
        )
        self.workflows: Iterator[dict[str, object]] = iter(
            [
                {
                    "id": "workflow-1",
                    "status": "running",
                    "steps": [
                        {
                            "step_index": 0,
                            "name": "Probe target",
                            "status": "running",
                        }
                    ],
                },
                {
                    "id": "workflow-1",
                    "status": "succeeded",
                    "steps": [
                        {
                            "step_index": 0,
                            "name": "Probe target",
                            "status": "succeeded",
                        }
                    ],
                },
                {
                    "id": "workflow-1",
                    "status": "succeeded",
                    "steps": [
                        {
                            "step_index": 0,
                            "name": "Probe target",
                            "status": "succeeded",
                        }
                    ],
                },
            ]
        )
        self.finalized = {
            "id": "session-1",
            "status": "completed",
            "outcome": "succeeded",
            "cleanup_status": "succeeded",
            "bench_id": "bench-01",
            "workflow_run_id": "workflow-1",
            "created_at": "2026-07-23T10:00:00Z",
            "started_at": "2026-07-23T10:00:01Z",
            "completed_at": "2026-07-23T10:00:04Z",
        }
        self.artifacts = {
            "items": [
                {
                    "id": "artifact-serial",
                    "name": "serial.log",
                    "artifact_type": "serial_log",
                }
            ]
        }

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append(("POST", path))
        if path == "/api/v1/ci/sessions":
            return {"id": "session-1", "status": "waiting_for_bench"}
        if path.endswith("/heartbeat"):
            return {"status": "running"}
        if path.endswith("/run"):
            return {"workflow_run_id": "workflow-1"}
        if path.endswith("/finalize"):
            return self.finalized
        raise AssertionError(path)

    def get(self, path: str, query: dict[str, object] | None = None) -> object:
        self.calls.append(("GET", path))
        if path == "/api/v1/ci/sessions/session-1":
            return next(self.sessions)
        if path == "/api/v1/workflow-runs/workflow-1":
            return next(self.workflows)
        if path == "/api/v1/ci/sessions/session-1/artifacts":
            return self.artifacts
        raise AssertionError(path)

    def upload_artifact(self, path: str, artifact_path: Path, **kwargs: object) -> object:
        self.calls.append(("UPLOAD", path))
        return {"id": "artifact-firmware"}

    def get_text(self, path: str) -> str:
        self.calls.append(("TEXT", path))
        return "<?xml version='1.0'?><testsuite tests='1'/>"

    def download(self, path: str) -> bytes:
        self.calls.append(("DOWNLOAD", path))
        return b"READY\nSELF_TEST=PASS\n"


def test_ci_run_orchestrates_heartbeat_outputs_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"firmware")
    junit = tmp_path / "results" / "hardware.xml"
    artifacts = tmp_path / "artifacts"
    github_output = tmp_path / "github-output.txt"
    github_summary = tmp_path / "github-summary.md"
    args = cli._build_parser().parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "smoke-test",
            "--artifact",
            f"firmware={firmware}",
            "--input",
            "expected_version=abc123",
            "--require",
            "capability=serial",
            "--label",
            "board=esp32",
            "--no-allow-physical",
            "--wait-timeout",
            "1s",
            "--reservation-duration",
            "1m",
            "--junit-output",
            str(junit),
            "--artifacts-directory",
            str(artifacts),
            "--interval",
            "0.001",
            "--output",
            "json",
        ]
    )
    monkeypatch.setattr(cli, "detect_ci_environment", ci_environment)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    client = SuccessfulCiClient()

    assert cli._ci_run(client, args) == CiExitCode.SUCCESS
    assert junit.read_text(encoding="utf-8").startswith("<?xml")
    assert (artifacts / "serial.log").read_bytes() == b"READY\nSELF_TEST=PASS\n"
    outputs = github_output.read_text(encoding="utf-8")
    assert "session-id=session-1" in outputs
    assert "bench-id=bench-01" in outputs
    assert "workflow-run-id=workflow-1" in outputs
    assert "result=succeeded" in outputs
    summary = github_summary.read_text(encoding="utf-8")
    assert "Hardware CI Result" in summary
    assert "Probe target" in summary
    assert ("POST", "/api/v1/ci/sessions/session-1/heartbeat") in client.calls
    assert client.calls.count(("POST", "/api/v1/ci/sessions/session-1/finalize")) == 1


def replay_args(tmp_path: Path) -> object:
    return cli._build_parser().parse_args(
        [
            "ci",
            "run",
            "--workflow",
            "smoke-test",
            "--artifact",
            f"firmware={tmp_path / 'already-uploaded.bin'}",
            "--junit-output",
            str(tmp_path / "replayed.xml"),
            "--artifacts-directory",
            str(tmp_path / "replayed-artifacts"),
            "--interval",
            "0.001",
            "--output",
            "json",
        ]
    )


class RunningReplayClient(SuccessfulCiClient):
    def __init__(self) -> None:
        super().__init__()
        self.sessions = iter([{"id": "session-1", "status": "succeeded", "outcome": "succeeded"}])
        terminal_workflow: dict[str, object] = {
            "id": "workflow-1",
            "status": "succeeded",
            "steps": [{"step_index": 0, "name": "Probe target", "status": "succeeded"}],
        }
        self.workflows = iter([terminal_workflow, terminal_workflow])

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append(("POST", path))
        if path == "/api/v1/ci/sessions":
            return {
                "id": "session-1",
                "status": "running",
                "bench_id": "bench-01",
                "workflow_run_id": "workflow-1",
            }
        if path.endswith("/finalize"):
            return self.finalized
        raise AssertionError(path)


class CompletedReplayClient(SuccessfulCiClient):
    def __init__(self) -> None:
        super().__init__()
        terminal_workflow: dict[str, object] = {
            "id": "workflow-1",
            "status": "succeeded",
            "steps": [{"step_index": 0, "name": "Probe target", "status": "succeeded"}],
        }
        self.workflows = iter([terminal_workflow])

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append(("POST", path))
        if path == "/api/v1/ci/sessions":
            return self.finalized
        raise AssertionError(path)


def test_ci_run_resumes_running_idempotent_create_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "detect_ci_environment", ci_environment)
    client = RunningReplayClient()

    assert cli._ci_run(client, replay_args(tmp_path)) == CiExitCode.SUCCESS
    assert not any(kind == "UPLOAD" for kind, _path in client.calls)
    assert ("POST", "/api/v1/ci/sessions/session-1/run") not in client.calls
    assert ("GET", "/api/v1/workflow-runs/workflow-1") in client.calls
    assert ("POST", "/api/v1/ci/sessions/session-1/finalize") in client.calls
    assert (tmp_path / "replayed.xml").is_file()
    assert (tmp_path / "replayed-artifacts" / "serial.log").is_file()


def test_ci_run_completed_replay_still_downloads_and_publishes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = tmp_path / "replayed-github-output.txt"
    monkeypatch.setattr(cli, "detect_ci_environment", ci_environment)
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    client = CompletedReplayClient()

    assert cli._ci_run(client, replay_args(tmp_path)) == CiExitCode.SUCCESS
    assert not any(kind == "UPLOAD" for kind, _path in client.calls)
    assert ("POST", "/api/v1/ci/sessions/session-1/run") not in client.calls
    assert ("POST", "/api/v1/ci/sessions/session-1/finalize") not in client.calls
    assert (tmp_path / "replayed.xml").is_file()
    assert (tmp_path / "replayed-artifacts" / "serial.log").is_file()
    assert "session-id=session-1" in github_output.read_text(encoding="utf-8")


class CancellationClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append(("POST", path))
        if path.endswith("/cancel"):
            return {"id": "session-1", "status": "cancel_requested"}
        if path.endswith("/finalize"):
            return {
                "id": "session-1",
                "status": "completed",
                "outcome": "cancelled",
                "cleanup_status": "succeeded",
            }
        raise AssertionError(path)

    def get(self, path: str, query: dict[str, object] | None = None) -> object:
        self.calls.append(("GET", path))
        return {"id": "session-1", "status": "cancelled", "outcome": "cancelled"}


def test_ci_cancel_waits_for_terminal_state_then_finalizes() -> None:
    client = CancellationClient()
    result = cli._cancel_and_finalize_ci_session(
        client,
        "session-1",
        wait_seconds=0.1,
        poll_interval=0.001,
    )

    assert result is not None
    assert result["status"] == "completed"
    assert client.calls == [
        ("POST", "/api/v1/ci/sessions/session-1/cancel"),
        ("GET", "/api/v1/ci/sessions/session-1"),
        ("POST", "/api/v1/ci/sessions/session-1/finalize"),
    ]


def test_ci_cleanup_requests_allow_the_agent_configured_deadline() -> None:
    class TimeoutClient(CancellationClient):
        def __init__(self) -> None:
            super().__init__()
            self.timeouts: list[float | None] = []

        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            idempotency_key: str | None = None,
            timeout: float | None = None,
        ) -> object:
            self.timeouts.append(timeout)
            return super().post(
                path,
                payload,
                idempotency_key=idempotency_key,
                timeout=timeout,
            )

    client = TimeoutClient()

    assert (
        cli._cancel_and_finalize_ci_session(
            client,
            "session-1",
            session={"cleanup_timeout_seconds": 125},
        )
        is not None
    )
    assert client.timeouts == [135.0, 135.0]


def test_heartbeat_worker_uses_independent_bounded_requests() -> None:
    called = threading.Event()
    observed_timeouts: list[float | None] = []

    class HeartbeatClient:
        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            timeout: float | None = None,
        ) -> object:
            assert path.endswith("/heartbeat")
            observed_timeouts.append(timeout)
            called.set()
            return {"status": "running"}

    worker = cli._CiHeartbeatWorker(HeartbeatClient(), "session-1", interval_seconds=0.01)
    worker.start()
    assert called.wait(timeout=1)
    worker.stop()

    assert observed_timeouts
    assert observed_timeouts[0] == 1.0
    assert not worker._thread.is_alive()


class FailedCleanupCancellationClient(CancellationClient):
    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        response = super().post(
            path,
            payload,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )
        if path.endswith("/finalize"):
            return {
                "id": "session-1",
                "status": "completed",
                "outcome": "infrastructure_error",
                "cleanup_status": "failed",
            }
        return response


def test_ci_session_cancel_reports_cleanup_failure_with_precedence() -> None:
    args = cli._build_parser().parse_args(
        ["ci", "session", "cancel", "session-1", "--output", "json"]
    )

    assert (
        cli._ci_session_command(FailedCleanupCancellationClient(), args)
        == CiExitCode.CLEANUP_FAILED
    )


def test_interrupted_ci_run_reports_cleanup_failure_with_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptingClient(FailedCleanupCancellationClient):
        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            idempotency_key: str | None = None,
            timeout: float | None = None,
        ) -> object:
            if path == "/api/v1/ci/sessions":
                return {"id": "session-1", "status": "waiting_for_bench"}
            if path.endswith("/heartbeat"):
                return {"id": "session-1", "status": "waiting_for_bench"}
            return super().post(
                path,
                payload,
                idempotency_key=idempotency_key,
                timeout=timeout,
            )

        def get(self, path: str, query: dict[str, object] | None = None) -> object:
            if path.endswith("/artifacts"):
                return {"items": []}
            if self.calls and self.calls[-1] == ("POST", "/api/v1/ci/sessions/session-1/cancel"):
                return super().get(path, query)
            raise KeyboardInterrupt

    args = cli._build_parser().parse_args(
        ["ci", "run", "--workflow", "smoke-test", "--interval", "0.001"]
    )
    monkeypatch.setattr(cli, "detect_ci_environment", ci_environment)

    assert cli._ci_run(InterruptingClient(), args) == CiExitCode.CLEANUP_FAILED


def test_interrupted_ci_run_preserves_exit_and_downloads_available_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class DiagnosticCancellationClient:
        def __init__(self) -> None:
            self.interrupted = False

        def post(
            self,
            path: str,
            payload: dict[str, object],
            *,
            idempotency_key: str | None = None,
            timeout: float | None = None,
        ) -> object:
            if path == "/api/v1/ci/sessions":
                return {
                    "id": "session-1",
                    "status": "running",
                    "workflow_run_id": "workflow-1",
                    "heartbeat_interval_seconds": 30,
                    "cleanup_timeout_seconds": 5,
                }
            if path.endswith(("/cancel", "/finalize")):
                return {
                    "id": "session-1",
                    "status": "completed",
                    "outcome": "cancelled",
                    "cleanup_status": "succeeded",
                    "workflow_run_id": "workflow-1",
                }
            raise AssertionError(path)

        def get(self, path: str, query: dict[str, object] | None = None) -> object:
            if path == "/api/v1/workflow-runs/workflow-1" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            if path == "/api/v1/workflow-runs/workflow-1":
                return {"status": "cancelled", "steps": []}
            if path == "/api/v1/ci/sessions/session-1/artifacts":
                return {
                    "items": [
                        {
                            "id": "serial-artifact",
                            "name": "serial.log",
                            "artifact_type": "serial_log",
                        }
                    ]
                }
            raise AssertionError(path)

        def get_text(self, path: str) -> str:
            raise cli.AgentApiError(404, "ARTIFACT_NOT_FOUND", f"missing {path}")

        def download(self, path: str) -> bytes:
            assert path == "/api/v1/artifacts/serial-artifact/content"
            return b"partial cancellation diagnostics\n"

    monkeypatch.setattr(cli, "detect_ci_environment", ci_environment)
    args = replay_args(tmp_path)

    assert cli._ci_run(DiagnosticCancellationClient(), args) == CiExitCode.WORKFLOW_CANCELLED
    assert (tmp_path / "replayed-artifacts" / "serial.log").read_bytes() == (
        b"partial cancellation diagnostics\n"
    )
    assert not (tmp_path / "replayed.xml").exists()
    assert "diagnostics download incomplete (JUnit XML)" in capsys.readouterr().err


def test_ci_session_watch_interrupt_cancels_and_finalizes() -> None:
    class InterruptedWatchClient(CancellationClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_get = True

        def get(self, path: str, query: dict[str, object] | None = None) -> object:
            if self.first_get:
                self.first_get = False
                raise KeyboardInterrupt
            return super().get(path, query)

    args = cli._build_parser().parse_args(
        ["ci", "session", "watch", "session-1", "--output", "json"]
    )
    client = InterruptedWatchClient()

    assert cli._ci_session_command(client, args) == CiExitCode.WORKFLOW_CANCELLED
    assert ("POST", "/api/v1/ci/sessions/session-1/cancel") in client.calls
    assert ("POST", "/api/v1/ci/sessions/session-1/finalize") in client.calls


class FailedResultsClient:
    def get(self, path: str, query: dict[str, object] | None = None) -> object:
        if path == "/api/v1/workflow-runs/workflow-1":
            return {"status": "failed", "error_code": None}
        if path.endswith("/results"):
            return {"results": [{"name": "Self test", "status": "failed"}]}
        raise AssertionError(path)


def test_ci_session_exit_codes_are_stable_and_cleanup_has_precedence() -> None:
    completed: dict[str, object] = {
        "status": "completed",
        "outcome": "failed",
        "cleanup_status": "succeeded",
    }
    assert (
        cli._ci_exit_for_session(
            completed,
            workflow_run_id="workflow-1",
            client=FailedResultsClient(),
        )
        is CiExitCode.HARDWARE_TEST_FAILED
    )
    assert (
        cli._ci_exit_for_session(
            {**completed, "cleanup_status": "failed"},
            workflow_run_id="workflow-1",
            client=FailedResultsClient(),
        )
        is CiExitCode.CLEANUP_FAILED
    )
    assert (
        cli._ci_exit_for_session(
            {
                "status": "completed",
                "outcome": "timed_out",
                "cleanup_status": "succeeded",
                "errors": ["BENCH_WAIT_TIMEOUT"],
            }
        )
        is CiExitCode.BENCH_WAIT_TIMEOUT
    )


def test_cleanup_failure_is_not_masked_by_missing_junit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class MissingJunitClient(SuccessfulCiClient):
        def get_text(self, path: str) -> str:
            raise cli.AgentApiError(404, "ARTIFACT_NOT_FOUND", f"missing {path}")

    args = replay_args(tmp_path)
    session = {
        **MissingJunitClient().finalized,
        "outcome": "infrastructure_error",
        "cleanup_status": "failed",
    }
    client = MissingJunitClient()

    assert (
        cli._finish_ci_run(
            client,
            session,
            workflow_run_id="workflow-1",
            args=args,
        )
        == CiExitCode.CLEANUP_FAILED
    )
    assert "diagnostics download incomplete" in capsys.readouterr().err


def test_ci_upload_local_validation_uses_stable_artifact_exit_code(tmp_path: Path) -> None:
    missing = tmp_path / "missing.bin"
    assert (
        cli.main(["ci", "upload", "session-1", str(missing)]) == CiExitCode.ARTIFACT_UPLOAD_FAILED
    )


ROOT = REPOSITORY_ROOT


class FakeResponse(BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def response(payload: object, *, status: int = 200) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode(), status=status)


def request_data(request: Request) -> bytes:
    data = request.data
    assert isinstance(data, bytes)
    return data


def test_agent_client_adds_bearer_custom_and_idempotency_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def open_request(request: Request, timeout: float) -> FakeResponse:
        requests.append(request)
        assert timeout == 3
        return response({"ok": True})

    monkeypatch.setattr(client_module, "urlopen", open_request)
    client = AgentClient("https://agent.example/", timeout=3, token="secret-token")

    assert client.get(
        "/api/v1/benches",
        {"online": True, "ignored": None},
        headers={"X-Trace": "trace-1", "authorization": "Bearer ignored"},
    ) == {"ok": True}
    assert requests[-1].full_url == "https://agent.example/api/v1/benches?online=True"
    assert requests[-1].get_header("Authorization") == "Bearer secret-token"
    assert requests[-1].get_header("X-trace") == "trace-1"

    assert client.post(
        "/api/v1/ci/sessions",
        {"provider": "local"},
        headers={"X-Request-ID": "request-1"},
        idempotency_key="local:repo:run:1",
    ) == {"ok": True}
    posted = requests[-1]
    assert posted.get_method() == "POST"
    assert posted.get_header("Content-type") == "application/json"
    assert posted.get_header("Idempotency-key") == "local:repo:run:1"
    assert posted.get_header("X-request-id") == "request-1"
    assert json.loads(request_data(posted)) == {"provider": "local"}

    with pytest.raises(ValueError, match="non-empty"):
        AgentClient("https://agent.example", token="  ")
    with pytest.raises(ValueError, match="newlines"):
        AgentClient("https://agent.example", token="secret\nheader")


def test_agent_client_preserves_firmware_upload_and_adds_generic_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def open_request(request: Request, timeout: float) -> FakeResponse:
        requests.append(request)
        return response({"id": "artifact-id"})

    monkeypatch.setattr(client_module, "urlopen", open_request)
    firmware = tmp_path / 'firm"ware.bin'
    firmware.write_bytes(b"firmware-bytes")
    client = AgentClient("http://agent", token="machine-token")

    assert client.upload(
        "/api/v1/benches/bench-01/actions/flash",
        firmware,
        owner="ci-user",
        version="1.2.3",
    ) == {"id": "artifact-id"}
    legacy_body = request_data(requests[-1])
    assert b'name="firmware"' in legacy_body
    assert b'name="owner"\r\n\r\nci-user' in legacy_body
    assert b'name="version"\r\n\r\n1.2.3' in legacy_body
    assert b"firmware-bytes" in legacy_body

    assert client.upload_artifact(
        "/api/v1/artifacts",
        firmware,
        name="firmware.bin",
        artifact_type="firmware",
        ci_session_id="session-id",
        checksum="a" * 64,
        fields={"optional": None, "channel": "stable"},
        idempotency_key="artifact-upload-1",
    ) == {"id": "artifact-id"}
    artifact_request = requests[-1]
    artifact_body = request_data(artifact_request)
    assert artifact_request.get_header("Authorization") == "Bearer machine-token"
    assert artifact_request.get_header("Idempotency-key") == "artifact-upload-1"
    assert b'name="file"' in artifact_body
    assert b'filename="firm\\"ware.bin"' in artifact_body
    assert b'name="artifact_type"\r\n\r\nfirmware' in artifact_body
    assert b'name="ci_session_id"\r\n\r\nsession-id' in artifact_body
    assert b'name="sha256"\r\n\r\n' + (b"a" * 64) in artifact_body
    assert b'name="channel"\r\n\r\nstable' in artifact_body
    assert b"optional" not in artifact_body


def test_agent_client_reads_binary_text_and_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: Iterator[FakeResponse] = iter(
        [
            FakeResponse(b"\x00\x01binary"),
            FakeResponse(b"<testsuite />"),
            FakeResponse(b"", status=204),
        ]
    )

    def open_request(request: Request, timeout: float) -> FakeResponse:
        return next(replies)

    monkeypatch.setattr(client_module, "urlopen", open_request)
    client = AgentClient("http://agent")

    assert client.download("/api/v1/artifacts/id/content") == b"\x00\x01binary"
    assert client.get_text("/api/v1/workflow-runs/id/results/junit") == "<testsuite />"
    assert client.delete("/api/v1/ci/sessions/id", {}) is None


def test_agent_client_allows_cleanup_specific_post_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    def open_request(request: Request, timeout: float) -> FakeResponse:
        observed.append(timeout)
        return response({"status": "completed"})

    monkeypatch.setattr(client_module, "urlopen", open_request)
    client = AgentClient("http://agent", timeout=3)

    assert client.post("/api/v1/ci/sessions/id/finalize", {}, timeout=75) == {"status": "completed"}
    assert observed == [75]


def test_agent_client_translates_json_plain_and_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: Iterator[Exception] = iter(
        [
            HTTPError(
                "http://agent/first",
                403,
                "Forbidden",
                Message(),
                BytesIO(
                    json.dumps(
                        {
                            "error": {
                                "code": "INSUFFICIENT_SCOPE",
                                "message": "Scope missing",
                                "details": {"scope": "artifacts:read"},
                            }
                        }
                    ).encode()
                ),
            ),
            HTTPError("http://agent/second", 500, "Broken", Message(), BytesIO(b"not-json")),
            URLError("offline"),
        ]
    )

    def fail_request(request: Request, timeout: float) -> FakeResponse:
        raise next(errors)

    monkeypatch.setattr(client_module, "urlopen", fail_request)
    client = AgentClient("http://agent")

    with pytest.raises(AgentApiError) as scoped:
        client.get("/first")
    assert scoped.value.status == 403
    assert scoped.value.code == "INSUFFICIENT_SCOPE"
    assert scoped.value.details == {"scope": "artifacts:read"}

    with pytest.raises(AgentApiError) as malformed:
        client.download("/second")
    assert malformed.value.status == 500
    assert malformed.value.code == "HTTP_ERROR"

    with pytest.raises(AgentConnectionError, match="offline"):
        client.download("/third")


@pytest.mark.parametrize(
    ("environment", "provider", "run_id", "repository", "ref", "actor", "attempt"),
    [
        (
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_RUN_ID": "123",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_REF": "refs/pull/1/merge",
                "GITHUB_SHA": "abc",
                "GITHUB_ACTOR": "octocat",
                "GITHUB_RUN_ATTEMPT": "2",
                "GITLAB_CI": "true",
            },
            CiProvider.GITHUB_ACTIONS,
            "123",
            "owner/repo",
            "refs/pull/1/merge",
            "octocat",
            "2",
        ),
        (
            {
                "GITHUB_ACTIONS": "false",
                "GITLAB_CI": "true",
                "CI_PIPELINE_ID": "456",
                "CI_PROJECT_PATH": "group/project",
                "CI_COMMIT_REF_NAME": "feature",
                "CI_COMMIT_SHA": "def",
                "GITLAB_USER_LOGIN": "gitlab-user",
                "CI_JOB_ID": "9",
            },
            CiProvider.GITLAB_CI,
            "456",
            "group/project",
            "feature",
            "gitlab-user",
            "9",
        ),
        (
            {
                "JENKINS_URL": "https://jenkins.example",
                "BUILD_ID": "789",
                "JOB_NAME": "embedded/main",
                "BRANCH_NAME": "main",
                "GIT_COMMIT": "fed",
                "BUILD_USER_ID": "jenkins-user",
                "BUILD_NUMBER": "4",
            },
            CiProvider.JENKINS,
            "789",
            "embedded/main",
            "main",
            "jenkins-user",
            "4",
        ),
    ],
)
def test_detect_ci_provider_metadata(
    environment: dict[str, str],
    provider: CiProvider,
    run_id: str,
    repository: str,
    ref: str,
    actor: str,
    attempt: str,
) -> None:
    detected = detect_ci_environment(environment)

    assert detected.provider is provider
    assert detected.external_run_id == run_id
    assert detected.repository == repository
    assert detected.ref == ref
    assert detected.actor == actor
    assert detected.attempt == attempt
    assert detected.idempotency_key == f"{provider.value}:{repository}:{run_id}:{attempt}"
    assert detected.as_payload()["provider"] == provider.value


def test_detect_local_and_unknown_ci_metadata() -> None:
    local = detect_ci_environment(
        {
            "LAB_PLATFORM_CI_RUN_ID": "local-run",
            "LAB_PLATFORM_CI_REPOSITORY": "workspace",
            "LAB_PLATFORM_CI_ACTOR": "developer",
        }
    )
    assert local.provider is CiProvider.LOCAL
    assert local.external_run_id == "local-run"
    assert local.repository == "workspace"

    unknown = detect_ci_environment({"CI": "true"})
    assert unknown.provider is CiProvider.UNKNOWN
    assert unknown.external_run_id.startswith("local-")
    assert unknown.external_run_id != detect_ci_environment({"CI": "true"}).external_run_id

    jenkins = detect_ci_environment({"JENKINS_URL": "https://jenkins.example"})
    assert jenkins.provider is CiProvider.JENKINS
    assert jenkins.ref is None


def test_stable_ci_exit_code_mapping_and_cleanup_precedence() -> None:
    assert [int(code) for code in CiExitCode] == [0, *range(10, 21)]
    assert exit_code_for_error("WORKFLOW_ASSERTION_FAILED") is CiExitCode.HARDWARE_TEST_FAILED
    assert exit_code_for_error("ARTIFACT_TOO_LARGE") is CiExitCode.ARTIFACT_UPLOAD_FAILED
    assert exit_code_for_error("DEVICE_NOT_FOUND") is CiExitCode.BACKEND_UNAVAILABLE
    assert exit_code_for_error(None, http_status=401) is CiExitCode.AUTHENTICATION_FAILED
    assert exit_code_for_error(None, http_status=503) is CiExitCode.BACKEND_UNAVAILABLE
    assert exit_code_for_error("UNKNOWN") is CiExitCode.CLIENT_OR_PROTOCOL_ERROR
    assert exit_code_for_status("succeeded") is CiExitCode.SUCCESS
    assert exit_code_for_status("succeeded", cleanup_succeeded=False) is CiExitCode.CLEANUP_FAILED
    assert exit_code_for_status("cancelled") is CiExitCode.WORKFLOW_CANCELLED
    assert exit_code_for_status("timed_out") is CiExitCode.SESSION_TIMED_OUT
    assert (
        exit_code_for_status("failed", error_code="WORKFLOW_ASSERTION_FAILED")
        is CiExitCode.HARDWARE_TEST_FAILED
    )
    assert exit_code_for_status("failed") is CiExitCode.WORKFLOW_FAILED


def test_github_summary_rendering_escapes_values_and_appends(tmp_path: Path) -> None:
    summary = HardwareCiSummary(
        status="Passed",
        bench_id="bench|01",
        backend="simlab",
        firmware="1.2.3<script>",
        duration_seconds=62,
        cleanup_status="Completed",
        steps=(
            CiSummaryStep("Probe target", "succeeded"),
            CiSummaryStep("Verify\nself-test", "failed"),
        ),
        artifacts=("serial.log", "report`name.xml"),
    )

    rendered = render_github_summary(summary)
    assert rendered.startswith("## Hardware CI Result\n")
    assert "bench\\|01" in rendered
    assert "1.2.3&lt;script&gt;" in rendered
    assert "| Duration | 1m 2s |" in rendered
    assert "- ✓ Probe target — succeeded" in rendered
    assert "- ✗ Verify self-test — failed" in rendered
    assert "`report\\`name.xml`" in rendered

    destination = tmp_path / "summary.md"
    assert append_github_summary(summary, {}) is None
    assert append_github_summary(summary, {"GITHUB_STEP_SUMMARY": str(destination)}) == destination
    assert append_github_summary(summary, {"GITHUB_STEP_SUMMARY": str(destination)}) == destination
    assert destination.read_text(encoding="utf-8").count("## Hardware CI Result") == 2

    compact = render_github_summary(
        HardwareCiSummary(
            status="Running",
            duration_seconds=4.5,
            steps=(
                CiSummaryStep("Waiting", "running"),
                CiSummaryStep("Optional", "skipped"),
            ),
        )
    )
    assert "| Duration | 4.5 seconds |" in compact
    assert "- … Waiting — running" in compact
    assert "- ○ Optional — skipped" in compact


def test_ci_integration_assets_are_valid_and_keep_token_out_of_arguments(tmp_path: Path) -> None:
    action_path = ROOT / "integrations/github-action/action.yml"
    entrypoint = ROOT / "integrations/github-action/entrypoint.sh"
    gitlab = ROOT / "integrations/gitlab/hardware-test.yml"
    jenkins = ROOT / "integrations/jenkins/Jenkinsfile.example"
    github_workflow = ROOT / ".github/workflows/hardware-test.yml"

    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["token"]["required"] is True
    assert action["inputs"]["allow-physical"]["default"] == "false"
    assert set(action["outputs"]) == {
        "session-id",
        "bench-id",
        "workflow-run-id",
        "result",
        "artifact-directory",
    }
    gitlab_job = yaml.safe_load(gitlab.read_text(encoding="utf-8"))
    assert isinstance(gitlab_job, dict)
    assert gitlab_job["hardware-test"]["tags"] == ["lab-network"]
    jenkins_pipeline = jenkins.read_text(encoding="utf-8")
    assert "agent { label 'lab-network' }" in jenkins_pipeline
    assert "agent any" not in jenkins_pipeline
    assert "set +x" in jenkins_pipeline
    dogfood = yaml.safe_load(github_workflow.read_text(encoding="utf-8"))
    assert isinstance(dogfood, dict)
    dogfood_inputs = dogfood["jobs"]["hardware-test"]["steps"][2]["with"]
    assert dogfood_inputs["allow-physical"] == "false"
    assert dogfood_inputs["required-labels"] == "board=esp32,location=simulation"
    subprocess.run(["bash", "-n", str(entrypoint)], check=True)

    recorder = tmp_path / "fake labctl"
    recorder.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'token=<%s>\\n' \"$LAB_PLATFORM_TOKEN\"\n"
        'for value in "$@"; do printf \'arg=<%s>\\n\' "$value"; done\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    firmware = tmp_path / "firmware with spaces.bin"
    firmware.write_bytes(b"firmware")
    environment = {
        **os.environ,
        "LABCTL_BIN": str(recorder),
        "LAB_PLATFORM_SERVER": "https://agent.example",
        "LAB_PLATFORM_TOKEN": "top-secret",
        "LAB_ACTION_WORKFLOW": "esp32-ci-test",
        "LAB_ACTION_FIRMWARE": str(firmware),
        "LAB_ACTION_EXPECTED_VERSION": "commit;not-a-command",
        "LAB_ACTION_REQUIRED_CAPABILITIES": "firmware,serial",
        "LAB_ACTION_REQUIRED_LABELS": "board=esp32,location=test lab",
        "LAB_ACTION_ALLOW_SIMULATED": "true",
        "LAB_ACTION_ALLOW_PHYSICAL": "false",
    }
    completed = subprocess.run(
        [str(entrypoint)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    arguments = [line for line in completed.stdout.splitlines() if line.startswith("arg=")]
    assert "token=<top-secret>" in completed.stdout
    assert not any("top-secret" in argument for argument in arguments)
    assert f"arg=<firmware={firmware}>" in arguments
    assert "arg=<expected_version=commit;not-a-command>" in arguments
    assert "arg=<location=test lab>" in arguments
    assert "arg=<--allow-simulated>" in arguments
    assert "arg=<--allow-physical>" not in arguments
    assert "arg=<--no-allow-physical>" in arguments

    environment["LAB_ACTION_ALLOW_SIMULATED"] = "false"
    environment["LAB_ACTION_ALLOW_PHYSICAL"] = "true"
    reversed_backends = subprocess.run(
        [str(entrypoint)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    reversed_arguments = [
        line for line in reversed_backends.stdout.splitlines() if line.startswith("arg=")
    ]
    assert "arg=<--no-allow-simulated>" in reversed_arguments
    assert "arg=<--allow-physical>" in reversed_arguments
