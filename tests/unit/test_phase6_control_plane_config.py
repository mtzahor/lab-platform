from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from lab_platform.control_plane.config import (
    AuditSettings,
    AuthorisationSettings,
    ControlPlaneConfig,
    IdentitySettings,
    LocalAuthSettings,
    LoginRateLimitSettings,
    OidcSettings,
    SessionSettings,
    load_control_plane_config,
)
from lab_platform.core import VERSION
from pydantic import ValidationError


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
    root = Path(__file__).resolve().parents[2]

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
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == "0.8.0a0"
    assert VERSION == "0.8.0-alpha"
