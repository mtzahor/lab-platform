from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def _bench_list(client: AgentClient, args: argparse.Namespace) -> None:
    payload = client.get(
        "/api/v1/benches",
        {
            "status": getattr(args, "status", None),
            "capability": getattr(args, "capability", None),
            "reserved": getattr(args, "reserved", None),
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
    bench_show = _read_parser(benches.add_parser("show"))
    bench_show.add_argument("bench_id")
    for name in ("reserve", "release", "power-on", "power-off", "power-cycle"):
        action = _read_parser(benches.add_parser(name))
        action.add_argument("bench_id")
        action.add_argument("--owner", required=True)
    flash = _read_parser(benches.add_parser("flash"))
    flash.add_argument("bench_id")
    flash.add_argument("firmware_path", type=Path)
    flash.add_argument("--owner", required=True)
    flash.add_argument("--version")

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


def _api_exit_code(error: AgentApiError) -> int:
    if error.status == 404:
        return 3
    if error.status == 403:
        return 5
    if error.status == 409:
        return 4
    if error.status == 503:
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
