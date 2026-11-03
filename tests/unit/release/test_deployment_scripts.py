from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest
import yaml
from lab_platform.persistence import MINIMUM_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION

from scripts import deployment_acceptance as acceptance
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_acceptance_compose_consumes_images_without_building_source() -> None:
    compose = yaml.safe_load((ROOT / "deploy/acceptance/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"postgres", "migrate", "bootstrap", "control-plane", "web", "agent"}
    assert all("build" not in service for service in services.values())
    assert services["control-plane"]["image"].startswith("${LAB_CONTROL_PLANE_IMAGE:")
    assert services["agent"]["image"].startswith("${LAB_AGENT_IMAGE:")
    assert services["postgres"]["image"].startswith("postgres:16.15-alpine@sha256:")
    assert services["web"]["image"].startswith("caddy:2.11.4-alpine@sha256:")
    assert services["agent"]["network_mode"] == "service:control-plane"
    assert services["web"]["network_mode"] == "service:control-plane"
    assert services["web"]["cap_drop"] == ["ALL"]
    assert services["web"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert services["control-plane"]["ports"] == ["127.0.0.1:${LAB_ACCEPTANCE_PORT:-18090}:8080"]
    assert all(
        mount.split(":", 1)[0] in {"./control-plane.yaml", "./Caddyfile"}
        or not mount.startswith(".")
        for service in services.values()
        for mount in service.get("volumes", [])
    )


def test_acceptance_compose_keeps_external_agent_state_separate_from_fresh_restore() -> None:
    compose = yaml.safe_load((ROOT / "deploy/acceptance/compose.yaml").read_text(encoding="utf-8"))
    volumes = compose["volumes"]

    assert volumes["postgres-data"]["name"].endswith("-postgres-data")
    assert volumes["artifact-data"]["name"].endswith("-artifact-data")
    assert volumes["backup-data"]["name"].endswith("-backup-data")
    assert volumes["bootstrap-state"]["name"].endswith("-bootstrap-state")
    assert set(acceptance.CONTROL_PLANE_VOLUME_SUFFIXES) == {"postgres-data", "artifact-data"}
    assert "bootstrap-state" not in acceptance.CONTROL_PLANE_VOLUME_SUFFIXES
    assert "agent-data" not in acceptance.CONTROL_PLANE_VOLUME_SUFFIXES
    assert compose["services"]["bootstrap"]["volumes"][-1].endswith(":/run/lab-platform-demo")
    assert compose["services"]["agent"]["volumes"][0].endswith(":/run/lab-platform-demo:ro")


def test_acceptance_schema_constants_and_non_root_backup_mount_cannot_drift() -> None:
    assert acceptance.CURRENT_SCHEMA == SCHEMA_VERSION
    assert acceptance.PREVIOUS_SCHEMA == MINIMUM_SUPPORTED_SCHEMA_VERSION
    assert acceptance.BACKUP_PATH.startswith("/var/lib/lab-platform/backups/")
    dockerfile = (ROOT / "docker/control-plane/Dockerfile").read_text(encoding="utf-8")
    assert "/var/lib/lab-platform/backups" in dockerfile


def test_control_plane_postgresql_client_matches_the_pinned_server() -> None:
    compose = yaml.safe_load((ROOT / "deploy/acceptance/compose.yaml").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "docker/control-plane/Dockerfile").read_text(encoding="utf-8")

    server = re.search(r"postgres:(\d+)[.]", compose["services"]["postgres"]["image"])
    client = re.search(r"postgresql-client-(\d+)", dockerfile)
    assert server is not None
    assert client is not None
    assert client.group(1) == server.group(1)


def test_previous_schema_fixture_is_exact_and_role_evidence_uses_membership() -> None:
    assert "DELETE FROM schema_migrations WHERE version = 12" in acceptance.PREVIOUS_SCHEMA_SQL
    assert "artifacts_retention_due" in acceptance.PREVIOUS_SCHEMA_SQL
    for column in (
        "retention_claim_token",
        "retention_claimed_at",
        "retention_attempt_count",
        "retention_last_error",
        "retention_deleted_at",
        "retention_state",
    ):
        assert f"DROP COLUMN IF EXISTS {column}" in acceptance.PREVIOUS_SCHEMA_SQL
    assert "'roles', (SELECT COUNT(*) FROM organisation_memberships)" in (
        acceptance.ENTITY_COUNTS_SQL
    )
    assert "COUNT(*) FROM role_assignments" not in acceptance.ENTITY_COUNTS_SQL


def test_workflow_owned_reservation_request_does_not_request_existing_reservation_release() -> None:
    class FakeClient:
        launch_payload: dict[str, object] | None = None

        def request_json(
            self,
            path: str,
            *,
            method: str = "GET",
            payload: dict[str, object] | None = None,
        ) -> dict[str, object]:
            if path.endswith("/runs"):
                assert method == "POST"
                assert payload is not None
                self.launch_payload = payload
                return {"operation": {"id": "00000000-0000-0000-0000-000000000101"}}
            return {
                "id": "00000000-0000-0000-0000-000000000101",
                "reservation_id": "00000000-0000-0000-0000-000000000102",
                "status": "succeeded",
            }

    client = FakeClient()

    result = acceptance.run_workflow(
        cast(acceptance.AcceptanceHttpClient, client),
        key="workflow-owned-reservation",
        timeout=0.1,
    )

    assert result["status"] == "succeeded"
    assert client.launch_payload is not None
    assert "reservation_id" not in client.launch_payload
    assert "release_reservation_after" not in client.launch_payload


def test_entity_evidence_requires_every_phase8_family_and_never_allows_regression() -> None:
    complete = {name: index + 1 for index, name in enumerate(acceptance.REQUIRED_ENTITY_COUNTS)}
    acceptance.require_entity_data(complete)
    acceptance.require_preserved_counts(complete, complete)

    missing = dict(complete)
    missing["audit_events"] = 0
    with pytest.raises(acceptance.AcceptanceError, match="audit_events"):
        acceptance.require_entity_data(missing)

    regressed = dict(complete)
    regressed["workflows"] -= 1
    with pytest.raises(acceptance.AcceptanceError, match="workflows"):
        acceptance.require_preserved_counts(complete, regressed)


def test_multipart_artifact_body_is_bounded_and_contains_digest_fields() -> None:
    content = b"phase-8-artifact\n"
    body, content_type = acceptance.encode_multipart(
        {
            "owner_type": "operation",
            "owner_id": "00000000-0000-0000-0000-000000000101",
        },
        filename="acceptance.txt",
        content=content,
        file_content_type="text/plain",
    )

    boundary = content_type.removeprefix("multipart/form-data; boundary=")
    assert boundary.startswith("lab-platform-")
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="owner_type"' in body
    assert b'filename="acceptance.txt"' in body
    assert content in body


@pytest.mark.parametrize(
    "prefix",
    ["UPPERCASE", "bad prefix", "../escape", "x", "a" * 52],
)
def test_acceptance_volume_prefix_rejects_unsafe_or_broad_names(prefix: str) -> None:
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.validate_volume_prefix(prefix)


def test_ci_and_release_gate_the_same_orchestrator() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))

    ci_job = ci["jobs"]["published-artifact-acceptance"]
    assert set(ci_job["needs"]) == {"quality", "deployment-manifests"}
    assert "scripts/deployment_acceptance.py" in json.dumps(ci_job)
    assert "build-push-action" in json.dumps(ci_job)

    release_job = release["jobs"]["deployment-acceptance"]
    assert set(release_job["needs"]) == {"metadata", "packages", "images"}
    serialized = json.dumps(release_job)
    assert "scripts/deployment_acceptance.py" in serialized
    assert "--expected-version" in serialized
    assert "docker pull" in serialized
    assert "deployment-acceptance" in release["jobs"]["github-release"]["needs"]


def test_acceptance_evidence_names_previous_schema_without_claiming_prior_image() -> None:
    source = (ROOT / "scripts/deployment_acceptance.py").read_text(encoding="utf-8")

    assert "previous_schema_migrated_with_data" in source
    assert "previous_minor_migrated_with_data" not in source


def test_demo_bootstrap_reuses_the_configured_default_organisation() -> None:
    source = (ROOT / "docker/demo-bootstrap.py").read_text(encoding="utf-8")

    ensure = source.index("ensure_default_organisation")
    bootstrap = source.index("bootstrap_admin")
    assert ensure < bootstrap
    assert "slug=config.identity.default_organisation_slug" in source
    assert "name=config.identity.default_organisation_name" in source


def _fake_deployment(tmp_path: Path) -> acceptance.ComposeDeployment:
    return acceptance.ComposeDeployment(
        compose_file=tmp_path / "compose.yaml",
        project_name="lab-platform-acceptance-test",
        volume_prefix="lab-platform-acceptance-test",
        control_plane_image="control-plane:test",
        agent_image="agent:test",
        port=18080,
    )


def test_schema_status_ignores_compose_progress_written_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {"current_version": SCHEMA_VERSION, "ready": True}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(status, indent=2) + "\n",
            stderr=" Container lab-platform-control-plane-run-1 Creating \n"
            " Container lab-platform-control-plane-run-1 Created \n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert acceptance.schema_status(_fake_deployment(tmp_path)) == status


def test_failed_compose_command_reports_stdout_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 2, stdout="partial output\n", stderr="no such service: control-plane\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(acceptance.AcceptanceError) as raised:
        _fake_deployment(tmp_path).run("ps", capture=True)

    assert "partial output" in str(raised.value)
    assert "no such service: control-plane" in str(raised.value)


@pytest.mark.parametrize(
    ("sent_signal", "expected_status", "expected_name"),
    [
        (signal.SIGINT, 130, "INT"),
        (signal.SIGTERM, 143, "TERM"),
    ],
)
def test_local_ci_demo_forwards_signal_without_logging_token(
    tmp_path: Path,
    sent_signal: signal.Signals,
    expected_status: int,
    expected_name: str,
) -> None:
    fake_labctl = tmp_path / "fake-labctl"
    fake_agent = tmp_path / "fake-agent"
    agent_ready_file = tmp_path / "agent-ready"
    agent_signal_file = tmp_path / "agent-signal"
    ready_file = tmp_path / "ci-ready"
    signal_file = tmp_path / "ci-signal"
    output_directory = tmp_path / "output"
    token = "demo-token-that-must-stay-secret"
    stale_token = "credential-for-another-agent"
    fake_labctl.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "health" ]]; then
  [[ -f "$LAB_DEMO_TEST_AGENT_READY_FILE" ]]
  exit
