from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import yaml
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError
from lab_platform.config import validate_config
from pydantic import ValidationError

DEFAULT_SERVER = "http://127.0.0.1:8080"
DEFAULT_MAX_FIRMWARE_BYTES = 100 * 1024 * 1024
_DURATION_PART = re.compile(r"(?P<value>\d+)(?P<unit>[hms])", re.IGNORECASE)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except AgentApiError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return _api_exit_code(exc)
    except AgentConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 6
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    if args.command == "bench":
        return _bench_command(client, args)
    if args.command == "reservation":
        return _reservation_command(client, args)
    if args.command == "workflow":
        return _workflow_command(client, args)
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
        _print_read_payload(payload, args.output, _probe_table)
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
        if operation.get("status") != "succeeded":
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
        payload = client.get(
            "/api/v1/reservations",
            {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "status": args.status,
                "starts_after": args.starts_after,
                "starts_before": args.starts_before,
                "limit": args.limit,
            },
        )
        _print_collection(payload, args.output, _reservation_table)
        return 0
    if command == "show":
        payload = client.get(f"/api/v1/reservations/{args.reservation_id}")
        _print_read_payload(payload, args.output, _reservation_show_table)
        return 0
    if command == "create":
        payload = client.post(
            "/api/v1/reservations",
            {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "starts_at": args.start,
                "duration_seconds": parse_duration(args.duration),
                "queue_if_busy": args.queue_if_busy,
                "idempotency_key": args.idempotency_key,
            },
        )
        _print_reservation(payload, args.output)
        return 0
    if command == "extend":
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/extend",
            {"owner": args.owner, "duration_seconds": parse_duration(args.duration)},
        )
        _print_reservation(payload, args.output)
        return 0
    if command == "release":
        payload = client.post(
            f"/api/v1/reservations/{args.reservation_id}/release",
            {"owner": args.owner},
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
    if command == "run":
        inputs = _workflow_inputs(args.input)
        payload = client.post(
            f"/api/v1/workflows/{args.workflow_name}/runs",
            {
                "bench_id": args.bench_id,
                "owner": args.owner,
                "reservation_id": args.reservation_id,
                "reserve_duration_seconds": (
                    parse_duration(args.reserve) if args.reserve is not None else None
                ),
                "release_after": args.release_after,
                "inputs": inputs,
            },
        )
        if args.output == "json":
            _print_json(payload)
        else:
            run = _require_mapping(payload, "workflow run")
            print(f"Workflow run created: {run.get('id', '')}")
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
    raise AssertionError("unreachable workflow command")


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
        status = str(payload.get("status", ""))
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
        status = str(payload.get("status", ""))
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
        if operation.get("status") in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(interval)


def _bench_list(client: AgentClient, args: argparse.Namespace) -> None:
    payload = client.get(
        "/api/v1/benches",
        {
            "status": getattr(args, "status", None),
            "capability": getattr(args, "capability", None),
            "reserved": getattr(args, "reserved", None),
            "online": True if getattr(args, "online", False) else None,
            "available": True if getattr(args, "available", False) else None,
            "label": getattr(args, "label", None),
        },
    )
    _print_collection(payload, args.output, _bench_table)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labctl")
    parser.add_argument("--server", "--url", dest="server", default=None)
    parser.add_argument("--config", type=Path, default=Path("~/.config/lab-platform/cli.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)

    _read_parser(commands.add_parser("version"))
    _read_parser(commands.add_parser("health"))
    _read_parser(commands.add_parser("benches", help=argparse.SUPPRESS))
    _read_parser(commands.add_parser("plugins", help=argparse.SUPPRESS))

    bench = commands.add_parser("bench", help="Inspect and control benches.")
    benches = bench.add_subparsers(dest="bench_command", required=True)
    bench_list = _read_parser(benches.add_parser("list"))
    bench_list.add_argument("--status")
    bench_list.add_argument("--capability")
    bench_list.add_argument("--reserved", action=argparse.BooleanOptionalAction, default=None)
    bench_list.add_argument("--online", action="store_true")
    bench_list.add_argument("--available", action="store_true")
    bench_list.add_argument("--label", action="append", default=[])
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
    reservation_extend = _read_parser(reservations.add_parser("extend"))
    reservation_extend.add_argument("reservation_id")
    reservation_extend.add_argument("--owner", required=True)
    reservation_extend.add_argument("--duration", required=True)
    for name in ("release", "cancel"):
        reservation_mutation = _read_parser(reservations.add_parser(name))
        reservation_mutation.add_argument("reservation_id")
        reservation_mutation.add_argument("--owner", required=True)
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
    workflow_run = _read_parser(workflows.add_parser("run"))
    workflow_run.add_argument("workflow_name")
    workflow_run.add_argument("--bench", dest="bench_id", required=True)
    workflow_run.add_argument("--owner", required=True)
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


def _read_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--output", choices=("table", "json"), default="table")
    return parser


def _client(args: argparse.Namespace) -> AgentClient:
    return AgentClient(_resolve_server(args))


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


def _bench_table(items: list[object]) -> None:
    rows = []
    for item in items:
        bench = _require_mapping(item, "bench")
        rows.append(
            (
                str(bench.get("id", "")),
                str(bench.get("status", "")).title(),
                _power(bench.get("powered")),
                str(bench.get("reserved_by") or "—"),
                str(bench.get("firmware_version") or "—"),
            )
        )
    _print_table(("ID", "STATUS", "POWER", "RESERVED BY", "FIRMWARE"), rows)


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
        reservation = _require_mapping(item, "reservation")
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
    _print_table(
        ("FIELD", "VALUE"),
        [
            (str(key).replace("_", " ").title(), str(value if value is not None else "—"))
            for key, value in reservation.items()
        ],
    )


def _print_reservation(payload: object, output: str, action: str | None = None) -> None:
    reservation = _require_mapping(payload, "reservation")
    if output == "json":
        _print_json(reservation)
        return
    status = str(reservation.get("status", "reservation")).replace("_", " ")
    verb = action or status
    print(f"Reservation {verb}.")
    print(f"Reservation ID: {reservation.get('id', '')}")
    print(f"Bench:          {reservation.get('bench_id', '')}")
    print(f"Owner:          {reservation.get('owner', '')}")
    print(f"Ends:           {reservation.get('ends_at') or '—'}")


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
