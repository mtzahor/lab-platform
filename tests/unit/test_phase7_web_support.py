from __future__ import annotations

import runpy
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lab_platform.control_plane.config import (
    ControlPlaneConfig,
    WebSettings,
    load_control_plane_config,
)
from lab_platform.control_plane.identity_api import safe_spa_return_to
from lab_platform.control_plane.web import create_web_router, web_content_security_policy
from pydantic import ValidationError


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
    root = Path(__file__).resolve().parents[2]

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
    root = Path(__file__).resolve().parents[2]
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
