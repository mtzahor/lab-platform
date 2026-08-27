from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.agent.server import AgentHttpServer
from lab_platform.cli.client import AgentApiError, AgentClient, AgentConnectionError
from lab_platform.config import (
    HardwareBenchSettings,
    PlatformConfig,
    RealBackendSettings,
    load_config,
)
from lab_platform.core import VERSION
from lab_platform.core.errors import PlatformError
from lab_platform.models import RemoteCommandStatus
from lab_platform.plugin_sdk import DiagnosticStatus, PluginRuntimeStatus
from lab_platform.plugins import PluginManager
from lab_platform.real_backend.discovery import SerialPortDiscovery
from lab_platform.real_backend.targets import default_target_registry


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "connect":
            return _connect(args)
        if args.command == "status":
            return _status_command(args, connection_only=False)
        if args.command == "doctor":
            return _doctor_command(args)
        if args.command == "connection" and args.connection_command == "status":
            return _status_command(args, connection_only=True)
        if args.command == "journal" and args.journal_command == "list":
            return _journal_list(args)
        if args.command == "reconnect":
            return _reconnect_command(args)
        return _serve(args)
    except AgentApiError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except (AgentConnectionError, OSError, PlatformError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _serve(args: argparse.Namespace) -> int:
    agent = create_agent(_config_source(args))

    if args.once:
        asyncio.run(_run_once(agent))
        return 0

    host = args.host or agent.config.agent.host
    port = args.port or agent.config.agent.port
    server = AgentHttpServer(agent=agent, host=host, port=port)
    try:
        _print_server_startup(agent)
        print(f"Listening on http://{server.host}:{server.port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Lab Agent")
    finally:
        server.shutdown()
    return 0


async def _run_once(agent: LabAgent) -> None:
    await agent.start()
    try:
        _print_startup(agent)
    finally:
        await agent.shutdown()


def _connect(args: argparse.Namespace) -> int:
    config = load_config(_config_source(args))
    token = os.environ.get(args.enrollment_token_env)
    if token is None or not token.strip():
        if config.identity.agent_id is not None and os.environ.get(
            config.identity.credential_env_var
        ):
            return _serve(args)
        raise ValueError(
            f"enrollment token environment variable is not set: {args.enrollment_token_env}"
        )
    control_plane = str(args.control_plane).rstrip("/")
    parsed = urlsplit(control_plane)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("--control-plane must be an HTTP(S) control-plane URL")
    if parsed.scheme == "http" and not (
        config.control_plane.allow_insecure_loopback and _is_loopback_host(parsed.hostname)
    ):
        raise ValueError(
            "Agent enrollment requires HTTPS; HTTP is allowed only for an explicitly enabled "
            "loopback development control plane"
        )
    response = _require_mapping(
        AgentClient(control_plane).post(
            "/api/v1/agents/enroll",
            {
                "enrollment_token": token,
                "request_id": str(uuid4()),
                "agent_version": VERSION,
                "protocol_version": "1.0",
                "location": args.location or config.agent.location,
            },
        ),
        "enrollment response",
    )
    agent = _require_mapping(response.get("agent"), "enrolled Agent")
    credential = _required_text(response.get("credential"), "Agent credential")
    gateway_url = _required_text(response.get("gateway_url"), "Agent gateway URL")
    agent_id = _required_text(agent.get("id"), "Agent ID")
    if args.output == "json":
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0

    print("Agent enrolled. Credentials were not written to disk.")
    print(f"Agent ID: {agent_id}")
    print(f"Gateway:  {gateway_url}")
    print()
    print("Set the credential in this Agent's secret environment:")
    print(f"export {args.credential_env}={shlex.quote(credential)}")
    print()
    print("Add this non-secret identity configuration, then start lab-agent again:")
    print("control_plane:")
    print("  enabled: true")
    print(f"  url: {gateway_url}")
    print("identity:")
    print(f"  agent_id: {agent_id}")
    print(f"  credential_env_var: {args.credential_env}")
    return 0


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().casefold().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _status_command(args: argparse.Namespace, *, connection_only: bool) -> int:
    path = "/api/v1/agent/connection" if connection_only else "/api/v1/agent/status"
    payload = _require_mapping(_local_client(args).get(path), "local Agent status")
    _print_local_status(payload, output=args.output, connection_only=connection_only)
    if connection_only and not bool(payload.get("connected")):
        return 1
    return 0


def _doctor_command(args: argparse.Namespace) -> int:
    checks: list[dict[str, object]] = []
    config: PlatformConfig | None = None
    try:
        config = load_config(_config_source(args))
        _doctor_check(checks, "configuration", True, "Configuration is valid")
    except (OSError, ValueError) as exc:
        _doctor_check(checks, "configuration", False, str(exc))

    if config is not None:
        database_ok = config.database.url.startswith("sqlite:///")
        _doctor_check(
            checks,
            "database",
            database_ok,
            "SQLite database URL configured"
            if database_ok
            else "Only sqlite:/// Agent database URLs are supported",
        )
        if config.control_plane.enabled:
            credential_present = bool(os.environ.get(config.identity.credential_env_var))
            _doctor_check(
                checks,
                "credential",
                credential_present,
                f"Secret is available via {config.identity.credential_env_var}"
                if credential_present
                else f"Secret is missing from {config.identity.credential_env_var}",
            )
            for name, path in (
                ("client certificate", config.identity.certificate_path),
                ("private key", config.identity.private_key_path),
                ("CA certificate", config.identity.ca_path),
            ):
                if path is not None:
                    resolved = (
                        path if path.is_absolute() else _storage_root(_config_source(args)) / path
                    )
                    _doctor_check(checks, name, resolved.is_file(), f"{resolved}")
        else:
            _doctor_check(
                checks,
                "control plane",
                True,
                "Distributed mode is disabled",
            )
        checks.extend(asyncio.run(_hardware_doctor_checks(config, _config_source(args))))

    if args.output == "json":
        print(json.dumps({"checks": checks}, indent=2, sort_keys=True))
    else:
        _print_rows(
            ("CHECK", "RESULT", "DETAIL"),
            [
                (
                    str(check["name"]),
                    "PASS" if check["ok"] else "FAIL",
                    str(check["detail"]),
                )
                for check in checks
            ],
        )
    return 0 if checks and all(bool(check["ok"]) for check in checks) else 1


async def _hardware_doctor_checks(
    config: PlatformConfig,
    config_source: Path,
) -> list[dict[str, object]]:
    """Inspect plugin, driver, resource, permission, and physical-access prerequisites."""

    checks: list[dict[str, object]] = []
    manager = PluginManager(
        plugin_directories=config.plugin_directories,
        allow_import_paths=config.allow_plugin_import_paths,
    )
    try:
        load_report = await manager.load_resilient(config.plugins)
        _doctor_check(
            checks,
            "plugins",
            load_report.ok,
            (
                f"Loaded {len(load_report.loaded)} configured plugin(s)"
                if load_report.ok
                else "; ".join(
                    f"{failure.plugin}: {failure.code} ({failure.message})"
                    for failure in load_report.failures
                )
            ),
        )
        for report in await manager.diagnose_all():
            failed = [check for check in report.checks if check.status is DiagnosticStatus.FAIL]
            healthy = report.status is PluginRuntimeStatus.HEALTHY and not failed
            detail = (
                ", ".join(f"{check.name}={check.status.value}" for check in report.checks)
                or report.status.value
            )
            _doctor_check(checks, f"plugin:{report.plugin}", healthy, detail)
    finally:
        shutdown_failures = await manager.shutdown()
        if shutdown_failures:
            _doctor_check(
                checks,
                "plugin cleanup",
                False,
                "; ".join(f"{failure.plugin}: {failure.message}" for failure in shutdown_failures),
            )

    real_backends = [
        backend for backend in config.effective_backends if isinstance(backend, RealBackendSettings)
    ]
    benches = [bench for backend in real_backends for bench in backend.config.benches]
    _doctor_check(
        checks,
        "resources",
        True,
        f"{len(benches)} physical bench resource definition(s) validated",
    )

    target_types = set(default_target_registry().target_types)
    unsupported = sorted(
        {
            bench.target_type
            for bench in benches
            if _normalise_target_type(bench.target_type) not in target_types
        }
    )
    _doctor_check(
        checks,
        "drivers",
        not unsupported,
        (
            f"Drivers available for {len(benches)} configured target(s)"
            if not unsupported
            else f"Unsupported target type(s): {', '.join(unsupported)}"
        ),
    )
    _doctor_check(
        checks,
        "resource conflicts",
        True,
        "Configured backend and bench identifiers are unique",
    )

    storage_root = _storage_root(config_source)
    artifact_directory = _resolve_from(storage_root, config.artifacts.directory)
    writable_root = _nearest_existing_parent(artifact_directory)
    _doctor_check(
        checks,
        "permissions:artifacts",
        os.access(writable_root, os.W_OK | os.X_OK),
        f"Creation root: {writable_root}",
    )
    for directory in config.plugin_directories:
        resolved = _resolve_from(storage_root, directory)
        accessible = resolved.is_dir() and os.access(resolved, os.R_OK | os.X_OK)
        _doctor_check(
            checks,
            f"permissions:plugin-directory:{resolved}",
            accessible,
            "Readable plugin directory" if accessible else "Directory is missing or unreadable",
        )

    if not benches:
        for name in ("serial", "usb", "external tools"):
            _doctor_check(
                checks,
                name,
                True,
                "No physical target resources are configured",
            )

    discovery = SerialPortDiscovery()
    for bench in benches:
        try:
            port = discovery.resolve(bench.connection)
        except (ImportError, OSError, PlatformError) as exc:
            _doctor_check(checks, f"serial:{bench.id}", False, str(exc))
            _doctor_check(checks, f"usb:{bench.id}", False, str(exc))
        else:
            accessible = os.access(port.device, os.R_OK | os.W_OK)
            detail = (
                f"{port.device} is readable and writable"
                if accessible
                else f"{port.device} exists but is not readable and writable"
            )
            _doctor_check(checks, f"serial:{bench.id}", accessible, detail)
            selector = bench.connection.usb
            stable_identity = bool(
                selector.serial_number
                or selector.vendor_id is not None
                or selector.product_id is not None
                or bench.connection.serial_port.casefold() != "auto"
            )
            _doctor_check(
                checks,
                f"usb:{bench.id}",
                stable_identity,
                (
                    f"Resolved stable device identity to {port.device}"
                    if stable_identity
                    else "Device resolved, but no stable USB selector is configured"
                ),
            )

        executable = _target_executable(bench)
        if executable is None:
            mount = bench.rp2040.mount_path
            ready = mount is not None and mount.is_dir() and os.access(mount, os.W_OK | os.X_OK)
            _doctor_check(
                checks,
                f"external-tool:{bench.id}",
                ready,
                f"UF2 mount is {'ready' if ready else 'missing or unwritable'}: {mount}",
            )
        else:
            tool_path = shutil.which(executable)
            _doctor_check(
                checks,
                f"external-tool:{bench.id}",
                tool_path is not None,
                tool_path or f"Executable not found: {executable}",
            )
    return checks


def _target_executable(bench: HardwareBenchSettings) -> str | None:
    target_type = _normalise_target_type(bench.target_type)
    if target_type in {"stm32", "openocd"}:
        return bench.openocd.executable
    if target_type == "jlink":
        return bench.jlink.executable
    if target_type == "nrf52":
        return bench.jlink.executable if bench.nrf52.tool == "jlink" else bench.nrf52.executable
    if target_type in {"pico", "raspberry-pi-pico", "rp2040"}:
        return None if bench.rp2040.tool == "uf2" else bench.rp2040.executable
    return bench.flash.tool


def _normalise_target_type(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _resolve_from(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _journal_list(args: argparse.Namespace) -> int:
    response = _require_mapping(
        _local_client(args).get(
            "/api/v1/agent/journal",
            {"status": args.status, "limit": args.limit},
        ),
        "command journal",
    )
    raw_entries = response.get("items")
    if not isinstance(raw_entries, list) or not all(
        isinstance(entry, dict) for entry in raw_entries
    ):
        raise ValueError("Local Agent returned invalid command journal entries")
    entries = cast(list[dict[str, object]], raw_entries)
    if args.output == "json":
        print(json.dumps({"items": entries}, indent=2, sort_keys=True))
    else:
        _print_rows(
            ("COMMAND", "TYPE", "BENCH", "STATUS", "RECEIVED"),
            [
                (
                    str(entry["command_id"]),
                    str(entry["command_type"]),
                    str(entry["bench_id"]),
                    str(entry["status"]),
                    str(entry["received_at"]),
                )
                for entry in entries
            ],
        )
    return 0


def _reconnect_command(args: argparse.Namespace) -> int:
    payload = _require_mapping(
        _local_client(args).post("/api/v1/agent/reconnect", {}),
        "reconnect response",
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Control-plane reconnect requested: {payload.get('endpoint', '')}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-agent")
    _add_config_arguments(parser)
    parser.add_argument("--host", default=None, help="Override the configured HTTP host.")
    parser.add_argument("--port", type=int, default=None, help="Override the configured HTTP port.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Start the agent, print the startup summary, and exit without serving HTTP.",
    )
    commands = parser.add_subparsers(dest="command")

    status = commands.add_parser("status", help="Show local Agent and connection status.")
    _add_config_arguments(status, suppress_defaults=True)
    _add_local_api_arguments(status)
    _add_diagnostic_output(status)

    doctor = commands.add_parser("doctor", help="Validate local Agent configuration and startup.")
    _add_config_arguments(doctor, suppress_defaults=True)
    _add_diagnostic_output(doctor)

    connection = commands.add_parser("connection", help="Inspect the control-plane channel.")
    connection_commands = connection.add_subparsers(dest="connection_command", required=True)
    connection_status = connection_commands.add_parser("status")
    _add_config_arguments(connection_status, suppress_defaults=True)
    _add_local_api_arguments(connection_status)
    _add_diagnostic_output(connection_status)

    journal = commands.add_parser("journal", help="Inspect the durable command journal.")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    journal_list = journal_commands.add_parser("list")
    _add_config_arguments(journal_list, suppress_defaults=True)
    _add_local_api_arguments(journal_list)
    _add_diagnostic_output(journal_list)
    journal_list.add_argument(
        "--status",
        type=str.upper,
        choices=tuple(status.value for status in RemoteCommandStatus),
    )
    journal_list.add_argument("--limit", type=int, default=100)

    reconnect = commands.add_parser("reconnect", help="Force a control-plane reconnect cycle.")
    _add_config_arguments(reconnect, suppress_defaults=True)
    _add_local_api_arguments(reconnect)
    _add_diagnostic_output(reconnect)

    connect = commands.add_parser("connect", help="Enroll with or connect to a control plane.")
    _add_config_arguments(connect, suppress_defaults=True)
    connect.add_argument("--control-plane", required=True)
    connect.add_argument(
        "--enrollment-token-env",
        default="LAB_AGENT_ENROLLMENT_TOKEN",
        help="Environment variable containing the one-time enrollment token.",
    )
    connect.add_argument(
        "--credential-env",
        default="LAB_AGENT_CREDENTIAL",
        help="Secret environment variable to reference in the generated configuration snippet.",
    )
    connect.add_argument("--location")
    connect.add_argument("--output", choices=("table", "json"), default="table")
    connect.add_argument("--host", default=None, help=argparse.SUPPRESS)
    connect.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    connect.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser


def _add_config_arguments(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
) -> None:
    default_dir: Path | str = argparse.SUPPRESS if suppress_defaults else Path("config")
    default_config: None | str = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_dir,
        help="Directory containing agent.yaml and simlab.yaml.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="Path to one complete Agent YAML configuration file.",
    )


def _add_diagnostic_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", choices=("table", "json"), default="table")


def _add_local_api_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        help="Running local Agent API URL (defaults to the configured host and port).",
    )
    parser.add_argument(
        "--token-env",
        default="LAB_PLATFORM_TOKEN",
        help="Environment variable containing a local Agent API token.",
    )


def _config_source(args: argparse.Namespace) -> Path:
    config = getattr(args, "config", None)
    return cast(Path, config or getattr(args, "config_dir", Path("config")))


def _storage_root(config_source: Path) -> Path:
    return (
        config_source.parent
        if config_source.is_file() or config_source.name == "config"
        else config_source
    )


def _local_client(args: argparse.Namespace) -> AgentClient:
    config = load_config(_config_source(args))
    host = config.agent.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    url = args.url or f"http://{host}:{config.agent.port}"
    return AgentClient(url, token=os.environ.get(args.token_env))


def _doctor_check(
    checks: list[dict[str, object]],
    name: str,
    ok: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})


def _print_local_status(
    payload: dict[str, object],
    *,
    output: str,
    connection_only: bool,
) -> None:
    if output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    connection = (
        payload
        if connection_only
        else _require_mapping(payload.get("control_plane"), "control-plane status")
    )
    if connection_only:
        rows = [
            ("Enabled", "Yes" if connection.get("enabled") else "No"),
            ("Connected", "Yes" if connection.get("connected") else "No"),
            ("Endpoint", str(connection.get("endpoint") or "—")),
            ("Queued messages", str(connection.get("queued_messages", 0))),
            ("Buffered events", str(connection.get("event_buffer_size", 0))),
        ]
    else:
        benches = payload.get("benches")
        bench_data = benches if isinstance(benches, dict) else {}
        rows = [
            ("Agent", str(payload.get("agent_name", ""))),
            ("Status", str(payload.get("status", "")).title()),
            ("Version", str(payload.get("version", ""))),
            ("Location", str(payload.get("location") or "—")),
            (
                "Benches",
                f"{bench_data.get('online', 0)}/{bench_data.get('total', 0)} online",
            ),
            ("Control plane", "Connected" if connection.get("connected") else "Disconnected"),
        ]
    _print_rows(("FIELD", "VALUE"), rows)


def _print_rows(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    print(render(headers))
    for row in rows:
        print(render(row))


def _require_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Control plane returned invalid {name} data")
    return cast(dict[str, object], value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Control plane did not return a valid {name}")
    return value.strip()


def _print_startup(agent: LabAgent) -> None:
    print(f"Lab Agent v{VERSION}")
    print("✓ Configuration loaded")
    print("✓ Logging initialized")
    print("✓ Event bus started")
    backends = agent.config.effective_backends
    if len(backends) > 1:
        identifiers = ", ".join(backend.id for backend in backends)
        print(f"✓ {len(backends)} backends registered: {identifiers}")
    elif backends[0].type == "real":
        print("✓ RealLabBackend started")
    elif agent.config.simlab.enabled:
        print("✓ SimLab backend started")
    else:
        print("✓ SimLab backend disabled")
    print(f"✓ {len(agent.benches())} benches registered")
    print(f"✓ {len(agent.plugins())} plugins loaded")
    print()


def _print_server_startup(agent: LabAgent) -> None:
    """Print information available before the ASGI lifespan starts the Agent."""

    print(f"Lab Agent v{VERSION}")
    print("\u2713 Configuration loaded")
    print("\u2713 Logging initialized")
    print("\u2713 Agent lifecycle delegated to the HTTP server")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
