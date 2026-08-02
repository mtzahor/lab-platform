from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from lab_platform.control_plane.config import ControlPlaneConfig, load_control_plane_config
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane.server import ControlPlaneHttpServer
from lab_platform.core import VERSION


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = _with_bind_host(load_control_plane_config(args.config), args.host)
    runtime = ControlPlaneRuntime(config)
    if args.command == "migrate":
        if args.once:
            parser.error("migrate cannot be combined with --once")
        _run_migrations(runtime)
        return 0
    if args.once:
        asyncio.run(_run_once(runtime))
        return 0

    host = config.control_plane.host
    port = args.port or config.control_plane.port
    server = ControlPlaneHttpServer(runtime, host=host, port=port)
    print(f"Lab Control Plane v{VERSION}")
    print(f"Listening on {config.control_plane.public_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Lab Control Plane")
    finally:
        server.shutdown()
    return 0


async def _run_once(runtime: ControlPlaneRuntime) -> None:
    await runtime.start()
    try:
        values = await runtime.metrics()
        print(f"Lab Control Plane v{VERSION}")
        print("✓ Configuration loaded")
        print("✓ Database migrations applied")
        print(f"✓ {values['agents_online']} Agents online")
        print(f"✓ {values['bench_inventory_total']} benches in unified inventory")
    finally:
        await runtime.stop()


def _run_migrations(runtime: ControlPlaneRuntime) -> None:
    runtime.database.initialize()
    try:
        print(f"Lab Control Plane v{VERSION}")
        print("✓ Configuration loaded")
        print("✓ Database migrations applied")
    finally:
        runtime.database.close()


def _with_bind_host(config: ControlPlaneConfig, host: str | None) -> ControlPlaneConfig:
    """Re-run transport policy validation for a command-line bind override."""

    if host is None:
        return config
    payload = config.model_dump(mode="python")
    settings = payload.get("control_plane")
    if not isinstance(settings, dict):  # pragma: no cover - model invariant
        raise TypeError("Control-plane configuration did not serialize as a mapping")
    settings["host"] = host
    return ControlPlaneConfig.model_validate(payload)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-control-plane")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "migrate"),
        default="serve",
        help="Serve requests (default) or apply database migrations and exit.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/control-plane.yaml"),
        help="Path to the control-plane YAML configuration.",
    )
    parser.add_argument("--host", default=None, help="Override the configured bind host.")
    parser.add_argument("--port", type=int, default=None, help="Override the configured port.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Initialize, report status, and exit without serving.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
