from __future__ import annotations

import runpy
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lab_platform.control_plane.cli import _with_bind_host
from lab_platform.control_plane.config import (
    AgentGatewaySettings,
    AuditSettings,
    AuthorisationSettings,
    ControlPlaneConfig,
    ControlPlaneDatabaseSettings,
    ControlPlaneSettings,
    IdentitySettings,
    LocalAuthSettings,
    LoginRateLimitSettings,
    OidcSettings,
    SessionSettings,
    WebSettings,
    load_control_plane_config,
)
from lab_platform.control_plane.identity_api import safe_spa_return_to
from lab_platform.control_plane.web import create_web_router, web_content_security_policy
from lab_platform.core import VERSION
from pydantic import ValidationError

from tests.support.paths import REPOSITORY_ROOT


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
    path = REPOSITORY_ROOT / "config" / "control-plane.postgresql.yaml"

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


def _loopback_config(*, development: dict[str, object] | None = None) -> ControlPlaneConfig:
    development_settings: dict[str, object] = {
        "allow_insecure_agent_transport": True,
    }
    development_settings.update(development or {})
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://127.0.0.1:8443",
            },
            "development": development_settings,
        }
    )


def _tls_config(
    *,
    host: str,
    public_url: str,
    development: dict[str, object],
) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": host,
                "public_url": public_url,
                "tls_certificate_path": Path("server.crt"),
                "tls_private_key_path": Path("server.key"),
            },
            "development": development,
        }
    )


def test_phase6_identity_and_security_defaults_preserve_phase5_configuration() -> None:
    config = _loopback_config()

    assert config.identity == IdentitySettings()
    assert config.identity.enabled
    assert config.identity.default_organisation_slug == "default"
    assert config.identity.default_organisation_name == "Default Organisation"
    assert config.identity.local_auth == LocalAuthSettings()
    assert config.identity.local_auth.minimum_password_length == 12
    assert config.identity.oidc == OidcSettings()
    assert config.identity.oidc.scopes == ("openid", "profile", "email")
    assert config.identity.sessions == SessionSettings()
    assert config.identity.sessions.access_token_minutes == 15
    assert config.identity.sessions.session_hours == 12
    assert config.identity.sessions.maximum_session_days == 7
    assert config.authorisation == AuthorisationSettings()
    assert config.authorisation.default_bench_visibility == "organisation"
    assert config.authorisation.hide_unauthorised_resources
    assert config.authorisation.legacy_token_compatibility_enabled
    assert config.audit == AuditSettings(enabled=True, retention_days=90)
    assert config.security.login_rate_limit == LoginRateLimitSettings(
        attempts=10,
        window_minutes=15,
    )
    assert not config.development.enabled
    assert config.development.auto_login_user is None


def test_checked_in_configs_expose_phase6_settings() -> None:
    root = REPOSITORY_ROOT

    local = load_control_plane_config(root / "config" / "control-plane.yaml")
    production = load_control_plane_config(root / "config" / "control-plane.postgresql.yaml")

    assert local.identity.enabled
    assert local.development.enabled
    assert local.development.auto_login_user == "local-admin"
    assert local.authorisation.legacy_token_compatibility_enabled
    assert local.audit.retention_days == 90
    assert local.security.login_rate_limit.attempts == 10

    assert production.identity.enabled
    assert not production.development.enabled
    assert production.development.auto_login_user is None
    assert production.identity.oidc.scopes == ("openid", "profile", "email")


def test_local_password_policy_cannot_be_weakened_below_twelve_characters() -> None:
    with pytest.raises(ValidationError):
        LocalAuthSettings(minimum_password_length=11)

    with pytest.raises(ValidationError):
        LocalAuthSettings.model_validate({"minimum_password_length": "12"})


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": True},
        {
            "enabled": True,
            "client_id": "lab-platform",
            "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
        },
        {
            "enabled": True,
            "issuer_url": "https://identity.example.com",
            "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
        },
        {
            "enabled": True,
            "issuer_url": "https://identity.example.com",
            "client_id": "lab-platform",
        },
    ],
)
def test_enabled_oidc_requires_complete_provider_configuration(
    settings: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="enabled OIDC requires"):
        OidcSettings.model_validate(settings)


def test_oidc_accepts_yaml_scopes_and_environment_variable_reference() -> None:
    settings = OidcSettings.model_validate(
        {
            "enabled": True,
            "issuer_url": "https://identity.example.com/oidc",
            "client_id": "lab-platform",
            "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
            "scopes": ["openid", "profile", "email"],
        }
    )

    assert settings.issuer_url == "https://identity.example.com/oidc"
    assert settings.scopes == ("openid", "profile", "email")


