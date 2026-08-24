from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from lab_platform.control_plane.config import (
    ApiRateLimitSettings,
    ControlPlaneConfig,
    ResourceLimitSettings,
    resolve_secret_environment,
)
from lab_platform.core.artifact_storage import S3CompatibleArtifactStorage
from lab_platform.logging import redact_log_text
from lab_platform.persistence.database_management import inspect_database_schema

DiagnosticStatus = Literal["ok", "warning", "error"]

_MINIMUM_SECRET_LENGTH = 32
_MINIMUM_SECRET_UNIQUE_CHARACTERS = 12
_LOW_DISK_BYTES = 1024 * 1024 * 1024
_LOW_DISK_FRACTION = 0.10


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: DiagnosticStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.status == "warning" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "warnings": self.warnings,
            "checks": [check.as_dict() for check in self.checks],
        }


def collect_diagnostics(
    config: ControlPlaneConfig,
    *,
    environ: Mapping[str, str] | None = None,
    check_database: bool = True,
    check_filesystem: bool = True,
    require_production: bool = False,
) -> DiagnosticReport:
    """Run deterministic deployment checks without changing database or application state."""

    source = os.environ if environ is None else environ
    production = require_production or config.profile == "production"
    checks: list[DiagnosticCheck] = [
        _check(
            "configuration",
            "ok",
            f"strict configuration loaded for the {config.profile} profile",
        )
    ]
    checks.extend(
        _profile_checks(config, production=production, require_production=require_production)
    )
    checks.extend(
        _transport_checks(config, production=production, check_filesystem=check_filesystem)
    )
    checks.extend(_secret_checks(config, source, production=production))
    checks.extend(_limit_checks(config, production=production))
    checks.extend(_artifact_checks(config, production=production, enabled=check_filesystem))
    if check_database:
        checks.extend(_database_checks(config))
    return DiagnosticReport(tuple(checks))


