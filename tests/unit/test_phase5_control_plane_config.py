from __future__ import annotations

from pathlib import Path

import pytest
from lab_platform.control_plane.cli import _with_bind_host
from lab_platform.control_plane.config import (
    AgentGatewaySettings,
    ControlPlaneConfig,
    ControlPlaneDatabaseSettings,
    ControlPlaneSettings,
    load_control_plane_config,
)
from pydantic import ValidationError


def test_control_plane_config_has_safe_loopback_bounded_defaults_and_is_frozen() -> None:
    with pytest.raises(ValidationError, match="requires control-plane TLS"):
        ControlPlaneConfig()

    config = ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "tls_certificate_path": Path("server.crt"),
                "tls_private_key_path": Path("server.key"),
            }
        }
    )

    assert config.control_plane.host == "127.0.0.1"
    assert config.control_plane.port == 8443
    assert config.agent_gateway.heartbeat_interval_seconds == 15
    assert config.agent_gateway.heartbeat_timeout_seconds == 45
    assert config.agent_gateway.offline_timeout_seconds == 90
    assert config.database.url == "sqlite:///./.lab-control-plane/control-plane.db"
    assert config.agent_gateway_url == "wss://127.0.0.1:8443/api/v1/agent-gateway"
    assert not config.development.allow_insecure_agent_transport
    assert not config.development.allow_tls_termination_proxy

    with pytest.raises(ValidationError, match="frozen"):
        config.control_plane.port = 9000


def test_load_control_plane_config_reads_all_sections_strictly(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.yaml"
    path.write_text(
        """
control_plane:
  host: 10.0.0.5
  port: 9443
  public_url: https://lab.example.internal:9443/
  log_level: WARNING
  max_request_body_size_mb: 8
  tls_certificate_path: ./tls/server.crt
  tls_private_key_path: ./tls/server.key
database:
  url: sqlite:///./state/control-plane.db
agent_gateway:
  heartbeat_interval_seconds: 10
  heartbeat_timeout_seconds: 30
  offline_timeout_seconds: 75
  handshake_timeout_seconds: 7
  maximum_message_size_mb: 4
  outgoing_queue_size: 128
  sequence_window_size: 2048
  monitor_interval_seconds: 0.5
distributed:
  maximum_clock_skew_seconds: 20
  offline_reservation_grace_seconds: 120
  operation_reconciliation_timeout_seconds: 240
  queue_commands_for_offline_agents: false
artifacts:
  directory: ./artifacts
  max_upload_size_mb: 750
  transfer_token_ttl_seconds: 180
  finalization_timeout_seconds: 240
development:
  allow_insecure_agent_transport: false
""".strip(),
        encoding="utf-8",
    )

    config = load_control_plane_config(path)

    assert config.control_plane.public_url == "https://lab.example.internal:9443"
    assert config.control_plane.tls_certificate_path == Path("tls/server.crt")
    assert config.control_plane.tls_private_key_path == Path("tls/server.key")
    assert config.agent_gateway.monitor_interval_seconds == 0.5
    assert config.distributed.operation_reconciliation_timeout_seconds == 240
    assert config.artifacts.directory == Path("artifacts")
    assert config.artifacts.finalization_timeout_seconds == 240
    assert config.agent_gateway_url == ("wss://lab.example.internal:9443/api/v1/agent-gateway")

    path.write_text("control_plane:\n  port: '8443'\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="valid integer"):
        load_control_plane_config(path)

    path.write_text("unknown_section: {}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_control_plane_config(path)


def test_checked_in_postgresql_configuration_is_production_safe() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "control-plane.postgresql.yaml"

    config = load_control_plane_config(path)

    assert config.database.url == (
        "postgresql://lab@database.internal:5432/lab_platform?sslmode=require&connect_timeout=10"
    )
    assert config.control_plane.host == "0.0.0.0"
    assert config.control_plane.public_url == "https://lab-control.internal:8443"
    assert not config.development.allow_insecure_agent_transport
    assert not config.development.allow_tls_termination_proxy


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        (
            "sqlite:////var/lib/lab-platform/control-plane.db",
            "sqlite:////var/lib/lab-platform/control-plane.db",
        ),
        (
            "postgresql://lab:encoded%20secret@database/lab_platform?sslmode=require",
            "postgresql://lab:encoded%20secret@database/lab_platform?sslmode=require",
        ),
        (
            "postgresql+psycopg://lab@database.example:5432/lab_platform?sslmode=verify-full",
            "postgresql://lab@database.example:5432/lab_platform?sslmode=verify-full",
        ),
        ("postgresql+psycopg:///lab_platform", "postgresql:///lab_platform"),
        ("postgresql:///lab_platform", "postgresql:///lab_platform"),
    ],
)
def test_database_accepts_supported_urls(url: str, expected: str) -> None:
    assert ControlPlaneDatabaseSettings(url=url).url == expected


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://relative.db",
        "mysql://database/lab_platform",
        "postgresql://database",
        "postgresql://database/",
        "postgresql://database/lab_platform#unsafe-fragment",
        "postgresql://database:invalid/lab_platform",
    ],
)
def test_database_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ControlPlaneDatabaseSettings(url=url)


