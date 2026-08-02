from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import yaml
from lab_platform.cli.ci_environment import detect_ci_environment
from lab_platform.cli.ci_exit_codes import (
    CiExitCode,
    exit_code_for_error,
    exit_code_for_status,
)
from lab_platform.cli.ci_summary import (
    CiSummaryStep,
    HardwareCiSummary,
    append_github_summary,
)
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError
from lab_platform.config import validate_config
from pydantic import ValidationError

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_MAX_FIRMWARE_BYTES = 100 * 1024 * 1024
_DURATION_PART = re.compile(r"(?P<value>\d+)(?P<unit>[hms])", re.IGNORECASE)
_CI_ASSIGNMENT_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "cleanup_pending", "completed"}
)
_CI_WORKFLOW_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_DISTRIBUTED_CI_RETRYABLE_ERRORS = frozenset(
    {"NO_COMPATIBLE_BENCH", "AGENT_OFFLINE", "AGENT_DEGRADED", "AGENT_DRAINING"}
)


class _CiArtifactUploadError(RuntimeError):
    """A local artifact could not be prepared for a CI upload."""


class _CiBenchWaitTimeout(RuntimeError):
    """The client-side bench wait deadline elapsed before assignment."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except AgentApiError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        if args.command == "ci":
            return int(exit_code_for_error(exc.code, http_status=exc.status))
        return _api_exit_code(exc)
    except AgentConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.command == "ci":
            return int(CiExitCode.CLIENT_OR_PROTOCOL_ERROR)
        return 6
    except _CiArtifactUploadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(CiExitCode.ARTIFACT_UPLOAD_FAILED)
    except KeyboardInterrupt:
        print("Hardware CI cancelled.", file=sys.stderr)
        return int(CiExitCode.WORKFLOW_CANCELLED)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.command == "ci":
            return int(CiExitCode.CLIENT_OR_PROTOCOL_ERROR)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "config" and args.config_command == "validate":
        validate_config(args.config_dir)
        print(f"Configuration valid: {args.config_dir}")
        return 0

    client = _client(args)
    if args.command == "version":
        payload = client.get("/api/v1/version")
        if args.output == "json":
            _print_json(payload)
        else:
            version = _require_mapping(payload, "version").get("version", "")
            print(f"labctl {version}")
        return 0
    if args.command == "health":
        _print_read_payload(client.get("/api/v1/health"), args.output, _health_table)
        return 0
    if args.command in {"benches", "plugins"}:
        if args.command == "plugins":
            _print_json(client.get("/plugins"))
        else:
            _bench_list(client, args)
        return 0
    if args.command == "agent":
        return _agent_command(client, args)
    if args.command == "bench":
        return _bench_command(client, args)
    if args.command == "reservation":
        return _reservation_command(client, args)
    if args.command == "workflow":
        return _workflow_command(client, args)
    if args.command == "token":
        return _token_command(client, args)
    if args.command == "ci":
        return _ci_command(client, args)
    if args.command == "operation":
        return _operation_command(client, args)
    if args.command == "event":
        payload = client.get(
            "/api/v1/events",
            {
                "bench_id": args.bench_id,
                "event_type": args.event_type,
                "limit": args.limit,
            },
        )
        _print_collection(payload, args.output, _event_table)
        return 0
    raise AssertionError("unreachable command")


def _agent_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.agent_command
    if command == "list":
        payload = client.get(
            "/api/v1/agents",
            {
                "status": args.status.upper() if args.status is not None else None,
                "location": args.location,
                "label": args.label,
                "version": args.version,
            },
        )
        _print_collection(payload, args.output, _agent_table)
        return 0
    if command == "show":
        payload = client.get(f"/api/v1/agents/{args.agent_id}")
        _print_read_payload(payload, args.output, _agent_show_table)
        return 0
    if command == "enrollment-token":
        return _agent_enrollment_token_command(client, args)
    if command == "timeline":
        payload = client.get(
            f"/api/v1/agents/{args.agent_id}/timeline",
            {
                "severity": (args.severity.upper() if args.severity is not None else None),
                "event_type": args.event_type,
                "since": args.since,
                "limit": args.limit,
            },
        )
        _print_collection(payload, args.output, _agent_timeline_table)
        return 0

    path = f"/api/v1/agents/{args.agent_id}"
    body: dict[str, object] = {}
    if command == "drain":
        path += "/drain"
        body["cancel_queued_work"] = args.cancel_queued_work
    elif command == "undrain":
        path += "/undrain"
    elif command == "revoke":
        path += "/revoke"
    elif command == "refresh":
        path += "/actions/refresh-inventory"
    else:
        raise AssertionError("unreachable Agent command")
    payload = client.post(path, body)
    if args.output == "json":
        _print_json(payload)
    elif command == "refresh":
        response = _require_mapping(payload, "inventory refresh")
        print(f"Inventory refresh requested: {response.get('request_id', '')}")
    else:
        _print_agent_mutation(payload, command)
    return 0


def _agent_enrollment_token_command(
    client: AgentClient,
    args: argparse.Namespace,
) -> int:
    command = args.enrollment_token_command
    if command == "list":
        payload = client.get("/api/v1/agents/enrollment-tokens")
        _print_collection(payload, args.output, _enrollment_token_table)
        return 0
    if command == "create":
        payload = client.post(
            "/api/v1/agents/enrollment-tokens",
            {
                "name": args.name,
                "expires_at": args.expires_at,
                "expires_in_seconds": (
                    None if args.expires_at is not None else parse_duration(args.expires_in)
                ),
                "allowed_labels": _key_value_map(args.allowed_label, "allowed Agent label"),
            },
        )
        token = _require_mapping(payload, "enrollment token")
        if args.output == "json":
            _print_json(token)
        else:
            print("Enrollment token created (shown once).")
            print(f"Token ID: {token.get('id', '')}")
            print(f"Expires:  {token.get('expires_at', '')}")
            print(f"Token:    {token.get('token', '')}")
        return 0
    if command == "revoke":
        client.delete(
            f"/api/v1/agents/enrollment-tokens/{args.token_id}",
            {},
        )
        if args.output == "json":
            _print_json({"token_id": args.token_id, "revoked": True})
        else:
            print(f"Revoked enrollment token {args.token_id}.")
        return 0
    raise AssertionError("unreachable enrollment-token command")


def _bench_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.bench_command
    if command == "list":
        _bench_list(client, args)
    elif command == "show":
        payload = client.get(f"/api/v1/benches/{args.bench_id}")
        _print_read_payload(payload, args.output, _bench_show_table)
    elif command == "timeline":
        payload = client.get(
            f"/api/v1/benches/{args.bench_id}/timeline",
            {
                "category": args.category,
                "after": args.after,
                "before": args.before,
                "limit": args.limit,
            },
        )
        _print_collection(payload, args.output, _timeline_table)
    elif command == "reserve":
        payload = client.post(f"/api/v1/benches/{args.bench_id}/reservation", {"owner": args.owner})
        if args.output == "json":
            _print_json(payload)
        else:
            print(f"Reserved {args.bench_id} for {args.owner}.")
    elif command == "release":
        client.delete(f"/api/v1/benches/{args.bench_id}/reservation", {"owner": args.owner})
        if args.output == "json":
            _print_json({"bench_id": args.bench_id, "released": True})
        else:
            print(f"Released {args.bench_id}.")
    elif command in {"power-on", "power-off", "power-cycle"}:
        payload = client.post(
            f"/api/v1/benches/{args.bench_id}/actions/{command}",
            {"owner": args.owner},
        )
        _print_operation_created(payload, args.output)
    elif command == "reset":
        payload = client.post(
            f"/api/v1/benches/{args.bench_id}/actions/reset",
            {"owner": args.owner},
        )
        _print_operation_created(payload, args.output)
    elif command == "probe":
        payload = client.post(
            f"/api/v1/benches/{args.bench_id}/actions/probe",
            {"owner": args.owner},
        )
        probe = _require_mapping(payload, "probe")
        operation_id = _optional_text(probe.get("operation_id"))
        if operation_id is None:
            _print_read_payload(probe, args.output, _probe_table)
        else:
            operation = _wait_for_terminal(client, operation_id)
            if str(operation.get("status", "")).casefold() != "succeeded":
                print(
                    f"probe failed [{operation.get('error_code')}]: "
                    f"{operation.get('error_message')}",
                    file=sys.stderr,
                )
                return 7
            result = _require_mapping(operation.get("result"), "probe result")
            _print_read_payload(result, args.output, _probe_table)
    elif command == "serial" and args.serial_command == "read":
        payload = client.post(
            f"/api/v1/benches/{args.bench_id}/actions/read-serial",
            {
                "owner": args.owner,
                "timeout_seconds": args.timeout,
                "until_pattern": args.until_pattern,
                "max_lines": args.max_lines,
            },
        )
        accepted = _require_mapping(payload, "operation")
        operation_id = str(accepted.get("operation_id", ""))
        operation = _wait_for_terminal(client, operation_id)
        if str(operation.get("status", "")).casefold() != "succeeded":
            print(
                f"serial read failed [{operation.get('error_code')}]: "
                f"{operation.get('error_message')}",
                file=sys.stderr,
            )
            return 7
        artifact = _require_mapping(
            client.get(f"/api/v1/operations/{operation_id}/artifacts/serial"),
            "serial artifact",
        )
        text = str(artifact.get("text", ""))
        if args.output == "json":
            _print_json({"operation": operation, "serial_output": text})
        else:
            print(text, end="" if text.endswith("\n") else "\n")
    elif command == "flash":
        checksum, size = _validate_firmware(args.firmware_path)
        payload = client.upload(
            f"/api/v1/benches/{args.bench_id}/actions/flash",
            args.firmware_path,
            owner=args.owner,
            version=args.version,
        )
        if args.output == "json":
            body = _require_mapping(payload, "operation")
            _print_json(
                {
                    **body,
                    "filename": args.firmware_path.name,
                    "sha256": checksum,
                    "size_bytes": size,
                }
            )
        else:
            print(f"Firmware: {args.firmware_path.name}")
            print(f"SHA-256:  {checksum}")
            print(f"Size:     {size} bytes")
            _print_operation_created(payload, "table")
    return 0


def _reservation_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.reservation_command
    if command == "list":
        query: dict[str, object] = {
            "bench_id": args.bench_id,
            "owner": args.owner,
            "status": args.status,
            "starts_after": args.starts_after,
            "starts_before": args.starts_before,
            "limit": args.limit,
        }
        if args.state:
            query["state"] = [state.upper() for state in args.state]
        if args.agent_id:
            query["agent_id"] = args.agent_id
        payload = client.get(
            "/api/v1/reservations",
            query,
        )
        _print_collection(payload, args.output, _reservation_table)
        return 0
    if command == "show":
        payload = client.get(f"/api/v1/reservations/{args.reservation_id}")
        _print_read_payload(payload, args.output, _reservation_show_table)
        return 0
    if command == "create":
        if "/" in args.bench_id or args.lease_ttl is not None or args.metadata:
            if args.start is not None or args.queue_if_busy:
                raise ValueError(
                    "distributed reservations do not support --start or --queue-if-busy"
                )
            idempotency_key = args.idempotency_key or f"labctl:reservation:{uuid4()}"
            request: dict[str, object] = {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "reservation_duration_seconds": parse_duration(args.duration),
                "lease_ttl_seconds": (
                    parse_duration(args.lease_ttl) if args.lease_ttl is not None else None
                ),
                "idempotency_key": idempotency_key,
                "metadata": _key_value_map(args.metadata, "reservation metadata"),
            }
        else:
            request = {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "starts_at": args.start,
                "duration_seconds": parse_duration(args.duration),
                "queue_if_busy": args.queue_if_busy,
                "idempotency_key": args.idempotency_key,
            }
        payload = client.post("/api/v1/reservations", request)
        _print_reservation(payload, args.output)
        return 0
    if command == "renew":
        idempotency_key = args.idempotency_key or f"labctl:reservation-renew:{uuid4()}"
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/renew",
            {
                "owner": args.owner,
                "expected_lease_version": args.expected_lease_version,
                "idempotency_key": idempotency_key,
                "lease_ttl_seconds": (
                    parse_duration(args.lease_ttl) if args.lease_ttl is not None else None
                ),
            },
        )
        _print_reservation(payload, args.output, action="renewed")
        return 0
    if command == "extend":
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/extend",
            {"owner": args.owner, "duration_seconds": parse_duration(args.duration)},
        )
        _print_reservation(payload, args.output)
        return 0
    if command == "release":
        body: dict[str, object] = {"owner": args.owner}
        if args.expected_lease_version is not None:
            idempotency_key = args.idempotency_key or f"labctl:reservation-release:{uuid4()}"
            body.update(
                {
                    "expected_lease_version": args.expected_lease_version,
                    "idempotency_key": idempotency_key,
                }
            )
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/release",
            body,
        )
        _print_reservation(payload, args.output, action="released")
        return 0
    if command == "cancel":
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/cancel",
            {"owner": args.owner},
        )
        _print_reservation(payload, args.output, action="cancelled")
        return 0
    if command == "queue":
        payload = client.post(
            f"/api/v1/benches/{args.bench_id}/queue",
            {
                "owner": args.owner,
                "duration_seconds": parse_duration(args.duration),
                "idempotency_key": args.idempotency_key,
            },
        )
        if args.output == "json":
            _print_json(payload)
        else:
            entry = _require_mapping(payload, "queue entry")
            print("Added to queue.")
            print(f"Queue entry: {entry.get('id', '')}")
            print(f"Position:    {entry.get('position', '—')}")
        return 0
    if command == "queue-list":
        payload = client.get(f"/api/v1/benches/{args.bench_id}/queue")
        _print_collection(payload, args.output, _queue_table)
        return 0
    if command == "queue-cancel":
        client.delete(f"/api/v1/queue/{args.queue_entry_id}", {"owner": args.owner})
        if args.output == "json":
            _print_json({"queue_entry_id": args.queue_entry_id, "cancelled": True})
        else:
            print(f"Cancelled queue entry {args.queue_entry_id}.")
        return 0
    raise AssertionError("unreachable reservation command")


def _workflow_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.workflow_command
    if command == "list":
        _print_collection(client.get("/api/v1/workflows"), args.output, _workflow_table)
        return 0
    if command == "show":
        payload = client.get(f"/api/v1/workflows/{args.workflow_name}")
        _print_read_payload(payload, args.output, _workflow_show_table)
        return 0
    if command == "register":
        definition = yaml.safe_load(args.definition.read_text(encoding="utf-8"))
        if not isinstance(definition, dict):
            raise ValueError("workflow definition must be a YAML or JSON mapping")
        payload = client.post("/api/v1/workflows", cast(dict[str, object], definition))
        _print_read_payload(payload, args.output, _workflow_show_table)
        return 0
    if command == "run":
        inputs = _workflow_inputs(args.input)
        distributed = (
            args.bench_id is None
            or "/" in args.bench_id
            or args.version is not None
            or args.kind is not None
            or args.location is not None
            or bool(args.bench_label)
            or bool(args.agent_label)
            or args.reservation_duration is not None
            or args.lease_ttl is not None
            or args.command_timeout is not None
            or args.idempotency_key is not None
        )
        if distributed:
            if args.reservation_id is not None or args.release_after:
                raise ValueError(
                    "distributed workflow runs own their lease; --reservation-id and "
                    "--release-after are not supported"
                )
            idempotency_key = args.idempotency_key or f"labctl:workflow:{uuid4()}"
            request = {
                "version": args.version,
                "owner": args.owner,
                "idempotency_key": idempotency_key,
                "inputs": inputs,
                "bench_id": args.bench_id,
                "kind": args.kind.upper() if args.kind is not None else None,
                "location": args.location,
                "bench_labels": _key_value_map(args.bench_label, "bench label"),
                "agent_labels": _key_value_map(args.agent_label, "Agent label"),
                "reservation_duration_seconds": (
                    parse_duration(args.reservation_duration)
                    if args.reservation_duration is not None
                    else parse_duration(args.reserve)
                    if args.reserve is not None
                    else None
                ),
                "lease_ttl_seconds": (
                    parse_duration(args.lease_ttl) if args.lease_ttl is not None else None
                ),
                "command_timeout_seconds": (
                    parse_duration(args.command_timeout)
                    if args.command_timeout is not None
                    else 3600
                ),
            }
        else:
            request = {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "reservation_id": args.reservation_id,
                "reserve_duration_seconds": (
                    parse_duration(args.reserve) if args.reserve is not None else None
                ),
                "release_after": args.release_after,
                "inputs": inputs,
            }
        request_key = request.get("idempotency_key")
        if request_key is None:
            payload = client.post(
                f"/api/v1/workflows/{args.workflow_name}/runs",
                request,
            )
        else:
            payload = client.post(
                f"/api/v1/workflows/{args.workflow_name}/runs",
                request,
                idempotency_key=str(request_key),
            )
        if args.output == "json":
            _print_json(payload)
        else:
            run = _require_mapping(payload, "workflow run")
            operation = run.get("operation")
            operation_data = (
                cast(dict[str, object], operation) if isinstance(operation, dict) else {}
            )
            run_id = run.get("id") or operation_data.get("id")
            print(f"Workflow run created: {run_id or ''}")
            agent = run.get("agent")
            if isinstance(agent, dict):
                print(f"Agent: {agent.get('name') or agent.get('slug') or agent.get('id', '')}")
            bench = run.get("bench")
            if isinstance(bench, dict):
                print(f"Bench: {bench.get('id', '')}")
        return 0
    if command == "watch":
        return _watch_workflow(client, args)
    if command == "cancel":
        payload = client.post(
            f"/api/v1/workflow-runs/{args.workflow_run_id}/cancel",
            {"owner": args.owner},
        )
        if args.output == "json":
            _print_json(payload)
        else:
            print(f"Cancellation requested for workflow run {args.workflow_run_id}.")
        return 0
    if command == "results":
        if args.format == "junit":
            content: object = client.get_text(
                f"/api/v1/workflow-runs/{args.workflow_run_id}/results/junit"
            )
            if args.output is not None:
                _write_atomic(args.output, str(content).encode("utf-8"))
            else:
                print(content, end="" if str(content).endswith("\n") else "\n")
        else:
            content = client.get(f"/api/v1/workflow-runs/{args.workflow_run_id}/results")
            if args.output is not None:
                _write_atomic(
                    args.output,
                    (json.dumps(content, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
            else:
                _print_json(content)
        return 0
    raise AssertionError("unreachable workflow command")


def _token_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.token_command
    if command == "create":
        payload = client.post(
            "/api/v1/tokens",
            {
                "name": args.name,
                "owner": args.owner,
                "scopes": args.scope,
                "expires_at": args.expires_at,
            },
        )
        if args.output == "json":
            _print_json(payload)
        else:
            token = _require_mapping(payload, "API token")
            print(f"Token ID: {token.get('id', '')}")
            print(f"Owner:    {token.get('owner', '')}")
            print(f"Token:    {token.get('token', '')}")
            print("Store this token now; it will not be shown again.")
        return 0
    if command == "list":
        payload = client.get("/api/v1/tokens")
        if args.output == "json":
            _print_json(payload)
        else:
            items = _require_list(_require_mapping(payload, "tokens").get("items"), "tokens")
            rows: list[tuple[str, ...]] = []
            for item in items:
                token = _require_mapping(item, "token")
                rows.append(
                    (
                        str(token.get("id", "")),
                        str(token.get("name", "")),
                        str(token.get("owner", "")),
                        "Revoked" if token.get("revoked_at") else "Active",
                    )
                )
            _print_table(("ID", "NAME", "OWNER", "STATUS"), rows)
        return 0
    if command == "revoke":
        payload = client.post(f"/api/v1/tokens/{args.token_id}/revoke", {})
        if args.output == "json":
            _print_json(payload)
        else:
            print(f"Revoked API token {args.token_id}.")
        return 0
    raise AssertionError("unreachable token command")


def _ci_command(client: AgentClient, args: argparse.Namespace) -> int:
    if args.ci_command == "session":
        return _ci_session_command(client, args)
    if args.ci_command == "run":
        return _ci_run(client, args)
    if args.ci_command == "upload":
        try:
            checksum = args.sha256 or _validate_firmware(args.path)[0]
        except (OSError, ValueError) as exc:
            raise _CiArtifactUploadError(str(exc)) from exc
        payload = client.upload_artifact(
            "/api/v1/artifacts",
            args.path,
            name=args.name or args.path.name,
            artifact_type=args.artifact_type,
            ci_session_id=args.session_id,
            checksum=checksum,
            idempotency_key=args.idempotency_key,
        )
        if args.output == "json":
            _print_json(payload)
        else:
            artifact = _require_mapping(payload, "artifact")
            print(f"Artifact uploaded: {artifact.get('id', '')}")
        return 0
    if args.ci_command == "artifacts":
        payload = client.get(f"/api/v1/ci/sessions/{args.session_id}/artifacts")
        if args.output == "json":
            _print_json(payload)
        else:
            _ci_artifact_table(
                _require_list(_require_mapping(payload, "artifacts").get("items"), "artifacts")
            )
        return 0
    if args.ci_command == "download":
        _write_atomic(
            args.output,
            client.download(f"/api/v1/artifacts/{args.artifact_id}/content"),
        )
        print(f"Downloaded artifact to {args.output}.")
        return 0
    raise AssertionError("unreachable CI command")


def _ci_session_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.ci_session_command
    if command == "create":
        environment = detect_ci_environment()
        payload: dict[str, object] = dict(environment.as_payload())
        for key, value in {
            "external_run_id": args.external_run_id,
            "repository": args.repository,
            "ref": args.ref,
            "commit_sha": args.commit_sha,
            "actor": args.actor,
        }.items():
            if value is not None:
                payload[key] = value
        payload["bench_request"] = _ci_bench_request(args)
        response = client.post(
            "/api/v1/ci/sessions",
            payload,
            idempotency_key=args.idempotency_key or environment.idempotency_key,
        )
        _print_ci_session(response, args.output)
        return 0
    session_id = str(args.session_id)
    if command == "show":
        _print_ci_session(client.get(f"/api/v1/ci/sessions/{session_id}"), args.output)
        return 0
    if command == "watch":
        return _watch_ci_session(client, session_id, args)
    if command == "cancel":
        current = _require_mapping(client.get(f"/api/v1/ci/sessions/{session_id}"), "CI session")
        response = _cancel_and_finalize_ci_session(
            client,
            session_id,
            idempotency_key=args.idempotency_key,
            session=current,
        )
        if response is not None:
            _print_ci_session(response, args.output)
            return int(_ci_exit_for_session(response))
        return int(CiExitCode.WORKFLOW_CANCELLED)
    if command == "finalize":
        current = _require_mapping(client.get(f"/api/v1/ci/sessions/{session_id}"), "CI session")
        response = client.post(
            f"/api/v1/ci/sessions/{session_id}/finalize",
            {},
            idempotency_key=args.idempotency_key,
            timeout=_ci_cleanup_request_timeout(current),
        )
        _print_ci_session(response, args.output)
        return int(_ci_exit_for_session(_require_mapping(response, "CI session")))
    raise AssertionError("unreachable CI session command")


def _ci_run(client: AgentClient, args: argparse.Namespace) -> int:
    environment = detect_ci_environment()
    session_id: str | None = None
    session_configuration: dict[str, object] | None = None
    workflow_run_id: str | None = None
    finalized = False
    heartbeat: _CiHeartbeatWorker | None = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def stop_heartbeat() -> None:
        nonlocal heartbeat
        if heartbeat is not None:
            heartbeat.stop()
            heartbeat = None

    signal.signal(signal.SIGTERM, terminate)
    try:
        created = _require_mapping(
            client.post(
                "/api/v1/ci/sessions",
                {
                    **environment.as_payload(),
                    "bench_request": _ci_bench_request(args),
                },
                idempotency_key=environment.idempotency_key,
            ),
            "CI session",
        )
        session_id = str(created.get("id", ""))
        session_configuration = created
        if not session_id:
            raise ValueError("Agent did not return a CI session ID")
        if str(created.get("status", "")).casefold() != "completed":
            heartbeat = _CiHeartbeatWorker(
                client,
                session_id,
                interval_seconds=_ci_heartbeat_interval(created),
            )
            heartbeat.start()
        created_status = str(created.get("status", "")).casefold()
        distributed_ci = "distributed_workflow" in created
        if args.output == "table":
            verb = "resumed" if created_status in {"running", "completed"} else "created"
            print(f"CI session {verb}: {session_id}")
        session = created
        if not distributed_ci and created_status in {"created", "waiting_for_bench"}:
            if args.output == "table":
                print("Finding compatible bench...")
            session = _wait_for_ci_assignment(client, session_id, args)
            session_configuration = session
        session_status = str(session.get("status", "")).casefold()
        if session_status == "completed":
            stop_heartbeat()
            finalized = True
            return _finish_ci_run(
                client,
                session,
                workflow_run_id=_optional_text(session.get("workflow_run_id")),
                args=args,
            )

        terminal = session
        if session_status == "reserved" or (
            distributed_ci and session_status in {"created", "waiting_for_bench"}
        ):
            if args.output == "table" and session.get("bench_id"):
                print(f"Assigned: {session.get('bench_id', '')}")
            artifact_references: dict[str, dict[str, str]] = {}
            for specification in args.artifact:
                name, path_text = _key_value(specification, "artifact")
                path = Path(path_text)
                try:
                    checksum, _size = _validate_firmware(path)
                except (OSError, ValueError) as exc:
                    raise _CiArtifactUploadError(str(exc)) from exc
                artifact_type = "firmware" if name == "firmware" else "input"
                artifact_key = f"{environment.idempotency_key}:artifact:{name}"
                if distributed_ci:
                    artifact_response = client.upload_artifact(
                        "/api/v1/artifacts",
                        path,
                        fields={
                            "owner_type": "ci_session",
                            "owner_id": session_id,
                            "artifact_type": artifact_type,
                            "expected_sha256": checksum,
                            "idempotency_key": artifact_key,
                        },
                    )
                else:
                    artifact_response = client.upload_artifact(
                        "/api/v1/artifacts",
                        path,
                        name=path.name,
                        artifact_type=artifact_type,
                        ci_session_id=session_id,
                        checksum=checksum,
                        idempotency_key=artifact_key,
                    )
                artifact = _require_mapping(artifact_response, "artifact")
                artifact_id = str(artifact.get("id", ""))
                if not artifact_id:
                    raise ValueError("Agent did not return an artifact ID")
                artifact_references[name] = {"artifact_id": artifact_id}
                if args.output == "table":
                    print(f"Uploaded {path.name}.")

            workflow_inputs: dict[str, object] = dict(_workflow_inputs(args.input))
            workflow_inputs.update(artifact_references)
            run_payload = {"workflow_name": args.workflow, "inputs": workflow_inputs}
            workflow_key = f"{environment.idempotency_key}:workflow"
            running = (
                _start_distributed_ci_workflow(
                    client,
                    session_id,
                    run_payload,
                    idempotency_key=workflow_key,
                    args=args,
                )
                if distributed_ci
                else _require_mapping(
                    client.post(
                        f"/api/v1/ci/sessions/{session_id}/run",
                        run_payload,
                        idempotency_key=workflow_key,
                    ),
                    "CI workflow",
                )
            )
            if distributed_ci:
                running_status = str(running.get("status", "")).casefold()
                if running_status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
                    terminal = running
                else:
                    operation_id = _optional_text(running.get("operation_id"))
                    if operation_id is None:
                        raise ValueError("Control plane did not return a distributed operation ID")
                    if args.output == "table":
                        print(f"Assigned: {running.get('bench_id', '')}")
                        print(f"Remote operation: {operation_id}")
                    terminal = _watch_distributed_ci_workflow(client, session_id, args)
            else:
                workflow_run_id = _optional_text(running.get("workflow_run_id"))
                if workflow_run_id is None:
                    raise ValueError("Agent did not return a workflow run ID")
                terminal = _watch_ci_workflow(client, session_id, workflow_run_id, args)
            session_configuration = terminal
        elif session_status == "running":
            if distributed_ci:
                operation_id = _optional_text(session.get("operation_id"))
                if operation_id is None:
                    raise ValueError("Running distributed CI session has no operation ID")
                if args.output == "table":
                    print(f"Resuming remote operation: {operation_id}")
                terminal = _watch_distributed_ci_workflow(client, session_id, args)
            else:
                workflow_run_id = _optional_text(session.get("workflow_run_id"))
                if workflow_run_id is None:
                    raise ValueError("Running CI session did not include a workflow run ID")
                if args.output == "table":
                    print(f"Resuming workflow run: {workflow_run_id}")
                terminal = _watch_ci_workflow(client, session_id, workflow_run_id, args)
            session_configuration = terminal
        elif session_status not in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            raise ValueError(
                f"Agent returned unexpected CI session status: {session_status or 'missing'}"
            )

        if workflow_run_id is None:
            workflow_run_id = _optional_text(terminal.get("workflow_run_id"))
        terminal_status = str(terminal.get("status", "")).casefold()
        finalized_payload = terminal
        stop_heartbeat()
        if terminal_status != "completed":
            finalized_payload = (
                _finalize_distributed_ci_session(
                    client,
                    session_id,
                    session=terminal,
                    idempotency_key=f"{environment.idempotency_key}:finalize",
                    poll_interval=_ci_poll_interval(args),
                )
                if distributed_ci
                else _require_mapping(
                    client.post(
                        f"/api/v1/ci/sessions/{session_id}/finalize",
                        {},
                        idempotency_key=f"{environment.idempotency_key}:finalize",
                        timeout=_ci_cleanup_request_timeout(terminal),
                    ),
                    "finalized CI session",
                )
            )
        finalized = True
        return _finish_ci_run(
            client,
            finalized_payload,
            workflow_run_id=workflow_run_id,
            args=args,
        )
    except _CiBenchWaitTimeout as exc:
        stop_heartbeat()
        cancelled: dict[str, object] | None = None
        if session_id is not None:
            cancelled = _cancel_and_finalize_ci_session(
                client,
                session_id,
                session=session_configuration,
                best_effort=True,
            )
        print(f"error: {exc}", file=sys.stderr)
        if cancelled is not None and _ci_exit_for_session(cancelled) is CiExitCode.CLEANUP_FAILED:
            return int(CiExitCode.CLEANUP_FAILED)
        return int(CiExitCode.BENCH_WAIT_TIMEOUT)
    except KeyboardInterrupt:
        stop_heartbeat()
        cancelled = None
        if session_id is not None:
            cancelled = _cancel_and_finalize_ci_session(
                client,
                session_id,
                session=session_configuration,
                best_effort=True,
            )
        if cancelled is not None:
            return _finish_ci_run(
                client,
                cancelled,
                workflow_run_id=workflow_run_id or _optional_text(cancelled.get("workflow_run_id")),
                args=args,
            )
        return int(CiExitCode.WORKFLOW_CANCELLED)
    except Exception:
        stop_heartbeat()
        if session_id is not None and not finalized:
            _cancel_and_finalize_ci_session(
                client,
                session_id,
                session=session_configuration,
                best_effort=True,
            )
        raise
    finally:
        stop_heartbeat()
        signal.signal(signal.SIGTERM, previous_term)


def _watch_ci_session(
    client: AgentClient,
    session_id: str,
    args: argparse.Namespace,
) -> int:
    interval = _ci_poll_interval(args)
    previous: tuple[object, object] | None = None
    latest: dict[str, object] | None = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            latest = _require_mapping(client.get(f"/api/v1/ci/sessions/{session_id}"), "CI session")
            current = (latest.get("status"), latest.get("bench_id"))
            if current != previous and args.output == "table":
                print(
                    f"{str(latest.get('status', '')).replace('_', ' ').title()}"
                    + (f" — {latest.get('bench_id')}" if latest.get("bench_id") else "")
                )
            if str(latest.get("status", "")).casefold() == "completed":
                if args.output == "json":
                    _print_json(latest)
                return int(_ci_exit_for_session(latest))
            client.post(f"/api/v1/ci/sessions/{session_id}/heartbeat", {})
            previous = current
            time.sleep(interval)
    except KeyboardInterrupt:
        cancelled = _cancel_and_finalize_ci_session(
            client,
            session_id,
            session=latest,
            best_effort=True,
        )
        if cancelled is not None:
            _print_ci_session(cancelled, args.output)
            return int(_ci_exit_for_session(cancelled))
        return int(CiExitCode.WORKFLOW_CANCELLED)
    finally:
        signal.signal(signal.SIGTERM, previous_term)


class _CiHeartbeatWorker:
    """Maintain a session heartbeat while the synchronous CLI performs I/O."""

    def __init__(
        self,
        client: AgentClient,
        session_id: str,
        *,
        interval_seconds: float,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._interval_seconds = interval_seconds
        self._request_timeout_seconds = min(5.0, max(1.0, interval_seconds / 2))
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"labctl-ci-heartbeat-{session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=self._request_timeout_seconds + 0.5)

    def _run(self) -> None:
        while not self._stopped.wait(self._interval_seconds):
            try:
                self._client.post(
                    f"/api/v1/ci/sessions/{self._session_id}/heartbeat",
                    {},
                    timeout=self._request_timeout_seconds,
                )
            except (AgentApiError, AgentConnectionError, ValueError):
                # The foreground request reports actionable transport/API errors.
                # Heartbeats continue so a transient failure can recover.
                continue


def _finish_ci_run(
    client: AgentClient,
    finalized_session: dict[str, object],
    *,
    workflow_run_id: str | None,
    args: argparse.Namespace,
) -> int:
    exit_code = _ci_exit_for_session(
        finalized_session,
        workflow_run_id=workflow_run_id,
        client=client,
    )
    best_effort_diagnostics = exit_code is not CiExitCode.SUCCESS
    _download_ci_outputs(
        client,
        finalized_session,
        workflow_run_id=workflow_run_id,
        junit_output=args.junit_output,
        artifacts_directory=args.artifacts_directory,
        output=args.output,
        best_effort=best_effort_diagnostics,
    )
    try:
        _publish_ci_integration_outputs(client, finalized_session, workflow_run_id, args)
    except (AgentApiError, AgentConnectionError, OSError, ValueError) as exc:
        if not best_effort_diagnostics:
            raise
        print(
            f"warning: diagnostics publication incomplete: {exc}",
            file=sys.stderr,
        )
    if args.output == "json":
        _print_json(finalized_session)
    else:
        print(
            "Cleanup complete."
            if finalized_session.get("cleanup_status") == "succeeded"
            else "Cleanup failed."
        )
        outcome = str(finalized_session.get("outcome", ""))
        print(f"Hardware CI: {outcome.replace('_', ' ').title()}")
    return int(exit_code)


def _ci_bench_request(args: argparse.Namespace) -> dict[str, object]:
    capabilities: set[str] = set()
    for specification in args.require:
        kind, value = _key_value(specification, "requirement")
        if kind != "capability":
            raise ValueError("CI requirements must use capability=<name>")
        capabilities.add(value.casefold())

    required_labels = _key_value_map(args.label, "label")
    preferred_labels = _key_value_map(args.prefer_label, "preferred label")
    if not args.allow_simulated and not args.allow_physical:
        raise ValueError("at least one of simulated or physical benches must be allowed")
    request: dict[str, object] = {
        "explicit_bench_id": args.bench,
        "required_capabilities": sorted(capabilities),
        "required_labels": required_labels,
        "preferred_labels": preferred_labels,
        "allow_simulated": bool(args.allow_simulated),
        "allow_physical": bool(args.allow_physical),
        "maximum_wait_seconds": parse_duration(str(args.wait_timeout)),
        "reservation_duration_seconds": parse_duration(str(args.reservation_duration)),
    }
    agent_labels = _key_value_map(args.agent_label, "Agent label")
    if agent_labels:
        request["required_agent_labels"] = agent_labels
    if args.preferred_location is not None:
        request["preferred_location"] = args.preferred_location
    return request


def _wait_for_ci_assignment(
    client: AgentClient,
    session_id: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    deadline = time.monotonic() + parse_duration(str(args.wait_timeout))
    interval = _ci_poll_interval(args)
    previous_status: str | None = None
    while True:
        session = _require_mapping(
            client.get(f"/api/v1/ci/sessions/{session_id}"),
            "CI session",
        )
        status = str(session.get("status", "")).casefold()
        if status == "reserved":
            return session
        if status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            if status == "completed":
                return session
            return _require_mapping(
                client.post(
                    f"/api/v1/ci/sessions/{session_id}/finalize",
                    {},
                    idempotency_key=f"ci-session:{session_id}:client-finalize",
                    timeout=_ci_cleanup_request_timeout(session),
                ),
                "finalized CI session",
            )
        if status not in {"created", "waiting_for_bench"}:
            raise ValueError(f"Agent returned unexpected CI session status: {status or 'missing'}")
        if args.output == "table" and status != previous_status:
            print(status.replace("_", " ").title())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _CiBenchWaitTimeout("Timed out waiting for a compatible bench")
        client.post(f"/api/v1/ci/sessions/{session_id}/heartbeat", {})
        previous_status = status
        time.sleep(min(interval, remaining))


def _start_distributed_ci_workflow(
    client: AgentClient,
    session_id: str,
    payload: dict[str, object],
    *,
    idempotency_key: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    """Retry central selection without requiring the CLI to know an Agent route."""

    deadline = time.monotonic() + parse_duration(str(args.wait_timeout))
    interval = _ci_poll_interval(args)
    announced_wait = False
    while True:
        try:
            return _require_mapping(
                client.post(
                    f"/api/v1/ci/sessions/{session_id}/run",
                    payload,
                    idempotency_key=idempotency_key,
                ),
                "distributed CI workflow",
            )
        except AgentApiError as exc:
            if exc.code not in _DISTRIBUTED_CI_RETRYABLE_ERRORS:
                raise
            current = _require_mapping(
                client.get(f"/api/v1/ci/sessions/{session_id}"),
                "CI session",
            )
            status = str(current.get("status", "")).casefold()
            if status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CiBenchWaitTimeout(
                    "Timed out waiting for a compatible distributed bench"
                ) from exc
            if args.output == "table" and not announced_wait:
                print("Waiting for an online compatible Agent and bench...")
                announced_wait = True
            client.post(f"/api/v1/ci/sessions/{session_id}/heartbeat", {})
            time.sleep(min(interval, remaining))


def _watch_distributed_ci_workflow(
    client: AgentClient,
    session_id: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    interval = _ci_poll_interval(args)
    previous: tuple[object, object, object] | None = None
    while True:
        session = _require_mapping(
            client.get(f"/api/v1/ci/sessions/{session_id}"),
            "CI session",
        )
        status = str(session.get("status", "")).casefold()
        if not status:
            raise ValueError("Control plane returned a CI session without a status")
        operation_id = _optional_text(session.get("operation_id"))
        progress: object = None
        message: object = None
        if operation_id is not None:
            operation = _require_mapping(
                client.get(f"/api/v1/operations/{operation_id}"),
                "distributed operation",
            )
            progress = operation.get("progress")
            message = operation.get("message")
        current = (status, progress, message)
        if args.output == "table" and current != previous:
            detail = f" — {message}" if message else ""
            percent = f" ({progress}%)" if isinstance(progress, int) else ""
            print(f"Remote workflow: {status.replace('_', ' ').title()}{percent}{detail}")
        if status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            return session
        if status not in {"created", "waiting_for_bench", "reserved", "running"}:
            raise ValueError(f"Control plane returned unexpected CI status: {status}")
        client.post(f"/api/v1/ci/sessions/{session_id}/heartbeat", {})
        previous = current
        time.sleep(interval)


def _finalize_distributed_ci_session(
    client: AgentClient,
    session_id: str,
    *,
    session: dict[str, object],
    idempotency_key: str,
    poll_interval: float,
) -> dict[str, object]:
    """Wait until control-plane cleanup has synchronized every remote artifact."""

    timeout = _ci_cleanup_request_timeout(session)
    deadline = time.monotonic() + timeout
    while True:
        current = _require_mapping(
            client.post(
                f"/api/v1/ci/sessions/{session_id}/finalize",
                {},
                idempotency_key=idempotency_key,
                timeout=timeout,
            ),
            "finalized CI session",
        )
        status = str(current.get("status", "")).casefold()
        if status == "completed":
            return current
        if status != "cleanup_pending":
            raise ValueError(f"Control plane returned unexpected cleanup status: {status}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentConnectionError("Timed out waiting for remote CI artifact finalization")
        time.sleep(min(max(0.01, poll_interval), remaining))


def _watch_ci_workflow(
    client: AgentClient,
    session_id: str,
    workflow_run_id: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    previous_steps: dict[int, str] = {}
    previous_run_status: str | None = None
    interval = _ci_poll_interval(args)
    while True:
        run = _require_mapping(
            client.get(f"/api/v1/workflow-runs/{workflow_run_id}"),
            "workflow run",
        )
        run_status = str(run.get("status", "")).casefold()
        if not run_status:
            raise ValueError("Agent returned a workflow run without a status")
        steps = _require_list(run.get("steps", []), "workflow steps")
        if args.output == "table":
            _print_ci_progress(steps, previous_steps)
            if not steps and run_status != previous_run_status:
                print(f"Workflow: {run_status.replace('_', ' ').title()}")

        session = _require_mapping(
            client.get(f"/api/v1/ci/sessions/{session_id}"),
            "CI session",
        )
        session_status = str(session.get("status", "")).casefold()
        if run_status in _CI_WORKFLOW_TERMINAL_STATUSES:
            return session
        if session_status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            return session
        if run_status not in {"pending", "running", "cancel_requested"}:
            raise ValueError(f"Agent returned unexpected workflow status: {run_status}")
        client.post(f"/api/v1/ci/sessions/{session_id}/heartbeat", {})
        previous_run_status = run_status
        time.sleep(interval)


def _print_ci_progress(steps: list[object], previous: dict[int, str]) -> None:
    total = len(steps)
    for fallback_index, item in enumerate(steps):
        step = _require_mapping(item, "workflow step")
        raw_index = step.get("step_index")
        index = raw_index if isinstance(raw_index, int) else fallback_index
        status = str(step.get("status", "")).casefold()
        if not status or previous.get(index) == status:
            continue
        if status != "pending":
            name = str(step.get("name") or step.get("action") or f"Step {index + 1}")
            print(f"[{index + 1}/{total}] {name} — {status.replace('_', ' ').title()}")
        previous[index] = status


def _cancel_and_finalize_ci_session(
    client: AgentClient,
    session_id: str,
    *,
    idempotency_key: str | None = None,
    wait_seconds: float = 2.0,
    poll_interval: float = 0.2,
    session: dict[str, object] | None = None,
    best_effort: bool = False,
) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    failure: Exception | None = None
    cleanup_timeout = _ci_cleanup_request_timeout(session)
    try:
        latest = _require_mapping(
            client.post(
                f"/api/v1/ci/sessions/{session_id}/cancel",
                {},
                timeout=cleanup_timeout,
            ),
            "cancelled CI session",
        )
    except (AgentApiError, AgentConnectionError, ValueError) as exc:
        failure = exc

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while latest is not None and time.monotonic() < deadline:
        status = str(latest.get("status", "")).casefold()
        if status in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            break
        try:
            latest = _require_mapping(
                client.get(f"/api/v1/ci/sessions/{session_id}"),
                "CI session",
            )
        except (AgentApiError, AgentConnectionError, ValueError):
            break
        if str(latest.get("status", "")).casefold() in _CI_ASSIGNMENT_TERMINAL_STATUSES:
            break
        time.sleep(min(max(0.01, poll_interval), max(0.0, deadline - time.monotonic())))

    try:
        latest = _require_mapping(
            client.post(
                f"/api/v1/ci/sessions/{session_id}/finalize",
                {},
                idempotency_key=idempotency_key
                or f"ci-session:{session_id}:client-cancel-finalize",
                timeout=cleanup_timeout,
            ),
            "finalized CI session",
        )
    except (AgentApiError, AgentConnectionError, ValueError) as exc:
        if failure is None:
            failure = exc

    if failure is not None and not best_effort:
        raise failure
    return latest


def _download_ci_outputs(
    client: AgentClient,
    session: dict[str, object],
    *,
    workflow_run_id: str | None,
    junit_output: Path | None,
    artifacts_directory: Path | None,
    output: str,
    best_effort: bool = False,
) -> list[str]:
    downloaded: list[str] = []
    if junit_output is not None and workflow_run_id is not None:
        try:
            junit = client.get_text(f"/api/v1/workflow-runs/{workflow_run_id}/results/junit")
            _write_atomic(junit_output, junit.encode("utf-8"))
        except (AgentApiError, AgentConnectionError, OSError, ValueError) as exc:
            if not best_effort:
                raise
            _warn_incomplete_diagnostics("JUnit XML", exc)

    if artifacts_directory is None and (junit_output is None or workflow_run_id is not None):
        return downloaded
    session_id = str(session.get("id", ""))
    if not session_id:
        error = ValueError("Agent did not return a CI session ID for artifact download")
        if not best_effort:
            raise error
        _warn_incomplete_diagnostics("artifact listing", error)
        return downloaded
    try:
        payload = _require_mapping(
            client.get(f"/api/v1/ci/sessions/{session_id}/artifacts"),
            "CI artifacts",
        )
        artifacts = _require_list(payload.get("items"), "CI artifacts")
    except (AgentApiError, AgentConnectionError, OSError, ValueError) as exc:
        if not best_effort:
            raise
        _warn_incomplete_diagnostics("artifact listing", exc)
        return downloaded
    if junit_output is not None and workflow_run_id is None:
        try:
            junit_artifact = next(
                (
                    _require_mapping(item, "artifact")
                    for item in artifacts
                    if _require_mapping(item, "artifact").get("artifact_type") == "junit"
                ),
                None,
            )
            if junit_artifact is None:
                raise ValueError("Finalized CI session did not provide a JUnit artifact")
            junit_artifact_id = str(junit_artifact.get("id", ""))
            if not junit_artifact_id:
                raise ValueError("Agent returned a JUnit artifact without an ID")
            _write_atomic(
                junit_output,
                client.download(f"/api/v1/artifacts/{junit_artifact_id}/content"),
            )
        except (AgentApiError, AgentConnectionError, OSError, ValueError) as exc:
            if not best_effort:
                raise
            _warn_incomplete_diagnostics("JUnit artifact", exc)

    if artifacts_directory is None:
        return downloaded
    try:
        artifacts_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if not best_effort:
            raise
        _warn_incomplete_diagnostics("artifact directory", exc)
        return downloaded
    seen_names: set[str] = set()
    if output == "table" and artifacts:
        print("Downloading artifacts:")
    for item in artifacts:
        try:
            artifact = _require_mapping(item, "artifact")
            artifact_id = str(artifact.get("id", ""))
            if not artifact_id:
                raise ValueError("Agent returned an artifact without an ID")
            name = _safe_artifact_filename(str(artifact.get("name", "")))
            if name in seen_names:
                path = Path(name)
                suffix = f"-{artifact_id[:8]}"
                name = f"{path.stem}{suffix}{path.suffix}"
                counter = 2
                while name in seen_names:
                    name = f"{path.stem}{suffix}-{counter}{path.suffix}"
                    counter += 1
            seen_names.add(name)
            _write_atomic(
                artifacts_directory / name,
                client.download(f"/api/v1/artifacts/{artifact_id}/content"),
            )
            downloaded.append(name)
            if output == "table":
                print(f"✓ {name}")
        except (AgentApiError, AgentConnectionError, OSError, ValueError) as exc:
            if not best_effort:
                raise
            _warn_incomplete_diagnostics("artifact download", exc)
    return downloaded


def _warn_incomplete_diagnostics(kind: str, error: Exception) -> None:
    print(
        f"warning: diagnostics download incomplete ({kind}): {error}",
        file=sys.stderr,
    )


def _publish_ci_integration_outputs(
    client: AgentClient,
    session: dict[str, object],
    workflow_run_id: str | None,
    args: argparse.Namespace,
) -> None:
    steps: list[CiSummaryStep] = []
    artifact_names: list[str] = []
    if workflow_run_id is not None:
        try:
            workflow = _require_mapping(
                client.get(f"/api/v1/workflow-runs/{workflow_run_id}"),
                "workflow run",
            )
            for item in _require_list(workflow.get("steps", []), "workflow steps"):
                step = _require_mapping(item, "workflow step")
                steps.append(
                    CiSummaryStep(
                        name=str(step.get("name") or step.get("action") or "Workflow step"),
                        status=str(step.get("status", "unknown")),
                    )
                )
        except (AgentApiError, AgentConnectionError, ValueError):
            pass

    session_id = str(session.get("id", ""))
    if session_id:
        try:
            artifacts = _require_mapping(
                client.get(f"/api/v1/ci/sessions/{session_id}/artifacts"),
                "CI artifacts",
            )
            artifact_names = [
                str(_require_mapping(item, "artifact").get("name", ""))
                for item in _require_list(artifacts.get("items"), "CI artifacts")
            ]
        except (AgentApiError, AgentConnectionError, ValueError):
            pass

    inputs = _workflow_inputs(args.input)
    outcome = str(session.get("outcome") or session.get("status") or "unknown")
    append_github_summary(
        HardwareCiSummary(
            status=outcome.replace("_", " ").title(),
            bench_id=_optional_text(session.get("bench_id")),
            backend=_optional_text(session.get("backend")),
            firmware=inputs.get("expected_version"),
            duration_seconds=_ci_duration_seconds(session),
            steps=tuple(steps),
            artifacts=tuple(name for name in artifact_names if name),
            cleanup_status=_optional_text(session.get("cleanup_status")),
        )
    )
    _append_github_outputs(
        {
            "session-id": session_id,
            "bench-id": str(session.get("bench_id") or ""),
            "workflow-run-id": workflow_run_id or "",
            "result": outcome,
            "artifact-directory": (
                str(args.artifacts_directory) if args.artifacts_directory is not None else ""
            ),
        }
    )


def _ci_exit_for_session(
    session: dict[str, object],
    *,
    workflow_run_id: str | None = None,
    client: AgentClient | None = None,
) -> CiExitCode:
    status = str(session.get("status", "")).casefold()
    cleanup_status = str(session.get("cleanup_status", "")).casefold()
    if cleanup_status == "failed" or (status == "completed" and cleanup_status != "succeeded"):
        return CiExitCode.CLEANUP_FAILED

    error_code = _ci_session_error_code(session)
    if error_code is not None:
        mapped = exit_code_for_error(error_code)
        if mapped is not CiExitCode.CLIENT_OR_PROTOCOL_ERROR:
            return mapped

    outcome = str(session.get("outcome") or "").casefold()
    if not outcome or outcome == "pending":
        outcome = status
    if outcome == "infrastructure_error":
        return CiExitCode.BACKEND_UNAVAILABLE
    if outcome == "failed" and client is not None and workflow_run_id:
        workflow_code = _workflow_failure_exit_code(client, workflow_run_id)
        if workflow_code is not None:
            return workflow_code
    if outcome in {
        "succeeded",
        "success",
        "passed",
        "failed",
        "cancel_requested",
        "cancelled",
        "canceled",
        "timed_out",
        "timeout",
        "abandoned",
    }:
        return exit_code_for_status(
            outcome,
            cleanup_succeeded=True,
            error_code=error_code,
        )
    return CiExitCode.CLIENT_OR_PROTOCOL_ERROR


def _workflow_failure_exit_code(
    client: AgentClient,
    workflow_run_id: str,
) -> CiExitCode | None:
    try:
        workflow = _require_mapping(
            client.get(f"/api/v1/workflow-runs/{workflow_run_id}"),
            "workflow run",
        )
        error_code = _optional_text(workflow.get("error_code"))
        if error_code is not None:
            mapped = exit_code_for_error(error_code)
            if mapped is not CiExitCode.CLIENT_OR_PROTOCOL_ERROR:
                return mapped
        payload = _require_mapping(
            client.get(f"/api/v1/workflow-runs/{workflow_run_id}/results"),
            "workflow results",
        )
        results = _require_list(payload.get("results"), "workflow results")
    except (AgentApiError, AgentConnectionError, ValueError):
        return None
    statuses = {
        str(_require_mapping(item, "test result").get("status", "")).casefold() for item in results
    }
    if "failed" in statuses:
        return CiExitCode.HARDWARE_TEST_FAILED
    if "error" in statuses:
        return CiExitCode.WORKFLOW_FAILED
    return None


def _ci_session_error_code(session: dict[str, object]) -> str | None:
    explicit = _optional_text(session.get("error_code"))
    if explicit is not None:
        return explicit
    errors = session.get("errors")
    if not isinstance(errors, list):
        return None
    for item in errors:
        if not isinstance(item, str):
            continue
        candidate = item.partition(":")[0].strip()
        if candidate and candidate.upper() == candidate and " " not in candidate:
            return candidate
    return None


def _print_ci_session(payload: object, output: str) -> None:
    session = _require_mapping(payload, "CI session")
    if output == "json":
        _print_json(session)
        return
    _print_table(
        ("FIELD", "VALUE"),
        [
            ("Session", str(session.get("id", ""))),
            ("Status", str(session.get("status", "")).replace("_", " ").title()),
            ("Outcome", str(session.get("outcome", "")).replace("_", " ").title()),
            ("Bench", str(session.get("bench_id") or "—")),
            ("Workflow run", str(session.get("workflow_run_id") or "—")),
            (
                "Cleanup",
                str(session.get("cleanup_status", "")).replace("_", " ").title(),
            ),
        ],
    )


def _ci_artifact_table(items: list[object]) -> None:
    rows: list[tuple[str, ...]] = []
    for item in items:
        artifact = _require_mapping(item, "artifact")
        rows.append(
            (
                str(artifact.get("id", "")),
                str(artifact.get("name", "")),
                str(artifact.get("artifact_type", "")),
                str(artifact.get("size_bytes", "")),
                str(artifact.get("sha256", "")),
            )
        )
    _print_table(("ID", "NAME", "TYPE", "SIZE", "SHA-256"), rows)


def _key_value(value: str, kind: str) -> tuple[str, str]:
    key, separator, item_value = value.partition("=")
    key = key.strip()
    item_value = item_value.strip()
    if not separator or not key or not item_value:
        raise ValueError(f"{kind} must use key=value: {value}")
    return key, item_value


def _key_value_map(values: list[str], kind: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, item_value = _key_value(value, kind)
        result[key] = item_value
    return result


def _safe_artifact_filename(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"Agent returned unsafe artifact name: {value!r}")
    return value


def _write_atomic(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _append_github_outputs(values: dict[str, str]) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination is None or not destination.strip():
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" not in value and "\r" not in value:
                stream.write(f"{key}={value}\n")
                continue
            delimiter = f"lab_platform_{uuid4().hex}"
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def _ci_duration_seconds(session: dict[str, object]) -> float | None:
    started = _parse_ci_datetime(session.get("started_at") or session.get("created_at"))
    completed = _parse_ci_datetime(session.get("completed_at"))
    if started is None or completed is None:
        return None
    return max(0.0, (completed - started).total_seconds())


def _parse_ci_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ci_poll_interval(args: argparse.Namespace) -> float:
    interval = float(args.interval)
    if interval <= 0:
        raise ValueError("CI polling interval must be greater than zero")
    return interval


def _ci_heartbeat_interval(session: dict[str, object]) -> float:
    return _ci_configured_seconds(session, "heartbeat_interval_seconds", default=30.0)


def _ci_cleanup_request_timeout(session: dict[str, object] | None) -> float:
    return _ci_configured_seconds(session, "cleanup_timeout_seconds", default=60.0) + 10.0


def _ci_configured_seconds(
    session: dict[str, object] | None,
    name: str,
    *,
    default: float,
) -> float:
    value = None if session is None else session.get(name)
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return default


def _watch_workflow(client: AgentClient, args: argparse.Namespace) -> int:
    last: tuple[object, object] | None = None
    while True:
        payload = _require_mapping(
            client.get(f"/api/v1/workflow-runs/{args.workflow_run_id}"), "workflow run"
        )
        current = (payload.get("current_step"), payload.get("status"))
        if args.output == "table" and current != last:
            step = payload.get("current_step")
            prefix = f"Step {int(step) + 1}: " if isinstance(step, int) else ""
            print(f"{prefix}{str(payload.get('status', '')).replace('_', ' ').title()}")
        status = str(payload.get("status", "")).casefold()
        if status in {"succeeded", "failed", "cancelled"}:
            if args.output == "json":
                _print_json(payload)
            else:
                print(f"\nWorkflow status: {status.replace('_', ' ').title()}")
            return 0 if status == "succeeded" else 7
        last = current
        time.sleep(args.interval)


def _operation_command(client: AgentClient, args: argparse.Namespace) -> int:
    command = args.operation_command
    if command == "show":
        payload = client.get(f"/api/v1/operations/{args.operation_id}")
        _print_read_payload(payload, args.output, _operation_show_table)
        return 0
    if command == "list":
        payload = client.get(
            "/api/v1/operations",
            {
                "bench_id": args.bench_id,
                "status": args.status,
                "type": args.operation_type,
                "limit": args.limit,
            },
        )
        _print_collection(payload, args.output, _operation_table)
        return 0
    if command == "reconcile":
        payload = client.post(f"/api/v1/operations/{args.operation_id}/reconcile", {})
        if args.output == "json":
            _print_json(payload)
        else:
            response = _require_mapping(payload, "operation reconciliation")
            print(f"Reconciliation requested: {response.get('request_id', '')}")
        return 0
    if command == "cancel":
        payload = client.post(
            f"/api/v1/operations/{args.operation_id}/cancel", {"owner": args.owner}
        )
        if args.output == "json":
            _print_json(payload)
        else:
            print(f"Cancellation requested for {args.operation_id}.")
        return 0
    if command == "watch":
        return _watch_operation(client, args)
    raise AssertionError("unreachable operation command")


def _watch_operation(client: AgentClient, args: argparse.Namespace) -> int:
    last: tuple[object, object, object] | None = None
    while True:
        payload = _require_mapping(
            client.get(f"/api/v1/operations/{args.operation_id}"), "operation"
        )
        current = (payload.get("progress"), payload.get("message"), payload.get("status"))
        if args.output == "table" and current != last:
            progress = _integer(payload.get("progress"))
            print(f"[{progress:3d}%] {payload.get('message') or payload.get('status', '')}")
        status = str(payload.get("status", "")).casefold()
        if status in {"succeeded", "failed", "cancelled"}:
            if args.output == "json":
                _print_json(payload)
            else:
                print(f"\nStatus: {status.replace('_', ' ').title()}")
            return 0 if status == "succeeded" else 7
        last = current
        time.sleep(args.interval)


def _wait_for_terminal(
    client: AgentClient, operation_id: str, interval: float = 0.05
) -> dict[str, object]:
    while True:
        operation = _require_mapping(client.get(f"/api/v1/operations/{operation_id}"), "operation")
        if str(operation.get("status", "")).casefold() in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return operation
        time.sleep(interval)


def _bench_list(client: AgentClient, args: argparse.Namespace) -> None:
    query: dict[str, object] = {
        "status": getattr(args, "status", None),
        "capability": getattr(args, "capability", None),
        "reserved": getattr(args, "reserved", None),
        "online": True if getattr(args, "online", False) else None,
        "available": True if getattr(args, "available", False) else None,
        "label": getattr(args, "label", None),
    }
    for key, value in (
        ("agent_id", getattr(args, "agent_id", None)),
        ("location", getattr(args, "location", None)),
        ("agent_label", getattr(args, "agent_label", None)),
    ):
        if value is not None and value != () and value != []:
            query[key] = value
    payload = client.get(
        "/api/v1/benches",
        query,
    )
    _print_collection(payload, args.output, _bench_table)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labctl")
    parser.add_argument("--server", "--url", dest="server", default=None)
    parser.add_argument("--config", type=Path, default=Path("~/.config/lab-platform/cli.yaml"))
    parser.add_argument(
        "--token-env",
        default="LAB_PLATFORM_TOKEN",
        help="Environment variable containing the API token (default: %(default)s).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    _read_parser(commands.add_parser("version"))
    _read_parser(commands.add_parser("health"))
    _read_parser(commands.add_parser("benches", help=argparse.SUPPRESS))
    _read_parser(commands.add_parser("plugins", help=argparse.SUPPRESS))

    agent = commands.add_parser("agent", help="Administer distributed lab Agents.")
    agents = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = _read_parser(agents.add_parser("list"))
    agent_list.add_argument("--status")
    agent_list.add_argument("--location")
    agent_list.add_argument("--label", action="append", default=[])
    agent_list.add_argument("--version")
    agent_show = _read_parser(agents.add_parser("show"))
    agent_show.add_argument("agent_id")
    enrollment_token = agents.add_parser("enrollment-token")
    enrollment_tokens = enrollment_token.add_subparsers(
        dest="enrollment_token_command", required=True
    )
    enrollment_create = _read_parser(enrollment_tokens.add_parser("create"))
    enrollment_create.add_argument("--name", required=True)
    enrollment_create.add_argument("--expires-in", default="30m")
    enrollment_create.add_argument("--expires-at")
    enrollment_create.add_argument("--allowed-label", action="append", default=[])
    _read_parser(enrollment_tokens.add_parser("list"))
    enrollment_revoke = _read_parser(enrollment_tokens.add_parser("revoke"))
    enrollment_revoke.add_argument("token_id")
    agent_drain = _read_parser(agents.add_parser("drain"))
    agent_drain.add_argument("agent_id")
    agent_drain.add_argument("--cancel-queued-work", action="store_true")
    for name in ("undrain", "revoke", "refresh"):
        mutation = _read_parser(agents.add_parser(name))
        mutation.add_argument("agent_id")
    agent_timeline = _read_parser(agents.add_parser("timeline"))
    agent_timeline.add_argument("agent_id")
    agent_timeline.add_argument("--severity")
    agent_timeline.add_argument("--event-type")
    agent_timeline.add_argument("--since")
    agent_timeline.add_argument("--limit", type=int, default=500)

    bench = commands.add_parser("bench", help="Inspect and control benches.")
    benches = bench.add_subparsers(dest="bench_command", required=True)
    bench_list = _read_parser(benches.add_parser("list"))
    bench_list.add_argument("--status")
    bench_list.add_argument("--capability")
    bench_list.add_argument("--reserved", action=argparse.BooleanOptionalAction, default=None)
    bench_list.add_argument("--online", action="store_true")
    bench_list.add_argument("--available", action="store_true")
    bench_list.add_argument("--label", action="append", default=[])
    bench_list.add_argument("--agent", dest="agent_id")
    bench_list.add_argument("--location")
    bench_list.add_argument("--agent-label", action="append", default=[])
    bench_show = _read_parser(benches.add_parser("show"))
    bench_show.add_argument("bench_id")
    timeline = _read_parser(benches.add_parser("timeline"))
    timeline.add_argument("bench_id")
    timeline.add_argument("--category")
    timeline.add_argument("--after")
    timeline.add_argument("--before")
    timeline.add_argument("--limit", type=int, default=50)
    for name in ("reserve", "release", "power-on", "power-off", "power-cycle", "reset"):
        action = _read_parser(benches.add_parser(name))
        action.add_argument("bench_id")
        action.add_argument("--owner", required=True)
    flash = _read_parser(benches.add_parser("flash"))
    flash.add_argument("bench_id")
    flash.add_argument("firmware_path", type=Path)
    flash.add_argument("--owner", required=True)
    flash.add_argument("--version")
    probe = _read_parser(benches.add_parser("probe"))
    probe.add_argument("bench_id")
    probe.add_argument("--owner", required=True)
    serial = benches.add_parser("serial")
    serial_commands = serial.add_subparsers(dest="serial_command", required=True)
    serial_read = _read_parser(serial_commands.add_parser("read"))
    serial_read.add_argument("bench_id")
    serial_read.add_argument("--owner", required=True)
    serial_read.add_argument("--timeout", type=float, default=10)
    serial_read.add_argument("--until", dest="until_pattern")
    serial_read.add_argument("--max-lines", type=int, default=500)

    reservation = commands.add_parser("reservation", help="Reserve and queue benches.")
    reservations = reservation.add_subparsers(dest="reservation_command", required=True)
    reservation_list = _read_parser(reservations.add_parser("list"))
    reservation_list.add_argument("--bench-id")
    reservation_list.add_argument("--owner")
    reservation_list.add_argument("--status")
    reservation_list.add_argument("--state", action="append", default=[])
    reservation_list.add_argument("--agent", dest="agent_id")
    reservation_list.add_argument("--starts-after")
    reservation_list.add_argument("--starts-before")
    reservation_list.add_argument("--limit", type=int, default=50)
    reservation_show = _read_parser(reservations.add_parser("show"))
    reservation_show.add_argument("reservation_id")
    reservation_create = _read_parser(reservations.add_parser("create"))
    reservation_create.add_argument("bench_id")
    reservation_create.add_argument("--owner", required=True)
    reservation_create.add_argument("--duration", required=True)
    reservation_create.add_argument("--start")
    reservation_create.add_argument("--queue-if-busy", action="store_true")
    reservation_create.add_argument("--idempotency-key")
    reservation_create.add_argument("--lease-ttl")
    reservation_create.add_argument("--metadata", action="append", default=[])
    reservation_extend = _read_parser(reservations.add_parser("extend"))
    reservation_extend.add_argument("reservation_id")
    reservation_extend.add_argument("--owner", required=True)
    reservation_extend.add_argument("--duration", required=True)
    reservation_renew = _read_parser(reservations.add_parser("renew"))
    reservation_renew.add_argument("reservation_id")
    reservation_renew.add_argument("--owner", required=True)
    reservation_renew.add_argument("--expected-lease-version", type=int, required=True)
    reservation_renew.add_argument("--lease-ttl")
    reservation_renew.add_argument("--idempotency-key")
    for name in ("release", "cancel"):
        reservation_mutation = _read_parser(reservations.add_parser(name))
        reservation_mutation.add_argument("reservation_id")
        reservation_mutation.add_argument("--owner", required=True)
        if name == "release":
            reservation_mutation.add_argument("--expected-lease-version", type=int)
            reservation_mutation.add_argument("--idempotency-key")
    reservation_queue = _read_parser(reservations.add_parser("queue"))
    reservation_queue.add_argument("bench_id")
    reservation_queue.add_argument("--owner", required=True)
    reservation_queue.add_argument("--duration", required=True)
    reservation_queue.add_argument("--idempotency-key")
    queue_list = _read_parser(reservations.add_parser("queue-list"))
    queue_list.add_argument("bench_id")
    queue_cancel = _read_parser(reservations.add_parser("queue-cancel"))
    queue_cancel.add_argument("queue_entry_id")
    queue_cancel.add_argument("--owner", required=True)

    workflow = commands.add_parser("workflow", help="Run sequential bench workflows.")
    workflows = workflow.add_subparsers(dest="workflow_command", required=True)
    _read_parser(workflows.add_parser("list"))
    workflow_show = _read_parser(workflows.add_parser("show"))
    workflow_show.add_argument("workflow_name")
    workflow_register = _read_parser(workflows.add_parser("register"))
    workflow_register.add_argument("definition", type=Path)
    workflow_run = _read_parser(workflows.add_parser("run"))
    workflow_run.add_argument("workflow_name")
    workflow_run.add_argument("--bench", dest="bench_id")
    workflow_run.add_argument("--owner", required=True)
    workflow_run.add_argument("--version", type=int)
    workflow_run.add_argument("--idempotency-key")
    workflow_run.add_argument("--kind", choices=("simulated", "physical"))
    workflow_run.add_argument("--location")
    workflow_run.add_argument("--bench-label", action="append", default=[])
    workflow_run.add_argument("--agent-label", action="append", default=[])
    workflow_run.add_argument("--reservation-duration")
    workflow_run.add_argument("--lease-ttl")
    workflow_run.add_argument("--command-timeout")
    workflow_run.add_argument("--reservation-id")
    workflow_run.add_argument("--reserve")
    workflow_run.add_argument("--release-after", action="store_true")
    workflow_run.add_argument("--input", action="append", default=[])
    workflow_watch = _read_parser(workflows.add_parser("watch"))
    workflow_watch.add_argument("workflow_run_id")
    workflow_watch.add_argument("--interval", type=float, default=1.0)
    workflow_cancel = _read_parser(workflows.add_parser("cancel"))
    workflow_cancel.add_argument("workflow_run_id")
    workflow_cancel.add_argument("--owner", required=True)
    workflow_results = workflows.add_parser("results")
    workflow_results.add_argument("workflow_run_id")
    workflow_results.add_argument("--format", choices=("json", "junit"), default="json")
    workflow_results.add_argument("--output", type=Path)

    token = commands.add_parser("token", help="Manage machine API tokens.")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    token_create = _read_parser(token_commands.add_parser("create"))
    token_create.add_argument("--name", required=True)
    token_create.add_argument("--owner", required=True)
    token_create.add_argument("--scope", action="append", required=True)
    token_create.add_argument("--expires-at")
    _read_parser(token_commands.add_parser("list"))
    token_revoke = _read_parser(token_commands.add_parser("revoke"))
    token_revoke.add_argument("token_id")

    ci = commands.add_parser("ci", help="Run provider-neutral hardware CI sessions.")
    ci_commands = ci.add_subparsers(dest="ci_command", required=True)
    ci_session = ci_commands.add_parser("session")
    ci_session_commands = ci_session.add_subparsers(dest="ci_session_command", required=True)
    ci_session_create = _read_parser(ci_session_commands.add_parser("create"))
    _add_ci_bench_request_arguments(ci_session_create)
    ci_session_create.add_argument("--external-run-id")
    ci_session_create.add_argument("--repository")
    ci_session_create.add_argument("--ref")
    ci_session_create.add_argument("--commit-sha")
    ci_session_create.add_argument("--actor")
    ci_session_create.add_argument("--idempotency-key")
    ci_session_show = _read_parser(ci_session_commands.add_parser("show"))
    ci_session_show.add_argument("session_id")
    ci_session_watch = _read_parser(ci_session_commands.add_parser("watch"))
    ci_session_watch.add_argument("session_id")
    ci_session_watch.add_argument("--interval", type=float, default=2.0)
    for name in ("cancel", "finalize"):
        mutation = _read_parser(ci_session_commands.add_parser(name))
        mutation.add_argument("session_id")
        mutation.add_argument("--idempotency-key")

    ci_run = _read_parser(ci_commands.add_parser("run"))
    ci_run.add_argument("--workflow", required=True)
    ci_run.add_argument("--artifact", action="append", default=[])
    ci_run.add_argument("--input", action="append", default=[])
    _add_ci_bench_request_arguments(ci_run)
    ci_run.add_argument("--junit-output", type=Path)
    ci_run.add_argument("--artifacts-directory", type=Path)
    ci_run.add_argument("--interval", type=float, default=2.0)

    ci_upload = _read_parser(ci_commands.add_parser("upload"))
    ci_upload.add_argument("session_id")
    ci_upload.add_argument("path", type=Path)
    ci_upload.add_argument("--name")
    ci_upload.add_argument("--artifact-type", default="firmware")
    ci_upload.add_argument("--sha256")
    ci_upload.add_argument("--idempotency-key")
    ci_artifacts = _read_parser(ci_commands.add_parser("artifacts"))
    ci_artifacts.add_argument("session_id")
    ci_download = ci_commands.add_parser("download")
    ci_download.add_argument("artifact_id")
    ci_download.add_argument("--output", type=Path, required=True)

    operation = commands.add_parser("operation", help="Inspect asynchronous operations.")
    operations = operation.add_subparsers(dest="operation_command", required=True)
    show = _read_parser(operations.add_parser("show"))
    show.add_argument("operation_id")
    operation_list = _read_parser(operations.add_parser("list"))
    operation_list.add_argument("--bench-id")
    operation_list.add_argument("--status")
    operation_list.add_argument("--type", dest="operation_type")
    operation_list.add_argument("--limit", type=int, default=50)
    watch = _read_parser(operations.add_parser("watch"))
    watch.add_argument("operation_id")
    watch.add_argument("--interval", type=float, default=1.0)
    cancel = _read_parser(operations.add_parser("cancel"))
    cancel.add_argument("operation_id")
    cancel.add_argument("--owner", required=True)
    reconcile = _read_parser(operations.add_parser("reconcile"))
    reconcile.add_argument("operation_id")

    event = commands.add_parser("event", help="Read stored event history.")
    events = event.add_subparsers(dest="event_command", required=True)
    event_list = _read_parser(events.add_parser("list"))
    event_list.add_argument("--bench-id")
    event_list.add_argument("--event-type")
    event_list.add_argument("--limit", type=int, default=50)

    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--config-dir", type=Path, default=Path("config"))
    return parser


def _add_ci_bench_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bench")
    parser.add_argument("--require", action="append", default=[])
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--prefer-label", action="append", default=[])
    parser.add_argument("--agent-label", action="append", default=[])
    parser.add_argument("--preferred-location")
    parser.add_argument(
        "--allow-simulated",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--allow-physical",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--wait-timeout", default="10m")
    parser.add_argument("--reservation-duration", default="30m")


def _read_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--output", choices=("table", "json"), default="table")
    return parser


def _client(args: argparse.Namespace) -> AgentClient:
    token_env = str(getattr(args, "token_env", "LAB_PLATFORM_TOKEN"))
    return AgentClient(_resolve_server(args), token=os.environ.get(token_env))


def _resolve_server(args: argparse.Namespace) -> str:
    if args.server:
        return str(args.server)
    environment = os.environ.get("LAB_PLATFORM_SERVER")
    if environment:
        return environment
    path = args.config.expanduser()
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            server = raw.get("server")
            if server is None and isinstance(raw.get("cli"), dict):
                server = raw["cli"].get("server")
            if isinstance(server, str) and server.strip():
                return server
    return DEFAULT_SERVER


def _validate_firmware(path: Path) -> tuple[str, int]:
    if not path.is_file():
        raise ValueError(f"Firmware file does not exist: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"Firmware file is empty: {path}")
    if size > DEFAULT_MAX_FIRMWARE_BYTES:
        raise ValueError("Firmware file exceeds the 100 MB client limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), size


def parse_duration(value: str) -> int:
    """Parse a compact duration such as ``30m`` or ``1h30m`` into seconds."""
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("duration cannot be empty")
    total = 0
    cursor = 0
    multipliers = {"h": 3600, "m": 60, "s": 1}
    for match in _DURATION_PART.finditer(normalized):
        if match.start() != cursor:
            raise ValueError(f"invalid duration: {value}")
        total += int(match.group("value")) * multipliers[match.group("unit")]
        cursor = match.end()
    if cursor != len(normalized) or total <= 0:
        raise ValueError(f"invalid duration: {value}")
    return total


def _workflow_inputs(values: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"workflow input must use key=value: {item}")
        inputs[key.strip()] = value
    return inputs


def _api_exit_code(error: AgentApiError) -> int:
    if error.status == 404:
        return 3
    if error.status == 403:
        return 5
    if error.status == 409:
        return 4
    if error.status in {503, 504}:
        return 6
    return 1


def _print_read_payload(payload: object, output: str, table: object) -> None:
    if output == "json":
        _print_json(payload)
    else:
        table(_require_mapping(payload, "response"))  # type: ignore[operator]


def _print_collection(payload: object, output: str, table: object) -> None:
    body = _require_mapping(payload, "collection")
    items = _require_list(body.get("items"), "items")
    if output == "json":
        _print_json(payload)
    else:
        table(items)  # type: ignore[operator]


def _print_operation_created(payload: object, output: str) -> None:
    operation = _require_mapping(payload, "operation")
    if output == "json":
        _print_json(operation)
    else:
        print(f"Operation created: {operation.get('operation_id', '')}")


def _health_table(payload: dict[str, object]) -> None:
    benches = payload.get("benches", {})
    bench_data = benches if isinstance(benches, dict) else {}
    rows = [
        ("Status", str(payload.get("status", "")).title()),
        ("Version", str(payload.get("version", ""))),
        ("Backend", str(payload.get("backend", ""))),
        ("Database", str(payload.get("database", "")).title()),
        ("Benches", f"{bench_data.get('online', 0)}/{bench_data.get('total', 0)} online"),
    ]
    _print_table(("FIELD", "VALUE"), rows)


def _agent_table(items: list[object]) -> None:
    rows: list[tuple[str, ...]] = []
    for item in items:
        agent = _require_mapping(item, "Agent")
        rows.append(
            (
                str(agent.get("id", "")),
                str(agent.get("name") or agent.get("slug") or ""),
                str(agent.get("status", "")).replace("_", " ").title(),
                str(agent.get("location") or "—"),
                str(agent.get("version") or "—"),
                str(agent.get("bench_count", 0)),
            )
        )
    _print_table(("ID", "NAME", "STATUS", "LOCATION", "VERSION", "BENCHES"), rows)


def _agent_show_table(agent: dict[str, object]) -> None:
    connection = agent.get("connection")
    connected = isinstance(connection, dict)
    benches = agent.get("benches")
    bench_count = len(benches) if isinstance(benches, list) else agent.get("bench_count", 0)
    labels = agent.get("labels")
    label_text = (
        ", ".join(f"{key}={value}" for key, value in sorted(labels.items()))
        if isinstance(labels, dict)
        else ""
    )
    _print_table(
        ("FIELD", "VALUE"),
        [
            ("Agent", str(agent.get("name") or agent.get("slug") or agent.get("id", ""))),
            ("ID", str(agent.get("id", ""))),
            ("Status", str(agent.get("status", "")).replace("_", " ").title()),
            ("Location", str(agent.get("location") or "—")),
            ("Version", str(agent.get("version") or "—")),
            ("Protocol", str(agent.get("protocol_version") or "—")),
            ("Connected", "Yes" if connected else "No"),
            ("Last seen", str(agent.get("last_seen_at") or "—")),
            ("Benches", str(bench_count)),
            ("Labels", label_text or "—"),
        ],
    )


def _print_agent_mutation(payload: object, action: str) -> None:
    response = _require_mapping(payload, "Agent mutation")
    nested = response.get("agent")
    agent = _require_mapping(nested, "Agent") if isinstance(nested, dict) else response
    name = str(agent.get("name") or agent.get("slug") or agent.get("id", ""))
    print(f"Agent {name} {action.replace('_', ' ')} requested.")
    print(f"Status: {str(agent.get('status', '')).replace('_', ' ').title()}")


def _enrollment_token_table(items: list[object]) -> None:
    rows: list[tuple[str, ...]] = []
    for item in items:
        token = _require_mapping(item, "enrollment token")
        status = (
            "Revoked"
            if token.get("revoked_at")
            else "Used"
            if token.get("used_at")
            else "Available"
        )
        rows.append(
            (
                str(token.get("id", "")),
                str(token.get("name", "")),
                status,
                str(token.get("expires_at", "")),
            )
        )
    _print_table(("ID", "NAME", "STATUS", "EXPIRES"), rows)


def _agent_timeline_table(items: list[object]) -> None:
    rows: list[tuple[str, ...]] = []
    for item in items:
        entry = _require_mapping(item, "Agent timeline entry")
        rows.append(
            (
                str(entry.get("timestamp", "")),
                str(entry.get("severity", "")).title(),
                str(entry.get("event_type", "")),
                str(entry.get("message", "")),
            )
        )
    _print_table(("TIMESTAMP", "SEVERITY", "EVENT", "MESSAGE"), rows)


def _bench_table(items: list[object]) -> None:
    if any(isinstance(item, dict) and "agent_id" in item for item in items):
        distributed_rows: list[tuple[str, ...]] = []
        for item in items:
            bench = _require_mapping(item, "bench")
            distributed_rows.append(
                (
                    str(bench.get("id", "")),
                    str(bench.get("agent_slug") or bench.get("agent_id") or "—"),
                    str(bench.get("status", "")).replace("_", " ").title(),
                    str(bench.get("kind", "")).title(),
                    str(bench.get("health", "")).title(),
                    str(bench.get("firmware_version") or "—"),
                )
            )
        _print_table(
            ("ID", "AGENT", "STATUS", "KIND", "HEALTH", "FIRMWARE"),
            distributed_rows,
        )
        return
    local_rows: list[tuple[str, ...]] = []
    for item in items:
        bench = _require_mapping(item, "bench")
        local_rows.append(
            (
                str(bench.get("id", "")),
                str(bench.get("status", "")).title(),
                _power(bench.get("powered")),
                str(bench.get("reserved_by") or "—"),
                str(bench.get("firmware_version") or "—"),
            )
        )
    _print_table(("ID", "STATUS", "POWER", "RESERVED BY", "FIRMWARE"), local_rows)


def _bench_show_table(bench: dict[str, object]) -> None:
    capabilities = bench.get("capabilities", [])
    capability_text = (
        ", ".join(str(item).title() for item in capabilities)
        if isinstance(capabilities, list)
        else ""
    )
    _print_table(
        ("FIELD", "VALUE"),
        [
            ("Bench", str(bench.get("id", ""))),
            ("Status", str(bench.get("status", "")).title()),
            ("Reserved by", str(bench.get("reserved_by") or "—")),
            ("Power", _power(bench.get("powered"))),
            ("Firmware", str(bench.get("firmware_version") or "—")),
            ("Capabilities", capability_text),
        ],
    )


def _probe_table(health: dict[str, object]) -> None:
    _print_table(
        ("FIELD", "VALUE"),
        [
            ("Bench", str(health.get("bench_id", ""))),
            ("Status", str(health.get("status", "")).title()),
            ("Chip", str(health.get("chip_type") or "—")),
            ("Serial port", str(health.get("serial_port") or "—")),
            ("MAC address", str(health.get("mac_address") or "—")),
        ],
    )


def _operation_table(items: list[object]) -> None:
    rows = []
    for item in items:
        operation = _require_mapping(item, "operation")
        rows.append(
            (
                str(operation.get("id", "")),
                str(operation.get("bench_id", "")),
                str(operation.get("type", "")).replace("_", " ").title(),
                str(operation.get("status", "")).replace("_", " ").title(),
                f"{_integer(operation.get('progress'))}%",
            )
        )
    _print_table(("ID", "BENCH", "TYPE", "STATUS", "PROGRESS"), rows)


def _operation_show_table(operation: dict[str, object]) -> None:
    _print_table(
        ("FIELD", "VALUE"),
        [
            (str(key).replace("_", " ").title(), str(value or "—"))
            for key, value in operation.items()
        ],
    )


def _event_table(items: list[object]) -> None:
    rows = []
    for item in items:
        event = _require_mapping(item, "event")
        rows.append(
            (
                str(event.get("timestamp", "")),
                str(event.get("type", "")),
                str(event.get("bench_id") or "—"),
                str(event.get("actor") or "—"),
            )
        )
    _print_table(("TIMESTAMP", "TYPE", "BENCH", "ACTOR"), rows)


def _reservation_table(items: list[object]) -> None:
    rows = []
    for item in items:
        reservation = _normalized_reservation(_require_mapping(item, "reservation"))
        rows.append(
            (
                str(reservation.get("id", "")),
                str(reservation.get("bench_id", "")),
                str(reservation.get("owner", "")),
                str(reservation.get("status", "")).replace("_", " ").title(),
                str(reservation.get("starts_at") or "—"),
                str(reservation.get("ends_at") or "—"),
            )
        )
    _print_table(("ID", "BENCH", "OWNER", "STATUS", "STARTS", "ENDS"), rows)


def _reservation_show_table(reservation: dict[str, object]) -> None:
    reservation = _normalized_reservation(reservation)
    _print_table(
        ("FIELD", "VALUE"),
        [
            (str(key).replace("_", " ").title(), str(value if value is not None else "—"))
            for key, value in reservation.items()
        ],
    )


def _print_reservation(payload: object, output: str, action: str | None = None) -> None:
    raw = _require_mapping(payload, "reservation")
    reservation = _normalized_reservation(raw)
    if output == "json":
        _print_json(raw)
        return
    status = str(reservation.get("status", "reservation")).replace("_", " ")
    verb = action or status
    print(f"Reservation {verb}.")
    print(f"Reservation ID: {reservation.get('id') or reservation.get('reservation_id', '')}")
    print(f"Bench:          {reservation.get('bench_id', '')}")
    print(f"Owner:          {reservation.get('owner', '')}")
    print(f"Ends:           {reservation.get('ends_at') or '—'}")


def _normalized_reservation(payload: dict[str, object]) -> dict[str, object]:
    nested = payload.get("reservation")
    if not isinstance(nested, dict):
        return payload
    reservation = cast(dict[str, object], nested)
    lease = payload.get("lease")
    lease_data = cast(dict[str, object], lease) if isinstance(lease, dict) else {}
    return {
        **reservation,
        "id": reservation.get("reservation_id") or reservation.get("id"),
        "status": payload.get("state") or reservation.get("status"),
        "lease_version": lease_data.get("lease_version"),
        "ends_at": lease_data.get("valid_until") or reservation.get("ends_at"),
        "revision": payload.get("revision"),
    }


def _queue_table(items: list[object]) -> None:
    rows = []
    for item in items:
        entry = _require_mapping(item, "queue entry")
        rows.append(
            (
                str(entry.get("position") or "—"),
                str(entry.get("id", "")),
                str(entry.get("owner", "")),
                str(entry.get("requested_duration_seconds", "")),
                str(entry.get("status", "")).replace("_", " ").title(),
            )
        )
    _print_table(("POSITION", "ID", "OWNER", "DURATION (S)", "STATUS"), rows)


def _timeline_table(items: list[object]) -> None:
    rows = []
    for item in items:
        entry = _require_mapping(item, "timeline entry")
        rows.append(
            (
                str(entry.get("timestamp", "")),
                str(entry.get("category", "")).title(),
                str(entry.get("event_type", "")),
                str(entry.get("actor") or "—"),
                str(entry.get("summary", "")),
            )
        )
    _print_table(("TIMESTAMP", "CATEGORY", "EVENT", "ACTOR", "SUMMARY"), rows)


def _workflow_table(items: list[object]) -> None:
    rows = []
    for item in items:
        workflow = _require_mapping(item, "workflow")
        steps = workflow.get("steps", [])
        count = len(steps) if isinstance(steps, list) else 0
        rows.append(
            (
                str(workflow.get("name", "")),
                str(workflow.get("version", "")),
                str(count),
                str(workflow.get("description") or "—"),
            )
        )
    _print_table(("NAME", "VERSION", "STEPS", "DESCRIPTION"), rows)


def _workflow_show_table(workflow: dict[str, object]) -> None:
    requirements = workflow.get("requirements", {})
    required_capabilities = (
        requirements.get("capabilities", []) if isinstance(requirements, dict) else []
    )
    capabilities = (
        ", ".join(str(item) for item in required_capabilities)
        if isinstance(required_capabilities, list)
        else ""
    )
    steps = workflow.get("steps", [])
    _print_table(
        ("FIELD", "VALUE"),
        [
            ("Name", str(workflow.get("name", ""))),
            ("Version", str(workflow.get("version", ""))),
            ("Description", str(workflow.get("description") or "—")),
            ("Capabilities", capabilities or "—"),
            ("Steps", str(len(steps) if isinstance(steps, list) else 0)),
        ],
    )


def _power(value: object) -> str:
    return "On" if value is True else "Off" if value is False else "Unknown"


def _integer(value: object) -> int:
    return int(value) if isinstance(value, (int, str)) else 0


def _require_mapping(payload: object, name: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"Agent returned invalid {name} data")
    return cast(dict[str, object], payload)


def _require_list(payload: object, name: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"Agent returned invalid {name} data")
    return cast(list[object], payload)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    print(render(headers))
    for row in rows:
        print(render(row))


if __name__ == "__main__":
    raise SystemExit(main())
