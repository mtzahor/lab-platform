from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from lab_platform.cli.client import AgentClient, AgentConnectionError
from lab_platform.config import validate_config
from lab_platform.core import VERSION
from pydantic import ValidationError


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "version":
            print(f"labctl {VERSION}")
        elif args.command == "health":
            _print_health(_client(args).get("health"))
        elif args.command == "benches":
            _print_benches(_client(args).get("benches"))
        elif args.command == "plugins":
            _print_plugins(_client(args).get("plugins"))
        elif args.command == "config" and args.config_command == "validate":
            validate_config(args.config_dir)
            print(f"Configuration valid: {args.config_dir}")
        else:  # pragma: no cover - argparse guarantees a command
            raise AssertionError("unreachable command")
    except (AgentConnectionError, OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labctl")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080",
        help="Lab Agent base URL (default: %(default)s).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Show the Lab Platform version.")
    commands.add_parser("health", help="Show Agent health.")
    commands.add_parser("benches", help="List SimLab benches.")
    commands.add_parser("plugins", help="List loaded plugins.")

    config = commands.add_parser("config", help="Configuration commands.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate", help="Validate YAML configuration.")
    validate.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing agent.yaml and simlab.yaml.",
    )
    return parser


def _client(args: argparse.Namespace) -> AgentClient:
    return AgentClient(str(args.url))


def _print_health(payload: object) -> None:
    health = _require_mapping(payload, "health")
    print(json.dumps(health, indent=2, sort_keys=True))


def _print_benches(payload: object) -> None:
    benches = _require_list(payload, "benches")
    rows: list[tuple[str, str, str]] = []
    for item in benches:
        bench = _require_mapping(item, "bench")
        capabilities = bench.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ValueError("Agent returned invalid bench capabilities")
        rows.append(
            (
                str(bench.get("name", "")),
                str(bench.get("status", "")).title(),
                ", ".join(str(capability) for capability in capabilities),
            )
        )
    _print_table(("NAME", "STATUS", "CAPABILITIES"), rows)


def _print_plugins(payload: object) -> None:
    plugins = _require_list(payload, "plugins")
    rows: list[tuple[str, str, str]] = []
    for item in plugins:
        plugin = _require_mapping(item, "plugin")
        capabilities = plugin.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ValueError("Agent returned invalid plugin capabilities")
        rows.append(
            (
                str(plugin.get("name", "")),
                str(plugin.get("version", "")),
                ", ".join(str(capability) for capability in capabilities),
            )
        )
    _print_table(("NAME", "VERSION", "CAPABILITIES"), rows)


def _require_mapping(payload: object, name: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"Agent returned invalid {name} data")
    return cast(dict[str, object], payload)


def _require_list(payload: object, name: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"Agent returned invalid {name} data")
    return cast(list[object], payload)


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
