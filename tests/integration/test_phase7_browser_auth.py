from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import lab_platform.control_plane.api as control_plane_api
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane.dashboard_api import _refresh_sse_actor
from lab_platform.models import (
    ApiTokenScope,
    AuthenticationContext,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    User,
)
from starlette.requests import Request

PASSWORD = "correct horse battery staple"


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "0.0.0.0",
                    "public_url": "https://lab.example.test",
                    "tls_certificate_path": tmp_path / "server.crt",
                    "tls_private_key_path": tmp_path / "server.key",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "artifacts": {"directory": tmp_path / "artifacts"},
            }
        )
    )


async def _seed_user(runtime: ControlPlaneRuntime) -> User:
    organisation = await runtime.identity_repository.get_organisation_by_slug("default")
    assert organisation is not None
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username="browser-user",
        display_name="Browser User",
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_user_with_password(
        user,
        PasswordCredential(
            user_id=user.id,
            password_hash=runtime.identity.hash_password(PASSWORD),
            created_at=now,
            updated_at=now,
        ),
        OrganisationMembership(
            organisation_id=organisation.id,
            user_id=user.id,
            role=OrganisationRole.MEMBER,
            created_at=now,
        ),
    )
    return user


def test_sse_revalidation_rejects_a_revoked_browser_session(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime), base_url="https://lab.example.test") as client:
        assert client.portal is not None
        client.portal.call(_seed_user, runtime)
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Lab-Auth-Mode": "cookie"},
            json={"username": "browser-user", "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        session_token = client.cookies.get("lab_session")
        assert session_token is not None
        actor = client.portal.call(runtime.identity.authenticate_session, session_token)
        assert actor.session_id is not None
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/api/v1/events",
                "raw_path": b"/api/v1/events",
                "query_string": b"",
                "headers": [(b"cookie", f"lab_session={session_token}".encode())],
                "client": ("127.0.0.1", 12345),
                "server": ("lab.example.test", 443),
                "root_path": "",
            }
        )
        dependency = control_plane_api._require_scopes(
            runtime,
            ApiTokenScope.BENCHES_READ,
            phase6_permissions=(),
        )

        current, refreshed = client.portal.call(
            _refresh_sse_actor,
            request,
            actor,
            dependency,
        )
        assert current is True
        assert isinstance(refreshed, AuthenticationContext)
        assert refreshed.session_id == actor.session_id

        client.portal.call(
            runtime.identity.revoke_session,
            actor,
            actor.session_id,
        )
        current, refreshed = client.portal.call(
            _refresh_sse_actor,
            request,
            actor,
            dependency,
        )
        assert current is False
        assert refreshed is None


def test_browser_cookie_session_csrf_refresh_logout_and_bearer_precedence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime), base_url="https://lab.example.test") as client:
        assert client.portal is not None
        client.portal.call(_seed_user, runtime)

        public_config = client.get("/api/v1/auth/config")
        assert public_config.status_code == 200
        assert public_config.json()["local_enabled"] is True
        assert public_config.json()["browser_session_enabled"] is True

        bearer_login = client.post(
            "/api/v1/auth/login",
            json={"username": "browser-user", "password": PASSWORD},
        ).json()
        bearer_token = bearer_login["access_token"]

        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Lab-Auth-Mode": "cookie"},
            json={"username": "browser-user", "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        assert "access_token" not in login.json()
        assert "lps_" not in login.text
        set_cookies = login.headers.get_list("set-cookie")
        session_header = next(value for value in set_cookies if value.startswith("lab_session="))
        csrf_header = next(value for value in set_cookies if value.startswith("lab_csrf="))
        assert "HttpOnly" in session_header
        assert "Secure" in session_header
        assert "SameSite=lax" in session_header
        assert "HttpOnly" not in csrf_header
        assert "Secure" in csrf_header

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["membership_role"] == "MEMBER"
        assert "organisation:read" in me.json()["permissions"]

        rejected = client.post("/api/v1/auth/refresh")
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "PERMISSION_DENIED"

        # An explicit bearer remains the API-client path even when stale browser
        # cookies are present, so it never acquires a CSRF requirement.
        bearer_refresh = client.post(
            "/api/v1/auth/refresh",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "X-CSRF-Token": "deliberately-wrong",
            },
        )
        assert bearer_refresh.status_code == 200, bearer_refresh.text
        assert str(bearer_refresh.json()["access_token"]).startswith("lps_")

        old_session = client.cookies.get("lab_session")
        csrf = client.cookies.get("lab_csrf")
        assert old_session is not None
        assert csrf is not None
        refreshed = client.post(
            "/api/v1/auth/refresh",
            headers={"X-CSRF-Token": csrf},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert "access_token" not in refreshed.json()
        assert client.cookies.get("lab_session") != old_session
        assert client.cookies.get("lab_csrf") != csrf

        rotated_csrf = client.cookies.get("lab_csrf")
        assert rotated_csrf is not None
        logged_out = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": rotated_csrf},
        )
        assert logged_out.status_code == 204
        assert client.cookies.get("lab_session") is None
        assert client.cookies.get("lab_csrf") is None
