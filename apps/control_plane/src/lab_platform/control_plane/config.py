from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ControlPlaneArtifactSettings(ControlPlaneConfigModel):
    directory: Path = Path("./.lab-control-plane/artifacts")
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


class ControlPlaneDevelopmentSettings(ControlPlaneConfigModel):
    allow_insecure_agent_transport: bool = False
    allow_tls_termination_proxy: bool = False


class ControlPlaneConfig(ControlPlaneConfigModel):
    control_plane: ControlPlaneSettings = Field(default_factory=ControlPlaneSettings)
    database: ControlPlaneDatabaseSettings = Field(default_factory=ControlPlaneDatabaseSettings)
    agent_gateway: AgentGatewaySettings = Field(default_factory=AgentGatewaySettings)
    distributed: DistributedSettings = Field(default_factory=DistributedSettings)
    artifacts: ControlPlaneArtifactSettings = Field(default_factory=ControlPlaneArtifactSettings)
    development: ControlPlaneDevelopmentSettings = Field(
        default_factory=ControlPlaneDevelopmentSettings
    )

    @model_validator(mode="after")
    def require_safe_agent_transport(self) -> ControlPlaneConfig:
        public_url = urlsplit(self.control_plane.public_url)
        tls_configured = self.control_plane.tls_certificate_path is not None
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

    @property
    def agent_gateway_url(self) -> str:
        """Return the public WebSocket endpoint derived from the REST public URL."""

        parsed = urlsplit(self.control_plane.public_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/api/v1/agent-gateway", "", ""))


def load_control_plane_config(
    path: str | Path = "config/control-plane.yaml",
) -> ControlPlaneConfig:
    """Load one complete control-plane YAML configuration file."""

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Expected a mapping in {config_path}")
    return ControlPlaneConfig.model_validate(dict(raw))


def _parse_public_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("control_plane.public_url must use HTTP or HTTPS")
    if parsed.hostname is None:
        raise ValueError("control_plane.public_url must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("control_plane.public_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("control_plane.public_url must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("control_plane.public_url must not contain a path")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("control_plane.public_url contains an invalid port") from exc
    return parsed


def _is_loopback_host(value: str) -> bool:
    normalized = value.strip().casefold().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
