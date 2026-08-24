from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from lab_platform.control_plane.backup import (
    ArtifactBackupAdapter,
    BackupService,
    DatabaseBackupAdapter,
    LocalArtifactBackupAdapter,
    S3ArtifactBackupAdapter,
)
from lab_platform.control_plane.compatibility import (
    AgentVersionInfo,
    UpgradeCheck,
    VersionCompatibilityPolicy,
    build_upgrade_report,
)
from lab_platform.control_plane.config import ControlPlaneConfig, load_control_plane_config
from lab_platform.control_plane.diagnostics import DiagnosticReport, collect_diagnostics
from lab_platform.control_plane.operational_events import StructuredOperationalEventSink
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane.server import ControlPlaneHttpServer
from lab_platform.core import VERSION, build_metadata
from lab_platform.logging import configure_logging, redact_log_text
from lab_platform.persistence.database_management import (
    SchemaStatus,
    inspect_database_schema,
    migrate_database,
    require_current_schema,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_command_shape(parser, args)
    config = _with_command_line_overrides(load_control_plane_config(args.config), args)
    configure_logging(
        level=config.control_plane.log_level,
        json_output=config.profile == "production",
    )
    if args.command == "config":
        return _run_diagnostics(
            config,
            check_database=not args.offline,
            require_production=False,
            output=args.output,
        )
    if args.command == "doctor":
        return _run_diagnostics(
            config,
            check_database=not args.offline,
            require_production=False,
            output=args.output,
        )
    if args.command == "production-check":
        return _run_diagnostics(
            config,
            check_database=not args.offline,
            require_production=True,
            output=args.output,
        )
    if args.command == "db":
        return _run_database_command(config, args.database_command, output=args.output)
    if args.command == "backup":
        return _run_backup_command(config, args)
    if args.command == "upgrade":
        return _run_upgrade_check(config, args)
    if args.command == "migrate":
        if args.once:
            parser.error("migrate cannot be combined with --once")
        return _run_database_migration(
            config,
            allow_unsupported_source=False,
            output=args.output,
        )

    if args.command == "serve" and config.profile == "production":
        report = collect_diagnostics(
            config,
            check_database=True,
            check_filesystem=True,
            require_production=True,
        )
        if not report.ok:
            _print_diagnostics(report, output=args.output)
            print(
                "error: production startup preflight failed; correct the reported issues "
                "before serving requests",
                file=sys.stderr,
            )
            return 1

    runtime = ControlPlaneRuntime(config)
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
    events = StructuredOperationalEventSink()
    events.emit(
        "DATABASE_MIGRATION_STARTED",
        {
            "profile": runtime.config.profile,
            "database_backend": runtime.config.database.url.partition(":")[0],
            "allow_unsupported_source": False,
        },
    )
    runtime.database.initialize()
    try:
        status = inspect_database_schema(runtime.config.database.url)
        events.emit(
            "DATABASE_MIGRATION_COMPLETED",
            {
                "profile": runtime.config.profile,
                "database_backend": status.backend,
                "schema_version": status.current_version,
                "target_schema_version": status.target_version,
            },
        )
        print(f"Lab Control Plane v{VERSION}")
        print("✓ Configuration loaded")
        print("✓ Database migrations applied")
    finally:
        runtime.database.close()


def _run_database_command(
    config: ControlPlaneConfig,
    command: str | None,
    *,
    output: str,
) -> int:
    if command == "migrate":
        return _run_database_migration(config, allow_unsupported_source=False, output=output)
    try:
        status = inspect_database_schema(config.database.url)
        if command == "check":
            require_current_schema(status)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {redact_log_text(str(exc))}", file=sys.stderr)
        return 1
    _print_schema_status(status, output=output)
    return 0


def _run_database_migration(
    config: ControlPlaneConfig,
    *,
    allow_unsupported_source: bool,
    output: str,
) -> int:
    events = StructuredOperationalEventSink()
    events.emit(
        "DATABASE_MIGRATION_STARTED",
        {
            "profile": config.profile,
            "database_backend": config.database.url.partition(":")[0],
            "allow_unsupported_source": allow_unsupported_source,
        },
    )
    try:
        status = migrate_database(
            config.database.url,
            allow_unsupported_source=allow_unsupported_source,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {redact_log_text(str(exc))}", file=sys.stderr)
        return 1
    events.emit(
        "DATABASE_MIGRATION_COMPLETED",
        {
            "profile": config.profile,
            "database_backend": status.backend,
            "schema_version": status.current_version,
            "target_schema_version": status.target_version,
        },
    )
    if output == "json":
        print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Lab Control Plane v{VERSION}")
        print("✓ Configuration loaded")
        print(f"✓ Database migrations applied (schema {status.current_version})")
    return 0


def _backup_service(config: ControlPlaneConfig) -> BackupService:
    artifacts: ArtifactBackupAdapter
    configuration = {
        "profile": config.profile,
        "public_url": config.control_plane.public_url,
        "artifact_backend": config.artifacts.storage_backend,
    }
    if config.artifacts.storage_backend == "local":
        artifacts = LocalArtifactBackupAdapter(config.artifacts.directory)
    else:
        s3 = config.artifacts.s3
        if s3.bucket is None:  # Defensive: validated by ControlPlaneConfig.
            raise ValueError("S3 artifact backup requires artifacts.s3.bucket")
        artifacts = S3ArtifactBackupAdapter(
            bucket=s3.bucket,
            prefix=s3.prefix,
            endpoint_url=s3.endpoint_url,
            region_name=s3.region_name,
        )
        configuration.update(
            {
                "artifact_bucket": s3.bucket,
                "artifact_prefix": s3.prefix,
            }
        )
    return BackupService(
        DatabaseBackupAdapter(config.database.url),
        artifacts,
        configuration=configuration,
        events=StructuredOperationalEventSink(),
    )


def _run_backup_command(config: ControlPlaneConfig, args: argparse.Namespace) -> int:
    service = _backup_service(config)
    try:
        if args.database_command == "create":
            backup = service.create(
                args.destination,
                include_artifacts=not args.exclude_artifacts,
            )
            if args.output == "json":
                verification = service.verify(backup)
                print(json.dumps(verification.as_dict(), indent=2, sort_keys=True))
            else:
                print(f"Backup created:\n{backup}")
            return 0
        assert args.backup_path is not None
        if args.database_command == "verify":
            verification = service.verify(args.backup_path, deep=True)
            if args.output == "json":
                print(json.dumps(verification.as_dict(), indent=2, sort_keys=True))
            else:
                print(f"Backup verified: {verification.path}")
                print(f"Entries verified: {verification.entries_verified}")
                print(f"Bytes verified: {verification.bytes_verified}")
            return 0
        confirmation = "RESTORE" if args.yes else input("Type RESTORE to continue: ").strip()
        restored = service.restore(
            args.backup_path,
            overwrite=args.overwrite,
            confirmation=confirmation,
        )
        if args.output == "json":
            print(
                json.dumps(
                    {
                        "backup": str(restored.backup),
                        "database_restored": restored.database_restored,
                        "artifacts_restored": restored.artifacts_restored,
                        "manifest": restored.manifest.as_dict(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"Backup restored: {restored.backup}")
            print(f"Artifacts restored: {restored.artifacts_restored}")
        return 0
    except (EOFError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {redact_log_text(str(exc))}", file=sys.stderr)
        return 1


def _run_upgrade_check(config: ControlPlaneConfig, args: argparse.Namespace) -> int:
    target_version = args.target_version or VERSION
    policy = _compatibility_policy(config)
    checks: list[UpgradeCheck] = []
    agents: list[AgentVersionInfo] = []
    try:
        schema = inspect_database_schema(config.database.url)
        checks.append(
            UpgradeCheck(
                code="DATABASE_SCHEMA",
                message=schema.message,
                blocking=(not schema.migration_allowed or schema.state == "empty"),
            )
        )
        diagnostic_report = collect_diagnostics(
            config,
            check_database=False,
            check_filesystem=True,
        )
        storage_checks = [
            check
            for check in diagnostic_report.checks
            if check.name in {"artifact_directory", "artifact_storage", "disk_space"}
        ]
        artifact_ready = bool(storage_checks) and all(
            check.status != "error" for check in storage_checks
        )
        checks.append(
            UpgradeCheck(
                code="ARTIFACT_STORAGE",
                message="; ".join(check.message for check in storage_checks)
                or "artifact storage diagnostics did not produce a result",
                blocking=not artifact_ready,
            )
        )
        if args.backup_path is not None:
            verification = _backup_service(config).verify(args.backup_path, deep=True)
            checks.append(
                UpgradeCheck(
                    code="BACKUP_VERIFIED",
                    message=f"backup verified: {verification.path.name}",
                    blocking=False,
                )
            )
        else:
            checks.append(
                UpgradeCheck(
                    code="BACKUP_NOT_VERIFIED",
                    message="no backup was supplied; create and verify one before upgrading",
                    blocking=config.profile == "production",
                )
            )
        if schema.state != "empty" and schema.migration_allowed:
            agents = _database_agent_versions(config)
        report = build_upgrade_report(
            policy,
            agents,
            target_version=target_version,
            checks=checks,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {redact_log_text(str(exc))}", file=sys.stderr)
        return 1
    if args.output == "json":
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Current: {report.current_version}")
        print(f"Target:  {report.target_version}")
        for check in report.checks:
            marker = "BLOCKED" if check.blocking else "OK"
            print(f"{check.code:<28} {marker:<7} {check.message}")
        print(f"Agents checked: {len(report.agents)}")
        print("Upgrade ready." if report.ready else "Upgrade blocked.")
    return 0 if report.ready else 1


def _compatibility_policy(config: ControlPlaneConfig) -> VersionCompatibilityPolicy:
    settings = config.compatibility.agents
    minimum = settings.minimum_supported_version or (
        "0.8.0" if config.profile == "production" else "0.6.0-alpha"
    )
    return VersionCompatibilityPolicy.from_strings(
        minimum_supported_agent=minimum,
        minimum_recommended_agent=settings.minimum_recommended_version,
        target_agent=settings.target_version,
        maximum_supported_agent=settings.maximum_supported_version,
        release_channel=build_metadata().release_channel,
    )


def _database_agent_versions(config: ControlPlaneConfig) -> list[AgentVersionInfo]:
    url = config.database.url
    connection: Any
    if url.startswith("sqlite:///"):
        database_path = Path(unquote(url.removeprefix("sqlite:///"))).expanduser().resolve()
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    else:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - required runtime dependency
            raise RuntimeError("PostgreSQL upgrade checks require psycopg") from exc
        normalized = (
            "postgresql://" + url.removeprefix("postgresql+psycopg://")
            if url.startswith("postgresql+psycopg://")
            else url
        )
        connection = psycopg.connect(normalized, autocommit=True)
    try:
        rows = connection.execute(
            "SELECT id, name, version, protocol_version FROM agents ORDER BY name, id"
        ).fetchall()
        return [
            AgentVersionInfo(
                id=UUID(str(row[0])),
                name=str(row[1]),
                version=str(row[2]),
                protocol_version=str(row[3]),
            )
            for row in rows
        ]
    finally:
        connection.close()


def _run_diagnostics(
    config: ControlPlaneConfig,
    *,
    check_database: bool,
    require_production: bool,
    output: str,
) -> int:
    report = collect_diagnostics(
        config,
        check_database=check_database,
        check_filesystem=True,
        require_production=require_production,
    )
    _print_diagnostics(report, output=output)
    return 0 if report.ok else 1


def _print_diagnostics(report: DiagnosticReport, *, output: str) -> None:
    if output == "json":
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return
    for check in report.checks:
        marker = "OK" if check.status == "ok" else "WARN" if check.status == "warning" else "FAIL"
        print(f"{check.name:<24} {marker:<4} {check.message}")


def _print_schema_status(status: SchemaStatus, *, output: str) -> None:
    if output == "json":
        print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
        return
    print(f"Database backend             {status.backend}")
    print(f"Database schema              {status.current_version}")
    print(f"Target schema                {status.target_version}")
    print(f"Minimum supported schema     {status.minimum_supported_version}")
    print(f"Migration status             {status.state}")
    print(f"Rollback compatibility       {status.rollback_compatibility}")


def _run_bootstrap_admin(runtime: ControlPlaneRuntime, args: argparse.Namespace) -> None:
    if not runtime.config.identity.enabled or not runtime.config.identity.local_auth.enabled:
        raise ValueError("bootstrap-admin requires enabled local identity authentication")
    password = _bootstrap_password(args.password_env)

    async def bootstrap() -> tuple[str, str]:
        if runtime.config.profile == "production":
            require_current_schema(inspect_database_schema(runtime.config.database.url))
        runtime.database.initialize()
        try:
            await runtime.identity_repository.ensure_default_organisation(
                slug=runtime.config.identity.default_organisation_slug,
                name=runtime.config.identity.default_organisation_name,
            )
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

    return _with_config_overrides(config, {"control_plane": {"host": host}} if host else {})


def _with_command_line_overrides(
    config: ControlPlaneConfig,
    args: argparse.Namespace,
) -> ControlPlaneConfig:
    control_plane: dict[str, object] = {}
    if args.host is not None:
        control_plane["host"] = args.host
    if args.port is not None:
        control_plane["port"] = args.port
    if args.public_url is not None:
        control_plane["public_url"] = args.public_url
    if args.log_level is not None:
        control_plane["log_level"] = args.log_level
    overrides: dict[str, object] = {}
    if control_plane:
        overrides["control_plane"] = control_plane
    if args.public_url is not None:
        overrides["web"] = {"public_url": args.public_url}
    if args.profile is not None:
        overrides["profile"] = args.profile
    if args.artifact_dir is not None:
        overrides["artifacts"] = {"directory": args.artifact_dir}
    return _with_config_overrides(config, overrides)


def _with_config_overrides(
    config: ControlPlaneConfig,
    overrides: Mapping[str, object],
) -> ControlPlaneConfig:
    if not overrides:
        return config
    payload = config.model_dump(mode="python")
    if not isinstance(payload.get("control_plane"), dict):  # pragma: no cover - model invariant
        raise TypeError("Control-plane configuration did not serialize as a mapping")
    return ControlPlaneConfig.model_validate(_deep_merge(payload, overrides))


def _deep_merge(base: Mapping[str, object], update: Mapping[str, object]) -> dict[str, object]:
    merged: dict[str, object] = dict(base)
    for key, value in update.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _validate_command_shape(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "db" and args.database_command not in {"status", "check", "migrate"}:
        parser.error("db requires one of: status, check, migrate")
    if args.command == "config" and args.database_command != "validate":
        parser.error("config requires: validate")
    if args.command == "backup" and args.database_command not in {"create", "verify", "restore"}:
        parser.error("backup requires one of: create, verify, restore")
    if args.command == "upgrade" and args.database_command != "check":
        parser.error("upgrade requires: check")
    if (
        args.command not in {"db", "config", "backup", "upgrade"}
        and args.database_command is not None
    ):
        parser.error(f"{args.command} does not accept a subcommand")
    if args.command == "backup" and args.database_command in {"verify", "restore"}:
        if args.backup_path is None:
            parser.error(f"backup {args.database_command} requires a backup path")
    elif args.command != "upgrade" and args.backup_path is not None:
        parser.error(f"{args.command} does not accept a backup path")
    if args.command not in {"serve", "doctor", "production-check", "config"} and args.offline:
        parser.error("--offline is only valid with config validate, doctor, or production-check")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-control-plane", allow_abbrev=False)
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "serve",
            "migrate",
            "bootstrap-admin",
            "config",
            "doctor",
            "production-check",
            "db",
            "backup",
            "upgrade",
        ),
        default="serve",
        help="Serve requests (default) or apply database migrations and exit.",
    )
    parser.add_argument(
        "database_command",
        nargs="?",
        help="Database action, or `validate` after the config command.",
    )
    parser.add_argument("backup_path", nargs="?", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/control-plane.yaml"),
        help="Path to the control-plane YAML configuration.",
    )
    parser.add_argument("--host", default=None, help="Override the configured bind host.")
    parser.add_argument("--port", type=int, default=None, help="Override the configured port.")
    parser.add_argument(
        "--profile",
        choices=("development", "test", "production"),
        default=None,
        help="Override the configured environment profile.",
    )
    parser.add_argument("--public-url", default=None, help="Override the configured public URL.")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=None,
    )
    parser.add_argument("--artifact-dir", type=Path, default=None)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("backups"),
        help="Backup output directory or explicit .tar.zst/.tar path.",
    )
    parser.add_argument(
        "--exclude-artifacts",
        action="store_true",
        help="Create a metadata/database-only backup.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow an explicitly confirmed restore to replace non-empty targets.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Supply the exact RESTORE confirmation non-interactively.",
    )
    parser.add_argument("--target-version", default=None, help="Upgrade target semantic version.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live database probe during configuration diagnostics.",
    )
    parser.add_argument("--output", choices=("text", "json"), default="text")
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