def _profile_checks(
    config: ControlPlaneConfig,
    *,
    production: bool,
    require_production: bool,
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    if require_production and config.profile != "production":
        checks.append(_check("profile", "error", "production-check requires profile=production"))
    else:
        checks.append(_check("profile", "ok", f"profile is {config.profile}"))

    public_scheme = urlsplit(config.control_plane.public_url).scheme
    checks.append(
        _check(
            "public_url",
            "ok" if not production or public_scheme == "https" else "error",
            (
                f"public URL uses {public_scheme.upper()}"
                if not production or public_scheme == "https"
                else "production public URL must use HTTPS"
            ),
        )
    )
    postgresql = config.database.url.startswith("postgresql://")
    checks.append(
        _check(
            "database_backend",
            "ok" if not production or postgresql else "error",
            (
                "PostgreSQL is configured"
                if postgresql
                else "production deployments require PostgreSQL"
                if production
                else "SQLite is configured for local use"
            ),
        )
    )
    pool_ok = not production or config.database.pool_size > 1
    checks.append(
        _check(
            "database_pool",
            "ok" if pool_ok else "error",
            (
                f"database pool size is {config.database.pool_size}"
                if pool_ok
                else "production database.pool_size must be greater than 1"
            ),
        )
    )
    if postgresql:
        query = parse_qs(urlsplit(config.database.url).query)
        sslmode = query.get("sslmode", [""])[-1].casefold()
        secure = sslmode in {"require", "verify-ca", "verify-full"}
        checks.append(
            _check(
                "database_tls",
                "ok" if secure else "warning",
                (
                    f"PostgreSQL sslmode={sslmode}"
                    if secure
                    else "PostgreSQL TLS is not explicitly required; keep the database private"
                ),
            )
        )
    return checks


def _transport_checks(
    config: ControlPlaneConfig,
    *,
    production: bool,
    check_filesystem: bool,
) -> list[DiagnosticCheck]:
    if config.proxy.enabled:
        return [
            _check(
                "reverse_proxy",
                "ok",
                "TLS termination trusts only " + ", ".join(config.proxy.trusted_networks),
            )
        ]

    certificate = config.control_plane.tls_certificate_path
    private_key = config.control_plane.tls_private_key_path
    if certificate is None or private_key is None:
        return [
            _check(
                "tls",
                "error" if production else "warning",
                "direct TLS files are not configured",
            )
        ]
    if not check_filesystem:
        return [_check("tls", "ok", "direct TLS certificate and private-key paths configured")]

    missing = [str(path) for path in (certificate, private_key) if not path.is_file()]
    unreadable = [
        str(path)
        for path in (certificate, private_key)
        if path.exists() and not os.access(path, os.R_OK)
    ]
    if missing:
        return [_check("tls", "error", "missing TLS file(s): " + ", ".join(missing))]
    if unreadable:
        return [_check("tls", "error", "unreadable TLS file(s): " + ", ".join(unreadable))]
    return [_check("tls", "ok", "direct TLS files are readable")]


def _secret_checks(
    config: ControlPlaneConfig,
    environ: Mapping[str, str],
    *,
    production: bool,
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    secret = config.security.secret_key
    if production:
        if secret is None:
            checks.append(_check("secret_key", "error", "LAB_SECRET_KEY is required in production"))
        elif _strong_secret(secret.get_secret_value()):
            checks.append(_check("secret_key", "ok", "application secret passes strength checks"))
        else:
            checks.append(
                _check(
                    "secret_key",
                    "error",
                    "application secret must be at least 32 characters with sufficient diversity",
                )
            )
    elif secret is not None:
        checks.append(
            _check(
                "secret_key",
                "ok" if _strong_secret(secret.get_secret_value()) else "warning",
                "application secret configured",
            )
        )

    oidc = config.identity.oidc
    if oidc.enabled and oidc.client_secret_env is not None:
        try:
            oidc_secret = resolve_secret_environment(oidc.client_secret_env, environ)
        except ValueError as exc:
            checks.append(_check("oidc_secret", "error", str(exc)))
        else:
            checks.append(
                _check(
                    "oidc_secret",
                    "ok" if oidc_secret else "error",
                    (
                        f"OIDC secret is available via {oidc.client_secret_env}"
                        if oidc_secret
                        else f"OIDC secret is missing from {oidc.client_secret_env}"
                    ),
                )
            )
    return checks


def _limit_checks(config: ControlPlaneConfig, *, production: bool) -> list[DiagnosticCheck]:
    resource_fields = _missing_resource_limits(config.resource_limits)
    rate_fields = _missing_rate_limits(config.security.api_rate_limits)
    return [
        _check(
            "resource_limits",
            "error"
            if production and resource_fields
            else "ok"
            if not resource_fields
            else "warning",
            (
                "all bounded resource limits are configured"
                if not resource_fields
                else "unbounded resource limits: " + ", ".join(resource_fields)
            ),
        ),
        _check(
            "api_rate_limits",
            "error" if production and rate_fields else "ok" if not rate_fields else "warning",
            (
                "all public API category limits are configured"
                if not rate_fields
                else "unconfigured API category limits: " + ", ".join(rate_fields)
            ),
        ),
    ]


def _artifact_checks(
    config: ControlPlaneConfig,
    *,
    production: bool,
    enabled: bool,
) -> list[DiagnosticCheck]:
    if config.artifacts.storage_backend == "s3":
        s3 = config.artifacts.s3
        if s3.bucket is None:
            return [_check("artifact_storage", "error", "S3 bucket is not configured")]
        if not enabled:
            return [
                _check(
                    "artifact_storage",
                    "ok",
                    f"S3-compatible storage is configured for bucket {s3.bucket}",
                )
            ]
        storage = S3CompatibleArtifactStorage(
            bucket=s3.bucket,
            prefix=s3.prefix,
            endpoint_url=s3.endpoint_url,
            region_name=s3.region_name,
        )
        try:
            asyncio.run(storage.exists("health/doctor-probe"))
        except Exception as exc:
            return [
                _check(
                    "artifact_storage",
                    "error",
                    "S3-compatible artifact storage is unavailable: " + redact_log_text(str(exc)),
                )
            ]
        return [
            _check(
                "artifact_storage",
                "ok",
                f"S3-compatible artifact storage is reachable in bucket {s3.bucket}",
            )
        ]
    directory = config.artifacts.directory
    checks: list[DiagnosticCheck] = []
    if production and not directory.is_absolute():
        checks.append(
            _check("artifact_directory", "error", "production artifact directory must be absolute")
        )
        return checks
    if not enabled:
        checks.append(_check("artifact_directory", "ok", f"artifact directory is {directory}"))
        return checks
    if not directory.exists():
        checks.append(
            _check("artifact_directory", "error", f"artifact directory is missing: {directory}")
        )
        return checks
    if not directory.is_dir():
        checks.append(
            _check("artifact_directory", "error", f"artifact path is not a directory: {directory}")
        )
        return checks
    try:
        with tempfile.NamedTemporaryFile(prefix=".lab-platform-write-check-", dir=directory):
            pass
    except OSError as exc:
        checks.append(
            _check(
                "artifact_directory",
                "error",
                f"artifact directory is not writable: {exc.strerror or exc}",
            )
        )
        return checks
    checks.append(
        _check("artifact_directory", "ok", f"artifact directory is writable: {directory}")
    )
    usage = shutil.disk_usage(directory)
    low = usage.free < _LOW_DISK_BYTES or usage.free / max(usage.total, 1) < _LOW_DISK_FRACTION
    checks.append(
        _check(
            "disk_space",
            "warning" if low else "ok",
            f"artifact filesystem has {usage.free} bytes free",
        )
    )
    return checks


def _database_checks(config: ControlPlaneConfig) -> list[DiagnosticCheck]:
    try:
        status = inspect_database_schema(config.database.url)
    except Exception as exc:
        message = redact_log_text(str(exc))
        return [_check("database_connection", "error", message or type(exc).__name__)]
    return [
        _check("database_connection", "ok", f"{status.backend} database connected"),
        _check("database_schema", "ok" if status.ready else "error", status.message),
    ]


def _missing_resource_limits(settings: ResourceLimitSettings) -> list[str]:
    payload = settings.model_dump()
    return [name for name, value in payload.items() if value is None]


def _missing_rate_limits(settings: ApiRateLimitSettings) -> list[str]:
    payload = settings.model_dump()
    return [name for name, value in payload.items() if value is None]


def _strong_secret(value: str) -> bool:
    normalized = value.strip()
    if len(normalized) < _MINIMUM_SECRET_LENGTH:
        return False
    if len(set(normalized)) < _MINIMUM_SECRET_UNIQUE_CHARACTERS:
        return False
    return normalized.casefold() not in {
        "change-me-change-me-change-me-change-me",
        "replace-me-replace-me-replace-me-replace-me",
    }


def _check(name: str, status: DiagnosticStatus, message: str) -> DiagnosticCheck:
    return DiagnosticCheck(name=name, status=status, message=message)


__all__ = [
    "DiagnosticCheck",
    "DiagnosticReport",
    "DiagnosticStatus",
    "collect_diagnostics",
]