@pytest.mark.parametrize(
    "issuer_url",
    [
        "identity.example.com",
        "ftp://identity.example.com",
        "https://user:secret@identity.example.com",
        "https://identity.example.com?tenant=lab",
        "https://identity.example.com#fragment",
        "https://identity.example.com:invalid",
    ],
)
def test_oidc_rejects_unsafe_or_ambiguous_issuer_urls(issuer_url: str) -> None:
    with pytest.raises(ValidationError):
        OidcSettings(issuer_url=issuer_url)


@pytest.mark.parametrize(
    "client_secret_env",
    ["", "9OIDC_SECRET", "OIDC-SECRET", "OIDC SECRET", "${OIDC_SECRET}"],
)
def test_oidc_client_secret_env_must_be_a_variable_name(client_secret_env: str) -> None:
    with pytest.raises(ValidationError):
        OidcSettings(client_secret_env=client_secret_env)


def test_enabled_oidc_requires_openid_scope() -> None:
    with pytest.raises(ValidationError, match="openid scope"):
        OidcSettings.model_validate(
            {
                "enabled": True,
                "issuer_url": "https://identity.example.com",
                "client_id": "lab-platform",
                "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
                "scopes": ["profile", "email"],
            }
        )


def test_oidc_requires_https_except_for_loopback_development() -> None:
    with pytest.raises(ValidationError, match="HTTPS except on loopback"):
        OidcSettings.model_validate(
            {
                "enabled": True,
                "issuer_url": "http://identity.example.com",
                "client_id": "lab-platform",
                "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
            }
        )

    loopback = OidcSettings.model_validate(
        {
            "enabled": True,
            "issuer_url": "http://127.0.0.1:9090",
            "client_id": "lab-platform",
            "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
        }
    )
    assert loopback.enabled


def test_enabled_oidc_requires_the_identity_layer() -> None:
    with pytest.raises(ValidationError, match="requires identity.enabled"):
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "public_url": "http://127.0.0.1:8443",
                },
                "identity": {
                    "enabled": False,
                    "oidc": {
                        "enabled": True,
                        "issuer_url": "https://identity.example.com",
                        "client_id": "lab-platform",
                        "client_secret_env": "LAB_PLATFORM_OIDC_CLIENT_SECRET",
                    },
                },
                "development": {"allow_insecure_agent_transport": True},
            }
        )


def test_oidc_scopes_are_non_empty_and_unique() -> None:
    with pytest.raises(ValidationError):
        OidcSettings.model_validate({"scopes": []})
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        OidcSettings.model_validate({"scopes": ["openid", "openid"]})


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {
                "access_token_minutes": 60,
                "session_hours": 1,
                "maximum_session_days": 7,
            },
            "shorter than session lifetime",
        ),
        (
            {
                "access_token_minutes": 15,
                "session_hours": 49,
                "maximum_session_days": 2,
            },
            "must not exceed maximum session lifetime",
        ),
    ],
)
def test_session_lifetimes_must_be_ordered(
    settings: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        SessionSettings.model_validate(settings)


def test_session_lifetime_may_equal_configured_maximum() -> None:
    settings = SessionSettings(
        access_token_minutes=15,
        session_hours=24,
        maximum_session_days=1,
    )

    assert settings.session_hours == 24


def test_development_auto_login_requires_explicit_development_mode() -> None:
    with pytest.raises(ValidationError, match="requires development.enabled"):
        _loopback_config(development={"auto_login_user": "local-admin"})


@pytest.mark.parametrize(
    ("host", "public_url"),
    [
        ("0.0.0.0", "https://127.0.0.1:8443"),
        ("127.0.0.1", "https://lab.example.internal:8443"),
    ],
)
def test_development_auto_login_requires_loopback_bind_and_public_hosts(
    host: str,
    public_url: str,
) -> None:
    with pytest.raises(ValidationError, match="loopback bind and public hosts"):
        _tls_config(
            host=host,
            public_url=public_url,
            development={"enabled": True, "auto_login_user": "local-admin"},
        )


def test_development_auto_login_accepts_explicit_loopback_mode() -> None:
    config = _loopback_config(development={"enabled": True, "auto_login_user": "local-admin"})

    assert config.development.auto_login_user == "local-admin"


def test_phase6_sections_remain_strict_and_bounded() -> None:
    with pytest.raises(ValidationError):
        IdentitySettings.model_validate({"enabled": "true"})
    with pytest.raises(ValidationError):
        IdentitySettings.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        IdentitySettings(default_organisation_slug="Invalid Slug")
    with pytest.raises(ValidationError):
        AuthorisationSettings(default_bench_visibility="public")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AuditSettings(retention_days=0)
    with pytest.raises(ValidationError):
        LoginRateLimitSettings(attempts=0)


def test_phase6_version_identifiers_are_consistent() -> None:
    root = REPOSITORY_ROOT
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == "0.9.0b0"
    assert VERSION == "0.9.0-beta"


def test_web_settings_are_strict_bounded_and_cross_checked() -> None:
    settings = WebSettings.model_validate(
        {
            "enabled": True,
            "public_url": "https://lab.example.test/",
            "api_base_url": "/api/v1/",
            "live_updates": {"sse_enabled": True, "polling_fallback_seconds": 4},
            "uploads": {"maximum_firmware_size_mb": 50},
            "features": {
                "identity_admin": True,
                "audit_viewer": True,
                "ci_sessions": True,
            },
            "branding": {"product_name": "Hardware Lab"},
        }
    )
    assert settings.public_url == "https://lab.example.test"
    assert settings.api_base_url == "/api/v1"
    assert settings.live_updates.polling_fallback_seconds == 4

    for invalid in (
        {"public_url": "http://lab.example.test"},
        {"api_base_url": "//evil.example/api"},
        {"api_base_url": "/api/v1?secret=value"},
        {"live_updates": {"polling_fallback_seconds": 0}},
        {"features": {"unexpected": True}},
    ):
        with pytest.raises(ValidationError):
            WebSettings.model_validate(invalid)

    with pytest.raises(ValidationError, match="requires web.public_url"):
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "public_url": "http://127.0.0.1:8443",
                },
                "web": {"enabled": True},
                "development": {"allow_insecure_agent_transport": True},
            }
        )


