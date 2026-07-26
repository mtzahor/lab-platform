from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from lab_platform.cli.ci_environment import CiEnvironment, CiProvider
from lab_platform.cli.ci_exit_codes import CiExitCode

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
