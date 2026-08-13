from __future__ import annotations

import argparse
import asyncio
import getpass
import os
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
    if args.command == "bootstrap-admin":
        if args.once:
            parser.error("bootstrap-admin cannot be combined with --once")
        if not args.username or not args.display_name:
            parser.error("bootstrap-admin requires --username and --display-name")
        _run_bootstrap_admin(runtime, args)
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
        values = await runtime.metrics(allow_internal_authorisation=True)
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


def _run_bootstrap_admin(runtime: ControlPlaneRuntime, args: argparse.Namespace) -> None:
    if not runtime.config.identity.enabled or not runtime.config.identity.local_auth.enabled:
        raise ValueError("bootstrap-admin requires enabled local identity authentication")
    password = _bootstrap_password(args.password_env)

    async def bootstrap() -> tuple[str, str]:
        runtime.database.initialize()
        try:
            organisation, user = await runtime.identity_administration.bootstrap_admin(
                organisation_slug=(
                    args.organisation_slug or runtime.config.identity.default_organisation_slug
                ),
                organisation_name=(
                    args.organisation_name or runtime.config.identity.default_organisation_name
                ),
                username=str(args.username),
                display_name=str(args.display_name),
                email=args.email,
                password=password,
                recovery=args.recovery,
            )
            return organisation.slug, user.username
        finally:
            runtime.database.close()

    organisation_slug, username = asyncio.run(bootstrap())
    mode = "recovered" if args.recovery else "created"
    print(f"Bootstrap administrator {mode}: {username}")
    print(f"Organisation: {organisation_slug}")


def _bootstrap_password(environment_name: str | None) -> str:
    if environment_name is not None:
        password = os.environ.get(environment_name)
        if password is None:
            raise ValueError(f"Password environment variable is not set: {environment_name}")
        if not password:
            raise ValueError("Bootstrap password environment variable must not be empty")
        return password
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Bootstrap passwords do not match")
    if not password:
        raise ValueError("Bootstrap password must not be empty")
    return password


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
    parser = argparse.ArgumentParser(prog="lab-control-plane", allow_abbrev=False)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "migrate", "bootstrap-admin"),
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
    parser.add_argument("--username", help="Bootstrap administrator username.")
    parser.add_argument("--display-name", help="Bootstrap administrator display name.")
    parser.add_argument("--email", help="Optional bootstrap administrator email address.")
    parser.add_argument("--organisation-slug", help="Organisation slug to create or recover.")
    parser.add_argument("--organisation-name", help="Organisation display name.")
    parser.add_argument(
        "--password-env",
        help="Read the password from this environment variable instead of prompting.",
    )
    parser.add_argument(
        "--recovery",
        action="store_true",
        help="Explicitly reset/recover an existing organisation owner.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