def test_database_rejects_empty_sqlite_path() -> None:
    with pytest.raises(ValidationError, match="must include a database path"):
        ControlPlaneDatabaseSettings(url="sqlite:///")


def test_database_url_is_omitted_from_settings_repr() -> None:
    settings = ControlPlaneDatabaseSettings(
        url="postgresql://lab:do-not-print@database/lab_platform"
    )

    assert "do-not-print" not in repr(settings)


def test_invalid_database_url_input_is_hidden_from_validation_errors() -> None:
    password = "do-not-echo-this-password"

    with pytest.raises(ValidationError) as captured:
        ControlPlaneDatabaseSettings(url=f"postgresql://lab:{password}@database")

    assert password not in str(captured.value)


def test_cli_bind_override_cannot_bypass_loopback_transport_policy() -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://127.0.0.1:8443",
            },
            "development": {"allow_insecure_agent_transport": True},
        }
    )

    assert _with_bind_host(config, "localhost").control_plane.host == "localhost"
    with pytest.raises(ValidationError, match="only on loopback"):
        _with_bind_host(config, "0.0.0.0")


@pytest.mark.parametrize(
    ("certificate", "private_key"),
    [
        (Path("server.crt"), None),
        (None, Path("server.key")),
    ],
)
def test_tls_certificate_and_private_key_are_a_required_pair(
    certificate: Path | None,
    private_key: Path | None,
) -> None:
    with pytest.raises(ValidationError, match="configured together"):
        ControlPlaneSettings(
            tls_certificate_path=certificate,
            tls_private_key_path=private_key,
        )


def test_https_requires_direct_tls_or_explicit_loopback_proxy() -> None:
    with pytest.raises(ValidationError, match="requires control-plane TLS"):
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {"public_url": "https://lab.example.internal:8443"},
                "development": {"allow_insecure_agent_transport": False},
            }
        )

    proxied = ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "https://lab.example.internal:8443",
            },
            "development": {
                "allow_insecure_agent_transport": False,
                "allow_tls_termination_proxy": True,
            },
        }
    )
    assert proxied.agent_gateway_url == "wss://lab.example.internal:8443/api/v1/agent-gateway"

    with pytest.raises(ValidationError, match="requires a loopback"):
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "0.0.0.0",
                    "public_url": "https://lab.example.internal:8443",
                },
                "development": {"allow_tls_termination_proxy": True},
            }
        )


def test_tls_files_cannot_be_configured_for_plain_http() -> None:
    with pytest.raises(ValidationError, match="require an HTTPS public URL"):
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "public_url": "http://127.0.0.1:8443",
                    "tls_certificate_path": Path("server.crt"),
                    "tls_private_key_path": Path("server.key"),
                },
                "development": {"allow_insecure_agent_transport": True},
            }
        )


@pytest.mark.parametrize(
    "values",
    [
        {
            "heartbeat_interval_seconds": 45,
            "heartbeat_timeout_seconds": 45,
            "offline_timeout_seconds": 90,
        },
        {
            "heartbeat_interval_seconds": 15,
            "heartbeat_timeout_seconds": 90,
            "offline_timeout_seconds": 90,
        },
    ],
)
def test_agent_heartbeat_thresholds_must_be_strictly_ordered(
    values: dict[str, int],
) -> None:
    with pytest.raises(ValidationError, match="shorter than"):
        AgentGatewaySettings(**values)


def test_insecure_agent_transport_requires_opt_in_and_loopback() -> None:
    with pytest.raises(ValidationError, match="HTTPS/WSS"):
        ControlPlaneConfig.model_validate(
            {"control_plane": {"public_url": "http://127.0.0.1:8443"}}
        )

    local = ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://localhost:8080",
            },
            "development": {"allow_insecure_agent_transport": True},
        }
    )
    assert local.agent_gateway_url == "ws://localhost:8080/api/v1/agent-gateway"

    for settings in (
        {
            "host": "0.0.0.0",
            "public_url": "http://localhost:8080",
        },
        {
            "host": "127.0.0.1",
            "public_url": "http://lab.example.internal:8080",
        },
    ):
        with pytest.raises(ValidationError, match="loopback"):
            ControlPlaneConfig.model_validate(
                {
                    "control_plane": settings,
                    "development": {"allow_insecure_agent_transport": True},
                }
            )


@pytest.mark.parametrize(
    "public_url",
    [
        "ftp://lab.example.internal",
        "https://user:secret@lab.example.internal",
        "https://lab.example.internal/api",
        "https://lab.example.internal?token=secret",
        "https://lab.example.internal/#fragment",
        "https://lab.example.internal:invalid",
    ],
)
def test_public_url_rejects_unsafe_or_ambiguous_values(public_url: str) -> None:
    with pytest.raises(ValidationError):
        ControlPlaneSettings(public_url=public_url)


def test_loader_reports_missing_malformed_and_non_mapping_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_control_plane_config(tmp_path / "missing.yaml")

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("control_plane: [", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML"):
        load_control_plane_config(malformed)

    non_mapping = tmp_path / "list.yaml"
    non_mapping.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Expected a mapping"):
        load_control_plane_config(non_mapping)
