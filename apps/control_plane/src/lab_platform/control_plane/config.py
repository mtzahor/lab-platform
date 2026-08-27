from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import yaml
from lab_platform.config import CONFIG_VERSION
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

_ORGANISATION_SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$"
_ENVIRONMENT_VARIABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_APPLICATION_VERSION_PATTERN = (
    r"^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-?(?:alpha|a|beta|b|preview|pre|rc|nightly|dev)(?:[.-]?[0-9]+)?)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


class ControlPlaneConfigModel(BaseModel):
    """Strict, immutable base for control-plane configuration."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ControlPlaneSettings(ControlPlaneConfigModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=8443, ge=1, le=65_535)
    public_url: str = "https://127.0.0.1:8443"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_request_body_size_mb: int = Field(default=1, ge=1, le=1024)
    tls_certificate_path: Path | None = None
    tls_private_key_path: Path | None = None

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        parsed = _parse_public_url(value)
        return urlunsplit(parsed).rstrip("/")

    @field_validator("tls_certificate_path", "tls_private_key_path", mode="before")
    @classmethod
    def parse_tls_path(cls, value: object) -> object:
        # Pydantic's strict Path type intentionally rejects YAML strings. Convert
        # only path-like configuration values before strict validation.
        if isinstance(value, str):
            return Path(value)
        return value

    @model_validator(mode="after")
    def require_complete_tls_pair(self) -> ControlPlaneSettings:
        certificate_configured = self.tls_certificate_path is not None
        key_configured = self.tls_private_key_path is not None
        if certificate_configured != key_configured:
            raise ValueError(
                "control_plane TLS certificate and private-key paths must be configured together"
            )
        return self


class ControlPlaneDatabaseSettings(ControlPlaneConfigModel):
    url: str = Field(
        default="sqlite:///./.lab-control-plane/control-plane.db",
        repr=False,
    )
    pool_size: int = Field(default=1, ge=1, le=100)

    @field_validator("url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if value.startswith("sqlite:///"):
            if not value.removeprefix("sqlite:///"):
                raise ValueError("SQLite database URL must include a database path")
            return value

        parsed = urlsplit(value)
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError(
                "control-plane database URL must use sqlite:///, postgresql://, "
                "or postgresql+psycopg://"
            )
        if parsed.path in {"", "/"}:
            raise ValueError("PostgreSQL database URL must include a database name")
        if parsed.fragment:
            raise ValueError("PostgreSQL database URL must not contain a fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("PostgreSQL database URL contains an invalid port") from exc

        # The runtime uses Psycopg directly rather than SQLAlchemy. Accept the familiar
        # SQLAlchemy-style spelling at the configuration boundary, then keep one canonical
        # libpq/Psycopg DSN internally.
        if parsed.scheme == "postgresql+psycopg":
            return "postgresql://" + value.removeprefix("postgresql+psycopg://")
        return value


class AgentGatewaySettings(ControlPlaneConfigModel):
    heartbeat_interval_seconds: int = Field(default=15, ge=1, le=3600)
    heartbeat_timeout_seconds: int = Field(default=45, ge=2, le=86_400)
    offline_timeout_seconds: int = Field(default=90, ge=3, le=604_800)
    handshake_timeout_seconds: int = Field(default=10, ge=1, le=300)
    maximum_message_size_mb: int = Field(default=2, ge=1, le=64)
    outgoing_queue_size: int = Field(default=256, ge=1, le=65_536)
    sequence_window_size: int = Field(default=4096, ge=1, le=1_000_000)
    monitor_interval_seconds: float = Field(default=1.0, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_heartbeat_ordering(self) -> AgentGatewaySettings:
        if self.heartbeat_interval_seconds >= self.heartbeat_timeout_seconds:
            raise ValueError("Agent heartbeat interval must be shorter than heartbeat timeout")
        if self.heartbeat_timeout_seconds >= self.offline_timeout_seconds:
            raise ValueError("Agent heartbeat timeout must be shorter than offline timeout")
        return self


class DistributedSettings(ControlPlaneConfigModel):
    maximum_clock_skew_seconds: int = Field(default=30, ge=0, le=3600)
    offline_reservation_grace_seconds: int = Field(default=300, ge=0, le=86_400)
    operation_reconciliation_timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=604_800,
    )
    queue_commands_for_offline_agents: bool = False
    scheduled_protection_window_seconds: int = Field(default=300, ge=0, le=86_400)


class S3ArtifactStorageSettings(ControlPlaneConfigModel):
    bucket: str | None = Field(default=None, min_length=1, max_length=255)
    prefix: str = Field(default="lab-platform", max_length=512)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2048)
    region_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        normalized = value.strip().strip("/")
        if "\\" in normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("artifacts.s3.prefix must be a safe object-key prefix")
        return normalized

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = _parse_http_url(
            value,
            field_name="artifacts.s3.endpoint_url",
            allow_path=True,
        )
        return urlunsplit(parsed).rstrip("/")


class ControlPlaneArtifactSettings(ControlPlaneConfigModel):
    storage_backend: Literal["local", "s3"] = "local"
    directory: Path = Path("./.lab-control-plane/artifacts")
    s3: S3ArtifactStorageSettings = Field(default_factory=S3ArtifactStorageSettings)
    max_upload_size_mb: int = Field(
        default=500,
        validation_alias=AliasChoices("maximum_upload_size_mb", "max_upload_size_mb"),
        ge=1,
        le=4096,
    )
    transfer_token_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    finalization_timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("directory", mode="before")
    @classmethod
    def parse_directory(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value)
        return value

    @model_validator(mode="after")
    def require_selected_backend_configuration(self) -> ControlPlaneArtifactSettings:
        if self.storage_backend == "s3" and self.s3.bucket is None:
            raise ValueError("S3 artifact storage requires artifacts.s3.bucket")
        return self


class ArtifactRetentionSettings(ControlPlaneConfigModel):
    enabled: bool = True
    default_days: int | None = Field(default=30, ge=1, le=36_500)
    failed_workflow_days: int | None = Field(default=90, ge=1, le=36_500)
    firmware_days: int | None = Field(default=180, ge=1, le=36_500)
    serial_log_days: int | None = Field(default=30, ge=1, le=36_500)
    junit_report_days: int | None = Field(default=90, ge=1, le=36_500)
    workflow_log_days: int | None = Field(default=30, ge=1, le=36_500)
    diagnostic_bundle_days: int | None = Field(default=14, ge=1, le=36_500)
    worker_interval_seconds: int = Field(default=3600, ge=10, le=86_400)
    batch_size: int = Field(default=100, ge=1, le=10_000)


class RetentionSettings(ControlPlaneConfigModel):
    artifacts: ArtifactRetentionSettings = Field(default_factory=ArtifactRetentionSettings)


class AgentCompatibilitySettings(ControlPlaneConfigModel):
    minimum_supported_version: str | None = Field(
        default=None,
        pattern=_APPLICATION_VERSION_PATTERN,
    )
    minimum_recommended_version: str = Field(
        default="0.8.0",
        pattern=_APPLICATION_VERSION_PATTERN,
    )
    target_version: str = Field(
        default="0.9.0-beta",
        pattern=_APPLICATION_VERSION_PATTERN,
    )
    maximum_supported_version: str = Field(
        default="0.9.999",
        pattern=_APPLICATION_VERSION_PATTERN,
    )


class CompatibilitySettings(ControlPlaneConfigModel):
    agents: AgentCompatibilitySettings = Field(default_factory=AgentCompatibilitySettings)


class LocalAuthSettings(ControlPlaneConfigModel):
    enabled: bool = True
    minimum_password_length: int = Field(default=12, ge=12, le=1024)


class OidcSettings(ControlPlaneConfigModel):
    enabled: bool = False
    issuer_url: str | None = Field(default=None, min_length=1, max_length=2048)
    client_id: str | None = Field(default=None, min_length=1, max_length=500)
    client_secret_env: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=_ENVIRONMENT_VARIABLE_PATTERN,
    )
    scopes: tuple[str, ...] = Field(
        default=("openid", "profile", "email"),
        min_length=1,
        max_length=32,
    )
    username_claim: str = Field(
        default="preferred_username",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    )
    transaction_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    clock_skew_seconds: int = Field(default=60, ge=0, le=300)

    @field_validator("issuer_url")
    @classmethod
    def validate_issuer_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("identity.oidc.issuer_url must be an absolute HTTP or HTTPS URL")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("identity.oidc.issuer_url must use HTTPS except on loopback")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("identity.oidc.issuer_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("identity.oidc.issuer_url must not contain a query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("identity.oidc.issuer_url contains an invalid port") from exc
        return value

    @field_validator("scopes", mode="before")
    @classmethod
    def parse_scopes(cls, value: object) -> object:
        # YAML represents sequences as lists while the immutable settings model
        # exposes scopes as a tuple.
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not scope or any(character.isspace() for character in scope) for scope in value):
            raise ValueError("identity.oidc.scopes must contain non-empty scope names")
        if len(set(value)) != len(value):
            raise ValueError("identity.oidc.scopes must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_enabled_configuration(self) -> OidcSettings:
        if not self.enabled:
            return self
        missing = [
            field
            for field in ("issuer_url", "client_id", "client_secret_env")
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError("enabled OIDC requires issuer_url, client_id, and client_secret_env")
        if "openid" not in self.scopes:
            raise ValueError("enabled OIDC requires the openid scope")
        return self


class SessionSettings(ControlPlaneConfigModel):
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    session_hours: int = Field(default=12, ge=1, le=8760)
    maximum_session_days: int = Field(default=7, ge=1, le=3650)

    @model_validator(mode="after")
    def validate_lifetime_ordering(self) -> SessionSettings:
        session_minutes = self.session_hours * 60
        maximum_session_minutes = self.maximum_session_days * 24 * 60
        if self.access_token_minutes >= session_minutes:
            raise ValueError("access-token lifetime must be shorter than session lifetime")
        if session_minutes > maximum_session_minutes:
            raise ValueError("session lifetime must not exceed maximum session lifetime")
        return self


class IdentitySettings(ControlPlaneConfigModel):
    enabled: bool = True
    default_organisation_slug: str = Field(
        default="default",
        min_length=1,
        max_length=100,
        pattern=_ORGANISATION_SLUG_PATTERN,
    )
    default_organisation_name: str = Field(
        default="Default Organisation",
        min_length=1,
        max_length=200,
    )
    local_auth: LocalAuthSettings = Field(default_factory=LocalAuthSettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    sessions: SessionSettings = Field(default_factory=SessionSettings)


class AuthorisationSettings(ControlPlaneConfigModel):
    default_bench_visibility: Literal["private", "organisation", "restricted"] = "organisation"
    hide_unauthorised_resources: bool = True
    legacy_token_compatibility_enabled: bool = True


class AuditSettings(ControlPlaneConfigModel):
    enabled: bool = True
    retention_days: int = Field(default=90, ge=1, le=3650)


class LoginRateLimitSettings(ControlPlaneConfigModel):
    attempts: int = Field(default=10, ge=1, le=10_000)
    window_minutes: int = Field(default=15, ge=1, le=10_080)


class ApiRateLimitPolicy(ControlPlaneConfigModel):
    requests: int = Field(ge=1, le=1_000_000)
    window_seconds: int = Field(ge=1, le=86_400)


class ApiRateLimitSettings(ControlPlaneConfigModel):
    """Opt-in public-edge policies; ``None`` preserves the existing unthrottled behavior."""

    artifact_upload: ApiRateLimitPolicy | None = None
    workflow_creation: ApiRateLimitPolicy | None = None
    agent_enrollment: ApiRateLimitPolicy | None = None
    expensive_search: ApiRateLimitPolicy | None = None


class SecuritySettings(ControlPlaneConfigModel):
    secret_key: SecretStr | None = Field(default=None, repr=False)
    login_rate_limit: LoginRateLimitSettings = Field(default_factory=LoginRateLimitSettings)
    api_rate_limits: ApiRateLimitSettings = Field(default_factory=ApiRateLimitSettings)


class ResourceLimitSettings(ControlPlaneConfigModel):
    """Deployment limits that are opt-in for compatibility and mandatory in production checks."""

    maximum_concurrent_workflows: int | None = Field(default=None, ge=1, le=100_000)
    maximum_active_ci_sessions: int | None = Field(default=None, ge=1, le=100_000)
    maximum_sse_streams: int | None = Field(default=None, ge=1, le=1_000_000)
    maximum_artifact_size_mb: int | None = Field(default=None, ge=1, le=4096)
    maximum_log_artifact_size_mb: int | None = Field(default=None, ge=1, le=4096)
    maximum_reservation_duration_minutes: int | None = Field(
        default=None,
        ge=1,
        le=10_080,
    )

    @model_validator(mode="after")
    def require_ordered_artifact_limits(self) -> ResourceLimitSettings:
        if (
            self.maximum_log_artifact_size_mb is not None
            and self.maximum_artifact_size_mb is not None
            and self.maximum_log_artifact_size_mb > self.maximum_artifact_size_mb
        ):
            raise ValueError("maximum log artifact size must not exceed maximum artifact size")
        return self


class WebLiveUpdatesSettings(ControlPlaneConfigModel):
    sse_enabled: bool = True
    polling_fallback_seconds: int = Field(default=5, ge=1, le=300)


class WebUploadsSettings(ControlPlaneConfigModel):
    maximum_firmware_size_mb: int = Field(default=100, ge=1, le=4096)


class WebFeaturesSettings(ControlPlaneConfigModel):
    identity_admin: bool = True
    audit_viewer: bool = True
    ci_sessions: bool = True


class WebBrandingSettings(ControlPlaneConfigModel):
    product_name: str = Field(default="Lab Platform", min_length=1, max_length=100)


class WebSettings(ControlPlaneConfigModel):
    """Public dashboard and integrated static-hosting configuration."""

    enabled: bool = False
    public_url: str | None = None
    api_base_url: str = "/api/v1"
    live_updates: WebLiveUpdatesSettings = Field(default_factory=WebLiveUpdatesSettings)
    uploads: WebUploadsSettings = Field(default_factory=WebUploadsSettings)
    features: WebFeaturesSettings = Field(default_factory=WebFeaturesSettings)
    branding: WebBrandingSettings = Field(default_factory=WebBrandingSettings)

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = _parse_http_url(value, field_name="web.public_url", allow_path=False)
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
            raise ValueError("web.public_url must use HTTPS except on loopback")
        return urlunsplit(parsed).rstrip("/")

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        if value.startswith("/"):
            parsed = urlsplit(value)
            if (
                value.startswith("//")
                or parsed.scheme
                or parsed.netloc
                or parsed.query
                or parsed.fragment
                or any(part in {".", ".."} for part in parsed.path.split("/"))
            ):
                raise ValueError(
                    "web.api_base_url must be a root-relative path without a query or fragment"
                )
            normalized = parsed.path.rstrip("/")
            if not normalized:
                raise ValueError("web.api_base_url must not resolve to the site root")
            return normalized

        parsed = _parse_http_url(value, field_name="web.api_base_url", allow_path=True)
        if parsed.path in {"", "/"}:
            raise ValueError("web.api_base_url must include an API path")
        return urlunsplit(parsed._replace(path=parsed.path.rstrip("/")))


class ControlPlaneDevelopmentSettings(ControlPlaneConfigModel):
    enabled: bool = False
    auto_login_user: str | None = Field(default=None, min_length=1, max_length=200)
    allow_insecure_agent_transport: bool = False
    allow_tls_termination_proxy: bool = False


class ReverseProxySettings(ControlPlaneConfigModel):
    """Explicit trust boundary for a TLS-terminating reverse proxy."""

    enabled: bool = False
    trusted_networks: tuple[str, ...] = ()

    @field_validator("trusted_networks", mode="before")
    @classmethod
    def parse_trusted_networks(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("trusted_networks")
    @classmethod
    def validate_trusted_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"proxy.trusted_networks contains an invalid network: {item}"
                ) from exc
            if network.prefixlen == 0:
                raise ValueError("proxy.trusted_networks must not trust every address")
            rendered = str(network)
            if rendered not in normalized:
                normalized.append(rendered)
        return tuple(normalized)

    @model_validator(mode="after")
    def require_explicit_trust(self) -> ReverseProxySettings:
        if self.enabled and not self.trusted_networks:
            raise ValueError("enabled reverse-proxy mode requires proxy.trusted_networks")
        return self


class ControlPlaneConfig(ControlPlaneConfigModel):
    config_version: Literal[1] = CONFIG_VERSION
    profile: Literal["development", "test", "production"] = "development"
    control_plane: ControlPlaneSettings = Field(default_factory=ControlPlaneSettings)
    database: ControlPlaneDatabaseSettings = Field(default_factory=ControlPlaneDatabaseSettings)
    agent_gateway: AgentGatewaySettings = Field(default_factory=AgentGatewaySettings)
    distributed: DistributedSettings = Field(default_factory=DistributedSettings)
    artifacts: ControlPlaneArtifactSettings = Field(default_factory=ControlPlaneArtifactSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    compatibility: CompatibilitySettings = Field(default_factory=CompatibilitySettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    authorisation: AuthorisationSettings = Field(default_factory=AuthorisationSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    resource_limits: ResourceLimitSettings = Field(default_factory=ResourceLimitSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    proxy: ReverseProxySettings = Field(default_factory=ReverseProxySettings)
    development: ControlPlaneDevelopmentSettings = Field(
        default_factory=ControlPlaneDevelopmentSettings
    )

    @model_validator(mode="after")
    def require_safe_agent_transport(self) -> ControlPlaneConfig:
        public_url = urlsplit(self.control_plane.public_url)
        tls_configured = self.control_plane.tls_certificate_path is not None
        if self.proxy.enabled:
            if public_url.scheme != "https":
                raise ValueError("reverse-proxy mode requires an HTTPS public URL")
            if tls_configured:
                raise ValueError(
                    "reverse-proxy TLS termination cannot be combined with direct control-plane TLS"
                )
            return self
        if public_url.scheme == "https":
            if tls_configured:
                return self
            if self.development.allow_tls_termination_proxy:
                if _is_loopback_host(self.control_plane.host):
                    return self
                raise ValueError(
                    "TLS-termination proxy mode requires a loopback control-plane bind host"
                )
            raise ValueError(
                "An HTTPS/WSS public URL requires control-plane TLS certificate/private-key "
                "paths, or explicit loopback TLS-termination proxy mode"
            )

        if tls_configured:
            raise ValueError(
                "Control-plane TLS certificate/private-key paths require an HTTPS public URL"
            )

        if not self.development.allow_insecure_agent_transport:
            raise ValueError(
                "Agent transport must use HTTPS/WSS unless insecure local development is enabled"
            )
        if not (
            _is_loopback_host(self.control_plane.host)
            and public_url.hostname is not None
            and _is_loopback_host(public_url.hostname)
        ):
            raise ValueError(
                "Insecure HTTP/WS Agent transport is allowed only on loopback interfaces"
            )
        return self

    @model_validator(mode="after")
    def require_safe_development_auto_login(self) -> ControlPlaneConfig:
        if self.development.auto_login_user is None:
            return self
        if not self.development.enabled:
            raise ValueError("development auto-login requires development.enabled")
        public_url = urlsplit(self.control_plane.public_url)
        if not (
            _is_loopback_host(self.control_plane.host)
            and public_url.hostname is not None
            and _is_loopback_host(public_url.hostname)
        ):
            raise ValueError(
                "development auto-login is allowed only with loopback bind and public hosts"
            )
        return self

    @model_validator(mode="after")
    def require_identity_for_oidc(self) -> ControlPlaneConfig:
        if self.identity.oidc.enabled and not self.identity.enabled:
            raise ValueError("OIDC authentication requires identity.enabled")
        return self

    @model_validator(mode="after")
    def require_safe_web_configuration(self) -> ControlPlaneConfig:
        if not self.web.enabled:
            return self
        if self.web.public_url is None:
            raise ValueError("enabled web dashboard requires web.public_url")
        if not self.identity.enabled:
            raise ValueError("enabled web dashboard requires identity.enabled")
        if self.web.features.audit_viewer and not self.audit.enabled:
            raise ValueError("web.features.audit_viewer requires audit.enabled")
        maximum_artifact_size_mb = (
            self.resource_limits.maximum_artifact_size_mb or self.artifacts.max_upload_size_mb
        )
        if self.web.uploads.maximum_firmware_size_mb > maximum_artifact_size_mb:
            raise ValueError(
                "web firmware upload limit must not exceed the control-plane artifact limit"
            )
        return self

    @model_validator(mode="after")
    def require_consistent_resource_limits(self) -> ControlPlaneConfig:
        maximum_artifact_size_mb = self.resource_limits.maximum_artifact_size_mb
        if (
            maximum_artifact_size_mb is not None
            and maximum_artifact_size_mb > self.artifacts.max_upload_size_mb
        ):
            raise ValueError(
                "resource maximum artifact size must not exceed artifacts.max_upload_size_mb"
            )
        return self

    @model_validator(mode="after")
    def require_production_profile_baseline(self) -> ControlPlaneConfig:
        if self.profile != "production":
            return self
        if not self.database.url.startswith("postgresql://"):
            raise ValueError("production profile requires PostgreSQL")
        if self.development.enabled or self.development.auto_login_user is not None:
            raise ValueError("production profile must disable development mode and auto-login")
        if self.development.allow_insecure_agent_transport:
            raise ValueError("production profile must disable insecure Agent transport")
        if self.development.allow_tls_termination_proxy:
            raise ValueError(
                "production profile must use proxy.enabled instead of the legacy development proxy"
            )
        if urlsplit(self.control_plane.public_url).scheme != "https":
            raise ValueError("production profile requires an HTTPS public URL")
        if self.web.enabled and self.web.public_url != self.control_plane.public_url:
            raise ValueError(
                "production integrated web and control-plane public URLs must be identical"
            )
        return self

    @property
    def agent_gateway_url(self) -> str:
        """Return the public WebSocket endpoint derived from the REST public URL."""

        parsed = urlsplit(self.control_plane.public_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/api/v1/agent-gateway", "", ""))


def load_control_plane_config(
    path: str | Path = "config/control-plane.yaml",
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> ControlPlaneConfig:
    """Load defaults, YAML, environment, and CLI overrides in increasing precedence."""

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Expected a mapping in {config_path}")
    file_values = dict(raw)
    if _nested_value_present(file_values, ("security", "secret_key")):
        raise ValueError(
            "security.secret_key is a secret and must come from LAB_SECRET_KEY or "
            "LAB_SECRET_KEY_FILE"
        )
    environment_values = _control_plane_environment(os.environ if environ is None else environ)
    merged = _deep_merge(file_values, environment_values)
    if overrides is not None:
        merged = _deep_merge(merged, overrides)
    return ControlPlaneConfig.model_validate(merged)


_EnvironmentConverter = Callable[[str], object]
_EnvironmentField = tuple[tuple[tuple[str, ...], ...], _EnvironmentConverter, bool]


_ENVIRONMENT_FIELDS: dict[str, _EnvironmentField] = {
    "LAB_PROFILE": ((("profile",),), str, False),
    "LAB_CONTROL_PLANE_HOST": ((("control_plane", "host"),), str, False),
    "LAB_CONTROL_PLANE_PORT": ((("control_plane", "port"),), lambda value: int(value), False),
    "LAB_PUBLIC_URL": (
        (("control_plane", "public_url"), ("web", "public_url")),
        str,
        False,
    ),
    "LAB_WEB_PUBLIC_URL": ((("web", "public_url"),), str, False),
    "LAB_LOG_LEVEL": ((("control_plane", "log_level"),), str, False),
    "LAB_DATABASE_URL": ((("database", "url"),), str, True),
    "LAB_DATABASE_POOL_SIZE": (
        (("database", "pool_size"),),
        lambda value: int(value),
        False,
    ),
    "LAB_ARTIFACT_DIR": ((("artifacts", "directory"),), str, False),
    "LAB_ARTIFACT_STORAGE_BACKEND": ((("artifacts", "storage_backend"),), str, False),
    "LAB_S3_BUCKET": ((("artifacts", "s3", "bucket"),), str, False),
    "LAB_S3_PREFIX": ((("artifacts", "s3", "prefix"),), str, False),
    "LAB_S3_ENDPOINT_URL": ((("artifacts", "s3", "endpoint_url"),), str, False),
    "LAB_S3_REGION": ((("artifacts", "s3", "region_name"),), str, False),
    "LAB_RETENTION_DEFAULT_DAYS": (
        (("retention", "artifacts", "default_days"),),
        lambda value: int(value),
        False,
    ),
    "LAB_RETENTION_FAILED_WORKFLOW_DAYS": (
        (("retention", "artifacts", "failed_workflow_days"),),
        lambda value: int(value),
        False,
    ),
    "LAB_RETENTION_FIRMWARE_DAYS": (
        (("retention", "artifacts", "firmware_days"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MINIMUM_AGENT_VERSION": (
        (("compatibility", "agents", "minimum_supported_version"),),
        str,
        False,
    ),
    "LAB_TARGET_AGENT_VERSION": (
        (("compatibility", "agents", "target_version"),),
        str,
        False,
    ),
    "LAB_SECRET_KEY": ((("security", "secret_key"),), str, True),
    "LAB_REVERSE_PROXY_ENABLED": (
        (("proxy", "enabled"),),
        lambda value: _environment_boolean("LAB_REVERSE_PROXY_ENABLED", value),
        False,
    ),
    "LAB_TRUSTED_PROXY_NETWORKS": (
        (("proxy", "trusted_networks"),),
        lambda value: _environment_csv(value),
        False,
    ),
    "LAB_MAX_CONCURRENT_WORKFLOWS": (
        (("resource_limits", "maximum_concurrent_workflows"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MAX_ACTIVE_CI_SESSIONS": (
        (("resource_limits", "maximum_active_ci_sessions"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MAX_SSE_STREAMS": (
        (("resource_limits", "maximum_sse_streams"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MAX_ARTIFACT_SIZE_MB": (
        (("resource_limits", "maximum_artifact_size_mb"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MAX_LOG_ARTIFACT_SIZE_MB": (
        (("resource_limits", "maximum_log_artifact_size_mb"),),
        lambda value: int(value),
        False,
    ),
    "LAB_MAX_RESERVATION_DURATION_MINUTES": (
        (("resource_limits", "maximum_reservation_duration_minutes"),),
        lambda value: int(value),
        False,
    ),
}


def resolve_secret_environment(
    name: str,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve one secret from ``NAME`` or a newline-terminated ``NAME_FILE`` mount."""

    source = os.environ if environ is None else environ
    direct = source.get(name)
    file_name = source.get(f"{name}_FILE")
    if direct is not None and file_name is not None:
        raise ValueError(f"Set only one of {name} and {name}_FILE")
    if direct is not None:
        if not direct:
            raise ValueError(f"{name} must not be empty")
        return direct
    if file_name is None:
        return None
    if not file_name:
        raise ValueError(f"{name}_FILE must not be empty")
    path = Path(file_name)
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise ValueError(f"Could not read {name}_FILE at {path}: {exc.strerror or exc}") from exc
    if not value:
        raise ValueError(f"{name}_FILE at {path} is empty")
    return value


def _control_plane_environment(environ: Mapping[str, str]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, (paths, converter, secret_file_supported) in _ENVIRONMENT_FIELDS.items():
        raw = (
            resolve_secret_environment(name, environ)
            if secret_file_supported
            else environ.get(name)
        )
        if raw is None:
            continue
        try:
            converted = converter(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid value for {name}") from exc
        for path in paths:
            _set_nested_value(values, path, converted)
    return values


def _environment_boolean(name: str, value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _environment_csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("comma-separated environment value must not be empty")
    return items


def _set_nested_value(values: dict[str, object], path: tuple[str, ...], value: object) -> None:
    target = values
    for component in path[:-1]:
        existing = target.setdefault(component, {})
        if not isinstance(existing, dict):  # pragma: no cover - constant path invariant
            raise TypeError(f"Environment path collision at {component}")
        target = existing
    target[path[-1]] = value


def _deep_merge(base: Mapping[str, object], update: Mapping[str, object]) -> dict[str, object]:
    merged: dict[str, object] = dict(base)
    for key, value in update.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _nested_value_present(values: Mapping[str, object], path: tuple[str, ...]) -> bool:
    current: object = values
    for component in path:
        if not isinstance(current, Mapping) or component not in current:
            return False
        current = current[component]
    return True


def _parse_public_url(value: str) -> SplitResult:
    return _parse_http_url(value, field_name="control_plane.public_url", allow_path=False)


def _parse_http_url(value: str, *, field_name: str, allow_path: bool) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{field_name} must use HTTP or HTTPS")
    if parsed.hostname is None:
        raise ValueError(f"{field_name} must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ValueError(f"{field_name} must not contain a path")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ValueError(f"{field_name} must not contain dot segments")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} contains an invalid port") from exc
    return parsed


def _is_loopback_host(value: str) -> bool:
    normalized = value.strip().casefold().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