fi
if [[ "${{1:-}}" == "token" && "${{2:-}}" == "create" ]]; then
  if [[ -n "${{LAB_PLATFORM_TOKEN:-}}" ]]; then
    printf 'stale token was sent during bootstrap\n' >&2
    exit 98
  fi
  printf '{{"token":"{token}"}}\n'
  exit 0
fi
if [[ "${{1:-}}" == "ci" && "${{2:-}}" == "session" ]]; then
  printf 'session show must not run after a signal\n' >&2
  exit 99
fi
if [[ "${{1:-}}" == "ci" && "${{2:-}}" == "run" ]]; then
  for argument in "$@"; do
    if [[ "$argument" == "$LAB_PLATFORM_TOKEN" ]]; then
      printf 'token leaked into argv\n' >&2
      exit 99
    fi
  done
  trap 'printf "INT\\n" >"$LAB_DEMO_TEST_SIGNAL_FILE"; exit 16' INT
  trap 'printf "TERM\\n" >"$LAB_DEMO_TEST_SIGNAL_FILE"; exit 16' TERM
  printf 'ready\n' >"$LAB_DEMO_TEST_READY_FILE"
  printf 'CI session created: session-test\n'
  printf 'fake CI run started\n'
  while true; do sleep 0.05; done
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_labctl.chmod(0o755)
    fake_agent.write_text(
        """#!/usr/bin/env bash
set -eu
trap 'printf "TERM\\n" >"$LAB_DEMO_TEST_AGENT_SIGNAL_FILE"; exit 0' TERM
printf 'ready\n' >"$LAB_DEMO_TEST_AGENT_READY_FILE"
while true; do sleep 0.05; done
""",
        encoding="utf-8",
    )
    fake_agent.chmod(0o755)
    environment = {
        **os.environ,
        "LAB_AGENT_BIN": str(fake_agent),
        "LABCTL_BIN": str(fake_labctl),
        "LAB_DEMO_OUTPUT_DIR": str(output_directory),
        "LAB_DEMO_TEST_AGENT_READY_FILE": str(agent_ready_file),
        "LAB_DEMO_TEST_AGENT_SIGNAL_FILE": str(agent_signal_file),
        "LAB_DEMO_TEST_READY_FILE": str(ready_file),
        "LAB_DEMO_TEST_SIGNAL_FILE": str(signal_file),
        "LAB_DEMO_USE_EXISTING_AGENT": "0",
        "LAB_PLATFORM_TOKEN": stale_token,
    }

    process = subprocess.Popen(
        [str(ROOT / "scripts/local-ci-demo.sh")],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_file.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("fake labctl did not start")
            time.sleep(0.01)
        assert process.poll() is None

        process.send_signal(sent_signal)
        output, _ = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == expected_status
    assert signal_file.read_text(encoding="utf-8") == f"{expected_name}\n"
    assert agent_signal_file.read_text(encoding="utf-8") == "TERM\n"
    assert token not in output
    assert stale_token not in output
    assert token not in (output_directory / "ci-run.log").read_text(encoding="utf-8")
    assert stale_token not in (output_directory / "ci-run.log").read_text(encoding="utf-8")
