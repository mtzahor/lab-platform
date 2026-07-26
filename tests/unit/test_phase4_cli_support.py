from __future__ import annotations

import json
import os
import subprocess
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
from lab_platform.cli.ci_environment import CiProvider, detect_ci_environment
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

ROOT = Path(__file__).resolve().parents[2]


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