def test_checked_in_control_plane_configs_enable_integrated_web() -> None:
    root = REPOSITORY_ROOT

    local = load_control_plane_config(root / "config" / "control-plane.yaml")
    production = load_control_plane_config(root / "config" / "control-plane.postgresql.yaml")

    assert local.web.enabled
    assert local.web.public_url == "http://127.0.0.1:8443"
    assert local.web.api_base_url == "/api/v1"
    assert production.web.enabled
    assert production.web.public_url == "https://lab-control.internal:8443"


def test_static_web_router_cache_fallback_exclusions_and_csp(tmp_path: Path) -> None:
    web_dist = tmp_path / "web_dist"
    assets = web_dist / "assets"
    assets.mkdir(parents=True)
    (web_dist / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
    (assets / "app-A1b2C3d4.js").write_text("export {};", encoding="utf-8")
    (assets / "app-A1b2C3d4.js.map").write_text("{}", encoding="utf-8")

    settings = WebSettings(
        enabled=True,
        public_url="https://dashboard.example.test",
        api_base_url="https://api.example.test/api/v1",
    )
    app = FastAPI()
    app.include_router(create_web_router(settings, directory=web_dist))

    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["cache-control"] == "no-cache"
        assert "script-src 'self'" in index.headers["content-security-policy"]
        assert "https://api.example.test" in index.headers["content-security-policy"]

        route = client.get("/benches/sim-001")
        assert route.status_code == 200
        assert route.text == "<html>dashboard</html>"

        asset = client.get("/assets/app-A1b2C3d4.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "javascript" in asset.headers["content-type"]

        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/assets/app-A1b2C3d4.js.map").status_code == 404
        assert client.get("/api/v1/missing").status_code == 404
        assert client.get("/.env").status_code == 404

    policy = web_content_security_policy(settings)
    assert "wss://api.example.test" in policy


def test_enabled_static_web_fails_fast_without_a_bundle(tmp_path: Path) -> None:
    settings = WebSettings(enabled=True, public_url="https://lab.example.test")
    with pytest.raises(RuntimeError, match="web_dist/index.html"):
        create_web_router(settings, directory=tmp_path / "missing")


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/benches/sim-001?tab=logs", "/benches/sim-001?tab=logs"),
        ("https://evil.example/path", "/"),
        ("//evil.example/path", "/"),
        ("/%2f%2fevil.example/path", "/"),
        ("/\\evil.example/path", "/"),
        ("/api/v1/auth/config", "/"),
        ("/docs", "/"),
    ],
)
def test_post_login_return_path_is_limited_to_spa_routes(
    candidate: str,
    expected: str,
) -> None:
    assert safe_spa_return_to(candidate) == expected


def test_wheel_verifier_selects_current_build_from_a_mixed_version_glob() -> None:
    root = REPOSITORY_ROOT
    verifier = runpy.run_path(str(root / "scripts" / "verify_web_wheel.py"))
    select_current_wheel = verifier["_select_current_wheel"]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    distribution = project["name"].replace("-", "_")
    version = project["version"]
    current = Path(f"dist/{distribution}-{version}-py3-none-any.whl")
    historical = [
        Path(f"dist/{distribution}-0.5.0a0-py3-none-any.whl"),
        Path(f"dist/{distribution}-0.6.0a0-py3-none-any.whl"),
    ]

    assert select_current_wheel([*historical, current]) == current
    assert select_current_wheel([historical[0]]) == historical[0]
    with pytest.raises(ValueError, match="exactly one.*current version"):
        select_current_wheel(historical)
