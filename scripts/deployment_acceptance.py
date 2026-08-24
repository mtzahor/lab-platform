#!/usr/bin/env python3
"""Exercise the Phase 8 published-image deployment, upgrade, and recovery cut-line."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar, cast
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = REPOSITORY_ROOT / "deploy" / "acceptance" / "compose.yaml"
CONTROL_PLANE_CONFIG = "/etc/lab-platform/control-plane.yaml"
BACKUP_PATH = "/var/lib/lab-platform/backups/deployment-acceptance.tar.zst"
DEMO_USERNAME = "demo-admin"
DEMO_PASSWORD = "LabPlatform-Demo-Only!"
WORKFLOW_NAME = "demo-smoke-test"
CURRENT_SCHEMA = 12
PREVIOUS_SCHEMA = 11
VOLUME_SUFFIXES = (
    "postgres-data",
    "artifact-data",
    "backup-data",
    "bootstrap-state",
    "agent-data",
    "agent-artifacts",
)
CONTROL_PLANE_VOLUME_SUFFIXES = ("postgres-data", "artifact-data")
REQUIRED_ENTITY_COUNTS = (
    "organisations",
    "users",
    "roles",
    "agents",
    "benches",
    "reservations",
    "workflows",
    "workflow_history",
    "artifacts",
    "audit_events",
)
VOLUME_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{4,50}$")
PREVIOUS_SCHEMA_SQL = """
DROP INDEX IF EXISTS artifacts_retention_due;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_claim_token;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_claimed_at;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_attempt_count;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_last_error;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_deleted_at;
ALTER TABLE artifacts DROP COLUMN IF EXISTS retention_state;
DELETE FROM schema_migrations WHERE version = 12;
"""
ENTITY_COUNTS_SQL = """
SELECT json_build_object(
  'organisations', (SELECT COUNT(*) FROM organisations),
  'users', (SELECT COUNT(*) FROM users),
  'roles', (SELECT COUNT(*) FROM organisation_memberships),
  'agents', (SELECT COUNT(*) FROM agents),
  'benches', (SELECT COUNT(*) FROM global_benches),
  'reservations', (SELECT COUNT(*) FROM reservation_leases),
  'workflows', (SELECT COUNT(*) FROM workflows),
  'workflow_history', (
    SELECT COUNT(*) FROM distributed_operations WHERE operation_type = 'RUN_WORKFLOW'
  ),
  'artifacts', (SELECT COUNT(*) FROM artifacts),
  'audit_events', (SELECT COUNT(*) FROM audit_events)
)::text;
"""

JsonObject = dict[str, object]
T = TypeVar("T")


class AcceptanceError(RuntimeError):
    """A release-blocking acceptance assertion failed."""


@dataclass(frozen=True, slots=True)
class PersistentSnapshot:
    organisation_id: str
    user_id: str
    agent_id: str
    bench_ids: tuple[str, ...]
    workflow_run_id: str
    reservation_id: str
    artifact_id: str
    audit_event_id: str


class ComposeDeployment:
    def __init__(
        self,
        *,
        compose_file: Path,
        project_name: str,
        volume_prefix: str,
        control_plane_image: str,
        agent_image: str,
        port: int,
    ) -> None:
        self.volume_prefix = volume_prefix
        self.environment = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": project_name,
            "LAB_ACCEPTANCE_VOLUME_PREFIX": volume_prefix,
            "LAB_ACCEPTANCE_PORT": str(port),
            "LAB_CONTROL_PLANE_IMAGE": control_plane_image,
            "LAB_AGENT_IMAGE": agent_image,
        }
        self.command = (
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--file",
            str(compose_file),
        )

    def run(
        self,
        *arguments: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector
            [*self.command, *arguments],
            cwd=REPOSITORY_ROOT,
            env=self.environment,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            check=False,
        )
        if check and completed.returncode != 0:
            output = (completed.stdout or "").strip()
            if len(output) > 6_000:
                output = output[-6_000:]
            raise AcceptanceError(
                f"Compose command failed ({completed.returncode}): {' '.join(arguments)}\n{output}"
            )
        return completed

    def control_plane(self, *arguments: str, capture: bool = False) -> str:
        completed = self.run(
            "run",
            "--rm",
            "--no-deps",
            "control-plane",
            *arguments,
            capture=capture,
        )
        return completed.stdout or ""

    def psql(self, sql: str) -> str:
        completed = self.run(
            "exec",
            "-T",
            "postgres",
            "psql",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--username",
            "lab_platform_acceptance",
            "--dbname",
            "lab_platform_acceptance",
            capture=True,
            input_text=sql,
        )
        return (completed.stdout or "").strip()

    def volume_name(self, suffix: str) -> str:
        if suffix not in VOLUME_SUFFIXES:
            raise ValueError(f"unknown acceptance volume suffix: {suffix}")
        return f"{self.volume_prefix}-{suffix}"

    def ensure_volumes_are_new(self) -> None:
        existing = [
            self.volume_name(suffix)
            for suffix in VOLUME_SUFFIXES
            if subprocess.run(  # noqa: S603 - fixed Docker inspection command
                ["docker", "volume", "inspect", self.volume_name(suffix)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        ]
        if existing:
            raise AcceptanceError(
                "refusing to reuse pre-existing acceptance volumes: " + ", ".join(existing)
            )

    def remove_volumes(self, suffixes: Sequence[str], *, required: bool = False) -> None:
        for suffix in suffixes:
            completed = subprocess.run(  # noqa: S603 - exact validated volume name
                ["docker", "volume", "rm", self.volume_name(suffix)],
                stdout=subprocess.PIPE if required else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if required else subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if required and completed.returncode != 0:
                raise AcceptanceError(
                    f"could not remove control-plane volume {self.volume_name(suffix)}: "
                    f"{(completed.stdout or '').strip()}"
                )


class AcceptanceHttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None

    def request_bytes(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        headers = {"Accept": "application/json"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback
                return response.read(), cast(Mapping[str, str], response.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2_000:]
            raise AcceptanceError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AcceptanceError(f"{method} {path} failed: {exc.reason}") from exc

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
    ) -> JsonObject:
        body = None
        content_type = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        raw, _headers = self.request_bytes(
            path,
            method=method,
            body=body,
            content_type=content_type,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise AcceptanceError(f"{method} {path} did not return a JSON object")
        return cast(JsonObject, parsed)

    def login(self) -> JsonObject:
        response = self.request_json(
            "/api/v1/auth/login",
            method="POST",
            payload={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token.startswith("lps_"):
            raise AcceptanceError("local login did not return an identity access token")
        self.token = token
        return response

    def collection(self, path: str) -> list[JsonObject]:
        payload = self.request_json(path)
        items = payload.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AcceptanceError(f"{path} did not return an item collection")
        return cast(list[JsonObject], items)

    def upload_artifact(self, owner_id: str, content: bytes) -> JsonObject:
        digest = hashlib.sha256(content).hexdigest()
        body, content_type = encode_multipart(
            {
                "owner_type": "operation",
                "owner_id": owner_id,
                "artifact_type": "acceptance_log",
                "expected_sha256": digest,
                "idempotency_key": "phase8-deployment-acceptance-artifact",
            },
            filename="deployment-acceptance.txt",
            content=content,
            file_content_type="text/plain",
        )
        raw, _headers = self.request_bytes(
            "/api/v1/artifacts",
            method="POST",
            body=body,
            content_type=content_type,
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise AcceptanceError("artifact upload did not return a JSON object")
        artifact = cast(JsonObject, parsed)
        if artifact.get("sha256") != digest or artifact.get("size_bytes") != len(content):
            raise AcceptanceError("uploaded artifact digest or size did not match")
        return artifact


def encode_multipart(
    fields: Mapping[str, str],
    *,
    filename: str,
    content: bytes,
    file_content_type: str,
) -> tuple[bytes, str]:
    boundary = f"lab-platform-{uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n').encode(),
            f"Content-Type: {file_content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def wait_for(label: str, probe: Callable[[], T | None], *, timeout: float) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result is not None:
                return result
        except (AcceptanceError, OSError, ValueError) as exc:
            last_error = exc
        time.sleep(0.5)
    suffix = f": {last_error}" if last_error is not None else ""
    raise AcceptanceError(f"timed out waiting for {label}{suffix}")


def wait_for_http(client: AcceptanceHttpClient, *, timeout: float) -> JsonObject:
    def probe() -> JsonObject | None:
        payload = client.request_json("/health/ready")
        return payload

    return wait_for("control-plane readiness through the web proxy", probe, timeout=timeout)


def wait_for_agent(
    client: AcceptanceHttpClient,
    *,
    timeout: float,
    expected_id: str | None = None,
) -> JsonObject:
    def probe() -> JsonObject | None:
        for agent in client.collection("/api/v1/agents"):
            if expected_id is not None and agent.get("id") != expected_id:
                continue
            bench_count = agent.get("bench_count")
            if (
                str(agent.get("status", "")).upper() == "ONLINE"
                and isinstance(bench_count, int)
                and bench_count >= 2
            ):
                return agent
        return None

    return wait_for("connected SimLab Agent and inventory", probe, timeout=timeout)


def run_workflow(
    client: AcceptanceHttpClient,
    *,
    key: str,
    timeout: float,
) -> JsonObject:
    launched = client.request_json(
        f"/api/v1/workflows/{WORKFLOW_NAME}/runs",
        method="POST",
        payload={
            "idempotency_key": key,
            "inputs": {},
            "kind": "SIMULATED",
            "agent_labels": {"deployment": "disposable-demo"},
            "reservation_duration_seconds": 300,
            "lease_ttl_seconds": 60,
            "command_timeout_seconds": 120,
        },
    )
    operation = _mapping(launched.get("operation"), "launched operation")
    operation_id = _text(operation.get("id"), "launched operation ID")

    def probe() -> JsonObject | None:
        workflow = client.request_json(f"/api/v1/workflow-runs/{operation_id}")
        status = str(workflow.get("status", "")).casefold()
        if status == "succeeded":
            return workflow
        if status in {"failed", "cancelled", "expired"}:
            raise AcceptanceError(f"workflow {operation_id} reached terminal status {status}")
        return None

    return wait_for(f"workflow {operation_id}", probe, timeout=timeout)


def capture_snapshot(
    client: AcceptanceHttpClient,
    workflow: JsonObject,
    artifact_content: bytes,
) -> PersistentSnapshot:
    me = client.request_json("/api/v1/auth/me")
    principal = _mapping(me.get("principal"), "authenticated principal")
    organisation = _mapping(me.get("organisation"), "authenticated organisation")
    agent = wait_for_agent(client, timeout=30)
    benches = client.collection("/api/v1/benches?online=true")
    if len(benches) < 2:
        raise AcceptanceError("SimLab did not publish its two expected benches")
    workflow_run_id = _text(workflow.get("id"), "workflow run ID")
    reservation_id = _text(workflow.get("reservation_id"), "workflow reservation ID")
    artifact = client.upload_artifact(workflow_run_id, artifact_content)
    artifact_id = _text(artifact.get("id"), "artifact ID")
    audit_events = client.collection(
        "/api/v1/audit-events?"
        + urllib.parse.urlencode({"action": "ARTIFACT_UPLOADED", "resource_id": artifact_id})
    )
    if not audit_events:
        raise AcceptanceError("artifact upload did not create a durable audit event")
    workflows = client.collection("/api/v1/workflows")
    if WORKFLOW_NAME not in {str(item.get("name")) for item in workflows}:
        raise AcceptanceError("bootstrap workflow is missing")
    reservations = client.collection("/api/v1/reservations?limit=500")
    if not _collection_contains_id(reservations, reservation_id):
        raise AcceptanceError("workflow reservation history is missing")
    return PersistentSnapshot(
        organisation_id=_text(organisation.get("id"), "organisation ID"),
        user_id=_text(principal.get("id"), "principal ID"),
        agent_id=_text(agent.get("id"), "Agent ID"),
        bench_ids=tuple(sorted(_text(item.get("id"), "bench ID") for item in benches)),
        workflow_run_id=workflow_run_id,
        reservation_id=reservation_id,
        artifact_id=artifact_id,
        audit_event_id=_text(audit_events[0].get("id"), "audit event ID"),
    )


def verify_restored_state(
    client: AcceptanceHttpClient,
    snapshot: PersistentSnapshot,
    artifact_content: bytes,
    *,
    timeout: float,
) -> None:
    login = client.login()
    principal = _mapping(login.get("principal"), "restored principal")
    organisation = _mapping(login.get("organisation"), "restored organisation")
    _expect_equal("restored user", principal.get("id"), snapshot.user_id)
    _expect_equal("restored organisation", organisation.get("id"), snapshot.organisation_id)
    agent = wait_for_agent(client, timeout=timeout, expected_id=snapshot.agent_id)
    _expect_equal("reconnected Agent", agent.get("id"), snapshot.agent_id)
    benches = client.collection("/api/v1/benches?online=true")
    restored_benches = {str(item.get("id")) for item in benches}
    if not set(snapshot.bench_ids).issubset(restored_benches):
        raise AcceptanceError("restored Agent inventory did not retain every original bench ID")
    workflow = client.request_json(f"/api/v1/workflow-runs/{snapshot.workflow_run_id}")
    _expect_equal("restored workflow status", str(workflow.get("status", "")).lower(), "succeeded")
    reservations = client.collection("/api/v1/reservations?limit=500")
    if not _collection_contains_id(reservations, snapshot.reservation_id):
        raise AcceptanceError("restored reservation history is missing")
    artifact = client.request_json(f"/api/v1/artifacts/{snapshot.artifact_id}")
    _expect_equal("restored artifact ID", artifact.get("id"), snapshot.artifact_id)
    restored_content, _headers = client.request_bytes(
        f"/api/v1/artifacts/{snapshot.artifact_id}/content"
    )
    if restored_content != artifact_content:
        raise AcceptanceError("restored artifact bytes do not match the uploaded content")
    audit_event = client.request_json(f"/api/v1/audit-events/{snapshot.audit_event_id}")
    _expect_equal("restored audit event", audit_event.get("id"), snapshot.audit_event_id)


def entity_counts(deployment: ComposeDeployment) -> dict[str, int]:
    raw = deployment.psql(ENTITY_COUNTS_SQL)
    try:
        payload = json.loads(raw.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"PostgreSQL returned invalid entity-count evidence: {raw}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceError("PostgreSQL entity-count evidence was not an object")
    counts: dict[str, int] = {}
    for key in REQUIRED_ENTITY_COUNTS:
        value = payload.get(key)
        if not isinstance(value, int):
            raise AcceptanceError(f"entity-count evidence is missing integer field {key}")
        counts[key] = value
    return counts


def require_entity_data(counts: Mapping[str, int]) -> None:
    missing = [name for name in REQUIRED_ENTITY_COUNTS if counts.get(name, 0) < 1]
    if missing:
        raise AcceptanceError(
            "required persistent entity families are empty: " + ", ".join(missing)
        )


def require_preserved_counts(before: Mapping[str, int], after: Mapping[str, int]) -> None:
    lost = [
        f"{name} ({before[name]} -> {after.get(name, 0)})"
        for name in REQUIRED_ENTITY_COUNTS
        if after.get(name, 0) < before[name]
    ]
    if lost:
        raise AcceptanceError("persistent entity counts regressed: " + ", ".join(lost))


def schema_status(deployment: ComposeDeployment) -> JsonObject:
    output = deployment.control_plane(
        "db",
        "status",
        "--config",
        CONTROL_PLANE_CONFIG,
        "--output",
        "json",
        capture=True,
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"database status returned invalid JSON: {output[-2_000:]}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceError("database status did not return a JSON object")
    return cast(JsonObject, payload)


def verify_dashboard(client: AcceptanceHttpClient) -> None:
    content, headers = client.request_bytes("/")
    content_type = str(headers.get("content-type", "")).lower()
    if "text/html" not in content_type or b'id="root"' not in content:
        raise AcceptanceError("published control-plane image did not serve the dashboard bundle")


def execute(args: argparse.Namespace) -> dict[str, object]:
    volume_prefix = args.volume_prefix or f"lab-platform-acceptance-{secrets.token_hex(4)}"
    validate_volume_prefix(volume_prefix)
    project_name = volume_prefix.replace("_", "-").replace(".", "-")
    deployment = ComposeDeployment(
        compose_file=args.compose_file.resolve(),
        project_name=project_name,
        volume_prefix=volume_prefix,
        control_plane_image=args.control_plane_image,
        agent_image=args.agent_image,
        port=args.port,
    )
    client = AcceptanceHttpClient(f"http://127.0.0.1:{args.port}")
    artifact_content = b"phase-8 deployment acceptance artifact\n"
    work_directory = args.work_directory.resolve()
    work_directory.mkdir(parents=True, exist_ok=True)
    evidence_path = work_directory / "evidence.json"
    log_path = work_directory / "compose.log"
    evidence: dict[str, object] = {
        "status": "running",
        "control_plane_image": args.control_plane_image,
        "agent_image": args.agent_image,
        "project_name": project_name,
        "volume_prefix": volume_prefix,
        "hardware_required": False,
        "steps": [],
    }
    steps = cast(list[str], evidence["steps"])
    deployment.ensure_volumes_are_new()
    try:
        print("Starting tagged-image PostgreSQL, control plane/web, and SimLab Agent...")
        deployment.run("up", "--detach")
        wait_for_http(client, timeout=args.timeout)
        verify_dashboard(client)
        version = client.request_json("/api/v1/version")
        if args.expected_version is not None:
            _expect_equal("control-plane version", version.get("version"), args.expected_version)
        login = client.login()
        wait_for_agent(client, timeout=args.timeout)
        steps.extend(
            ["deployment_ready", "dashboard_served", "owner_authenticated", "agent_enrolled"]
        )

        print("Running the pre-backup SimLab workflow and uploading an artifact...")
        first_workflow = run_workflow(
            client,
            key="phase8-deployment-acceptance-before-restore",
            timeout=args.timeout,
        )
        snapshot = capture_snapshot(client, first_workflow, artifact_content)
        before_counts = entity_counts(deployment)
        require_entity_data(before_counts)
        evidence.update(
            {
                "version": version,
                "snapshot": asdict(snapshot),
                "entity_counts_before_backup": before_counts,
                "login_organisation": login.get("organisation"),
            }
        )
        steps.extend(["workflow_before_restore_succeeded", "artifact_uploaded"])

        print("Quiescing application services and creating a deep-verified backup...")
        deployment.run("stop", "agent", "web", "control-plane")
        deployment.control_plane(
            "backup",
            "create",
            "--config",
            CONTROL_PLANE_CONFIG,
            "--destination",
            BACKUP_PATH,
        )
        deployment.control_plane(
            "backup",
            "verify",
            BACKUP_PATH,
            "--config",
            CONTROL_PLANE_CONFIG,
            "--output",
            "json",
        )
        deployment.control_plane(
            "upgrade",
            "check",
            BACKUP_PATH,
            "--config",
            CONTROL_PLANE_CONFIG,
            "--target-version",
            args.expected_version or str(version.get("version", "")),
            "--output",
            "json",
        )
        steps.extend(["backup_created", "backup_deep_verified", "upgrade_preflight_passed"])

        print("Validating the supported previous-schema PostgreSQL migration with product data...")
        deployment.psql(PREVIOUS_SCHEMA_SQL)
        before_migration = schema_status(deployment)
        _expect_equal("previous schema state", before_migration.get("state"), "upgrade_required")
        _expect_equal(
            "previous schema version", before_migration.get("current_version"), PREVIOUS_SCHEMA
        )
        deployment.control_plane(
            "db",
            "migrate",
            "--config",
            CONTROL_PLANE_CONFIG,
            "--output",
            "json",
        )
        after_migration = schema_status(deployment)
        _expect_equal("migrated schema state", after_migration.get("state"), "current")
        _expect_equal(
            "migrated schema version", after_migration.get("current_version"), CURRENT_SCHEMA
        )
        deployment.control_plane("db", "check", "--config", CONTROL_PLANE_CONFIG)
        after_migration_counts = entity_counts(deployment)
        require_preserved_counts(before_counts, after_migration_counts)
        evidence.update(
            {
                "schema_before_migration": before_migration,
                "schema_after_migration": after_migration,
                "entity_counts_after_migration": after_migration_counts,
            }
        )
        steps.append("previous_schema_migrated_with_data")

        print(
            "Destroying control-plane state and restoring into fresh database/artifact volumes..."
        )
        deployment.run("down", "--remove-orphans")
        deployment.remove_volumes(CONTROL_PLANE_VOLUME_SUFFIXES, required=True)
        deployment.run("up", "--detach", "postgres")

        def postgres_ready() -> bool | None:
            completed = deployment.run(
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "--username",
                "lab_platform_acceptance",
                "--dbname",
                "lab_platform_acceptance",
                capture=True,
                check=False,
            )
            return True if completed.returncode == 0 else None

        wait_for("fresh PostgreSQL volume", postgres_ready, timeout=args.timeout)
        deployment.control_plane(
            "backup",
            "restore",
            BACKUP_PATH,
            "--config",
            CONTROL_PLANE_CONFIG,
            "--yes",
        )
        restored_schema = schema_status(deployment)
        _expect_equal("restored schema state", restored_schema.get("state"), "current")
        deployment.control_plane("db", "check", "--config", CONTROL_PLANE_CONFIG)
        deployment.control_plane("doctor", "--config", CONTROL_PLANE_CONFIG, "--output", "json")
        restored_counts = entity_counts(deployment)
        require_preserved_counts(before_counts, restored_counts)
        steps.extend(["fresh_control_plane_state_created", "backup_restored"])

        print("Starting the restored service, reconnecting the Agent, and verifying history...")
        deployment.run("up", "--detach", "--no-deps", "control-plane")
        deployment.run("up", "--detach", "--no-deps", "web")
        wait_for_http(client, timeout=args.timeout)
        verify_dashboard(client)
        deployment.run("up", "--detach", "--no-deps", "agent")
        client.token = None
        verify_restored_state(
            client,
            snapshot,
            artifact_content,
            timeout=args.timeout,
        )
        second_workflow = run_workflow(
            client,
            key="phase8-deployment-acceptance-after-restore",
            timeout=args.timeout,
        )
        second_workflow_id = _text(second_workflow.get("id"), "second workflow ID")
        if second_workflow_id == snapshot.workflow_run_id:
            raise AcceptanceError("post-restore workflow did not create a new durable run")
        final_counts = entity_counts(deployment)
        require_preserved_counts(restored_counts, final_counts)
        evidence.update(
            {
                "restored_schema": restored_schema,
                "entity_counts_after_restore": restored_counts,
                "entity_counts_final": final_counts,
                "workflow_run_after_restore": second_workflow_id,
            }
        )
        steps.extend(
            [
                "dashboard_restored",
                "history_verified",
                "artifact_bytes_verified",
                "agent_reconnected",
                "workflow_after_restore_succeeded",
            ]
        )
        evidence["status"] = "passed"
        print(f"Published-image deployment acceptance passed; evidence: {evidence_path}")
        return evidence
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        logs = deployment.run(
            "logs",
            "--no-color",
            "--timestamps",
            capture=True,
            check=False,
        ).stdout
        log_path.write_text(logs or "<no Compose logs available>\n", encoding="utf-8")
        raise
    finally:
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if args.keep:
            print(f"Keeping acceptance deployment {project_name} for inspection.")
        else:
            deployment.run("down", "--remove-orphans", capture=True, check=False)
            deployment.remove_volumes(VOLUME_SUFFIXES)


def validate_volume_prefix(prefix: str) -> None:
    if VOLUME_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise AcceptanceError(
            "volume prefix must be 5-51 lowercase letters, digits, dots, underscores, or hyphens"
        )


def _mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} is missing or is not an object")
    return cast(JsonObject, value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AcceptanceError(f"{label} is missing or empty")
    return value


def _expect_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AcceptanceError(f"{label} mismatch: expected {expected!r}, received {actual!r}")


def _collection_contains_id(items: Sequence[Mapping[str, object]], expected_id: str) -> bool:
    for item in items:
        if item.get("id") == expected_id:
            return True
        reservation = item.get("reservation")
        if isinstance(reservation, Mapping) and reservation.get("id") == expected_id:
            return True
    return False


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--control-plane-image",
        default=os.environ.get("LAB_CONTROL_PLANE_IMAGE"),
        help="Tagged or digest-pinned control-plane image under acceptance.",
    )
    parser.add_argument(
        "--agent-image",
        default=os.environ.get("LAB_AGENT_IMAGE"),
        help="Tagged or digest-pinned Agent image under acceptance.",
    )
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--work-directory", type=Path, default=Path("build/deployment-acceptance"))
    parser.add_argument("--volume-prefix", default=None)
    parser.add_argument("--port", type=int, default=18_090)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--keep", action="store_true")
    parsed = parser.parse_args(argv)
    if not parsed.control_plane_image or not parsed.agent_image:
        parser.error("--control-plane-image and --agent-image are required")
    if not parsed.compose_file.is_file():
        parser.error(f"Compose file does not exist: {parsed.compose_file}")
    if not 1 <= parsed.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if parsed.timeout <= 0:
        parser.error("--timeout must be positive")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    try:
        execute(arguments(argv))
    except (AcceptanceError, OSError) as exc:
        print(f"deployment acceptance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
