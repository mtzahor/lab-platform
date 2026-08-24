from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml
from lab_platform.persistence import MINIMUM_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION

from scripts import deployment_acceptance as acceptance

ROOT = Path(__file__).resolve().parents[2]


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
