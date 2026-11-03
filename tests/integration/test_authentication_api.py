from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import lab_platform.control_plane.api as control_plane_api
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from lab_platform.control_plane import ControlPlaneRuntime, create_app
from lab_platform.control_plane.dashboard_api import _refresh_sse_actor
from lab_platform.core.identity import IssuedApiCredential
from lab_platform.core.oidc import OidcProviderMetadata
from lab_platform.models import (
    ApiTokenScope,
    AuditEvent,
    AuditOutcome,
    AuthenticationContext,
    AuthenticationSource,
    CiSession,
    Organisation,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    PrincipalType,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    ServiceAccount,
    User,
    UserStatus,
)
from starlette.requests import Request

from tests.support.control_plane import make_control_plane_runtime

PASSWORD = "correct horse battery staple"


async def _get_ci_session(runtime: ControlPlaneRuntime, session_id: UUID) -> CiSession:
    return await runtime.ci.get(session_id, synchronize=False)


async def _login_audit_events(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    action: str = "USER_LOGIN_SUCCEEDED",
) -> list[AuditEvent]:
    return await runtime.identity_repository.list_audit_events(
        organisation_id,
        action=action,
    )


def _runtime(
    tmp_path: Path, *, legacy_token_compatibility_enabled: bool = True
) -> ControlPlaneRuntime:
    return make_control_plane_runtime(
        tmp_path,
        development_enabled=True,
        overrides={
            "authorisation": {
                "legacy_token_compatibility_enabled": legacy_token_compatibility_enabled
            }
        },
    )


async def _seed_user(
    runtime: ControlPlaneRuntime,
    username: str,
    display_name: str,
    role: OrganisationRole,
) -> User:
    organisation = await _default_organisation(runtime)
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username=username,
        display_name=display_name,
        created_at=now,
        updated_at=now,
    )
    password = PasswordCredential(
        user_id=user.id,
        password_hash=runtime.identity.hash_password(PASSWORD),
        created_at=now,
        updated_at=now,
    )
    membership = OrganisationMembership(
        organisation_id=organisation.id,
        user_id=user.id,
        role=role,
        created_at=now,
    )
    await runtime.identity_repository.create_user_with_password(user, password, membership)
    return user


async def _seed_service_credential(
    runtime: ControlPlaneRuntime,
    role: RoleName,
    permission_restrictions: set[str] | None = None,
) -> tuple[ServiceAccount, IssuedApiCredential]:
    organisation = await _default_organisation(runtime)
    now = datetime.now(UTC)
    account = ServiceAccount(
        organisation_id=organisation.id,
        name="github-ci",
        description="Phase 6 API integration test",
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_service_account(account)
    await runtime.identity_repository.create_role_assignment(
        RoleAssignment(
            organisation_id=organisation.id,
            subject_type=RoleSubjectType.SERVICE_ACCOUNT,
            subject_id=account.id,
            role=role,
            resource_type=ResourceType.ORGANISATION,
            resource_id=str(organisation.id),
            created_by=account.id,
            created_at=now,
        )
    )
    issued = await runtime.identity.issue_api_credential(
        service_account=account,
        name="test credential",
        permission_restrictions=permission_restrictions,
    )
    return account, issued


async def _default_organisation(runtime: ControlPlaneRuntime) -> Organisation:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    assert organisation is not None
    return organisation


def _login(client: TestClient, username: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _bearer(token: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_local_login_me_and_owner_authorisation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )

        invalid = client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "wrong password"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "INVALID_CREDENTIALS"

        payload = _login(client, "ALICE")
        assert set(payload) == {
            "access_token",
            "token_type",
            "expires_at",
            "principal",
            "organisation",
            "session",
        }
        assert payload["token_type"] == "bearer"
        assert str(payload["access_token"]).startswith("lps_")
        assert "secret_hash" not in str(payload)
        principal = payload["principal"]
        assert isinstance(principal, dict)
        assert principal["id"] == str(user.id)

        headers = _bearer(payload["access_token"])
        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["principal"]["display_name"] == "Alice Example"
        sessions = client.get("/api/v1/auth/sessions", headers=headers)
        assert sessions.status_code == 200
        assert len(sessions.json()["items"]) == 1
        assert "secret_hash" not in str(sessions.json())

        agents = client.get("/api/v1/agents", headers=headers)
        assert agents.status_code == 200, agents.text
        assert agents.json() == {"items": []}

        anonymous_legacy_bootstrap = client.post(
            "/api/v1/tokens",
            json={
                "name": "unsafe anonymous bootstrap",
                "owner": "anonymous",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert anonymous_legacy_bootstrap.status_code == 401


def test_login_audit_redacts_secret_shaped_user_agent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )
        injected_token = f"lps_{uuid4().hex}_{'x' * 43}"
        injected_client_secret = "opaque-client-secret-from-header"
        injected_api_key = "opaque-api-key-from-header"
        injected_passphrase = "passphrase-from-header"
        injected_password = "swordfish-from-header"
        injected_private_key = "-----BEGIN PGP PRIVATE KEY BLOCK----- opaque-private-key-payload"
        response = client.post(
            "/api/v1/auth/login",
            headers={
                "User-Agent": (
                    f"security-test Bearer {injected_token}; "
                    f'"client_secret": "{injected_client_secret}"; '
                    f"x-api-key={injected_api_key}; "
                    f"passphrase: {injected_passphrase}; "
                    f"password: {injected_password}; {injected_private_key}"
                )
            },
            json={"username": "alice", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text

        events = client.portal.call(
            _login_audit_events,
            runtime,
            user.organisation_id,
        )
        assert len(events) == 1
        serialized = events[0].model_dump_json()
        assert injected_token not in serialized
        assert injected_client_secret not in serialized
        assert injected_api_key not in serialized
        assert injected_passphrase not in serialized
        assert injected_password not in serialized
        assert "opaque-private-key-payload" not in serialized
        assert events[0].user_agent == "[REDACTED]"


def test_login_audit_redacts_and_bounds_oversized_user_agent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.OWNER,
        )
        oidc_token = f"eyJ{'a' * 16}.{'b' * 16}.{'c' * 16}"
        oversized_user_agent = f"{'u' * 994} {oidc_token}{'z' * 100}"

        invalid = client.post(
            "/api/v1/auth/login",
            headers={"User-Agent": oversized_user_agent},
            json={"username": "alice", "password": "wrong password"},
        )
        assert invalid.status_code == 401, invalid.text

        valid = client.post(
            "/api/v1/auth/login",
            headers={"User-Agent": oversized_user_agent},
            json={"username": "alice", "password": PASSWORD},
        )
        assert valid.status_code == 200, valid.text

        events = [
            *client.portal.call(
                _login_audit_events,
                runtime,
                user.organisation_id,
                "USER_LOGIN_FAILED",
            ),
            *client.portal.call(
                _login_audit_events,
                runtime,
                user.organisation_id,
                "USER_LOGIN_SUCCEEDED",
            ),
        ]
        assert len(events) == 2
        for event in events:
            assert event.user_agent is not None
            assert len(event.user_agent) == 1000
            assert event.user_agent.endswith("…[REDACTED]")
            serialized = event.model_dump_json()
            assert oidc_token not in serialized
            assert "eyJ" not in serialized


def test_session_refresh_revoke_and_logout(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        client.portal.call(
            _seed_user,
            runtime,
            "alice",
            "Alice Example",
            OrganisationRole.MEMBER,
        )
        first = _login(client, "alice")
        second = _login(client, "alice")
        first_headers = _bearer(first["access_token"])

        listing = client.get("/api/v1/auth/sessions", headers=first_headers)
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 2

        second_session = second["session"]
        assert isinstance(second_session, dict)
        revoked = client.delete(
            f"/api/v1/auth/sessions/{second_session['id']}",
            headers=first_headers,
        )
        assert revoked.status_code == 204
        rejected = client.get("/api/v1/auth/me", headers=_bearer(second["access_token"]))
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "SESSION_REVOKED"

        refreshed = client.post("/api/v1/auth/refresh", headers=first_headers)
        assert refreshed.status_code == 200, refreshed.text
        refreshed_token = refreshed.json()["access_token"]
        assert refreshed_token != first["access_token"]
        stale = client.get("/api/v1/auth/me", headers=first_headers)
        assert stale.status_code == 401
        fresh_headers = _bearer(refreshed_token)
        assert client.get("/api/v1/auth/me", headers=fresh_headers).status_code == 200

        logged_out = client.post("/api/v1/auth/logout", headers=fresh_headers)
        assert logged_out.status_code == 204
        after_logout = client.get("/api/v1/auth/me", headers=fresh_headers)
        assert after_logout.status_code == 401
        assert after_logout.json()["error"]["code"] == "SESSION_REVOKED"


def test_member_denial_is_audited_and_credential_narrowing_is_enforced(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        member = client.portal.call(
            _seed_user,
            runtime,
            "bob",
            "Bob Member",
            OrganisationRole.MEMBER,
        )
        member_login = _login(client, "bob")
        denied = client.get("/api/v1/agents", headers=_bearer(member_login["access_token"]))
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["required_permission"] == "agents:read"
        metrics_denied = client.get(
            "/metrics",
            headers=_bearer(member_login["access_token"]),
        )
        assert metrics_denied.status_code == 403
        assert metrics_denied.json()["error"]["details"]["required_permission"] == "agents:read"

        organisation = client.portal.call(_default_organisation, runtime)
        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            organisation.id,
        )
        assert any(
            event.action == "PERMISSION_DENIED"
            and event.actor_id == member.id
            and event.metadata == {"required_permission": "agents:read"}
            for event in audit_events
        )

        _account, issued = client.portal.call(
            _seed_service_credential,
            runtime,
            RoleName.AUDITOR,
            {"benches:read"},
        )
        credential_headers = _bearer(issued.token)
        me = client.get("/api/v1/auth/me", headers=credential_headers)
        assert me.status_code == 200
        assert me.json()["principal"]["type"] == "SERVICE_ACCOUNT"
        narrowed = client.get("/api/v1/agents", headers=credential_headers)
        assert narrowed.status_code == 403
        sessions = client.get("/api/v1/auth/sessions", headers=credential_headers)
        assert sessions.status_code == 403
        assert sessions.json()["error"]["details"] == {
            "required_permission": "users:read",
            "resource_type": "SESSION",
            "resource_id": None,
        }
        target_session_id = uuid4()
        revoke_session = client.delete(
            f"/api/v1/auth/sessions/{target_session_id}",
            headers=credential_headers,
        )
        assert revoke_session.status_code == 403
        assert revoke_session.json()["error"]["details"] == {
            "required_permission": "users:read",
            "resource_type": "SESSION",
            "resource_id": str(target_session_id),
        }

        service_denials = [
            event
            for event in client.portal.call(
                runtime.identity_repository.list_audit_events,
                organisation.id,
            )
            if event.action == "PERMISSION_DENIED"
            and event.actor_id == issued.credential.principal_id
            and event.resource_type == "SESSION"
        ]
        assert {event.resource_id for event in service_denials} == {None, str(target_session_id)}
        assert all(
            event.resource_type == "SESSION"
            and event.metadata == {"required_permission": "users:read"}
            for event in service_denials
        )


def test_service_account_runs_ci_with_identity_and_legacy_flag_is_honoured(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        account, issued = client.portal.call(
            _seed_service_credential,
            runtime,
            RoleName.WORKFLOW_RUNNER,
        )
        headers = _bearer(issued.token)
        created = client.post(
            "/api/v1/ci/sessions",
            headers=headers,
            json={
                "provider": "github_actions",
                "external_run_id": "phase6-identity",
                "requested_by": "spoofed-request-owner",
                "bench_request": {},
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["requested_by"] == account.name
        session = client.portal.call(_get_ci_session, runtime, UUID(created.json()["id"]))
        assert session.organisation_id == account.organisation_id
        assert session.requested_by_principal_id == account.id
        assert session.requested_by_principal_type is PrincipalType.SERVICE_ACCOUNT
        assert client.get("/api/v1/agents", headers=headers).status_code == 403
        # Resource routes hide both missing and inaccessible Agent identities.
        assert (
            client.post(
                f"/api/v1/agents/{uuid4()}/drain",
                headers=headers,
                json={},
            ).status_code
            == 404
        )

    disabled_runtime = _runtime(
        tmp_path / "legacy-disabled",
        legacy_token_compatibility_enabled=False,
    )
    with TestClient(create_app(disabled_runtime)) as client:
        assert client.portal is not None
        client.portal.call(
            _seed_user,
            disabled_runtime,
            "owner",
            "Owner",
            OrganisationRole.OWNER,
        )
        owner = _login(client, "owner")
        response = client.post(
            "/api/v1/tokens",
            headers=_bearer(owner["access_token"]),
            json={
                "name": "disabled",
                "owner": "owner",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"


ISSUER = "https://identity.example.test"


CLIENT_ID = "lab-platform"


class FakeOidcProvider:
    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce: str | None = None
        self.username = "alice"
        self.claim_overrides: dict[str, object] = {}
        self.last_code: str | None = None
        self.last_redirect_uri: str | None = None
        self.last_code_verifier: str | None = None
        self.exchange_count = 0

    async def metadata(self) -> OidcProviderMetadata:
        return OidcProviderMetadata(
            issuer=ISSUER,
            authorization_endpoint=f"{ISSUER}/authorize",
            token_endpoint=f"{ISSUER}/token",
            jwks_uri=f"{ISSUER}/jwks",
        )

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> str:
        self.exchange_count += 1
        self.last_code = code
        self.last_redirect_uri = redirect_uri
        self.last_code_verifier = code_verifier
        now = datetime.now(UTC).timestamp()
        claims: dict[str, object] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": self.nonce,
            "sub": "provider-subject-123",
            "preferred_username": self.username,
        }
        claims.update(self.claim_overrides)
        return self._signed_token(claims)

    async def jwks(self) -> Mapping[str, object]:
        numbers = self._private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kid": "test-key",
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "n": _encode_int(numbers.n),
                    "e": _encode_int(numbers.e),
                }
            ]
        }

    def _signed_token(self, claims: dict[str, object]) -> str:
        header = _encode_json({"alg": "RS256", "kid": "test-key", "typ": "JWT"})
        payload = _encode_json(claims)
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = self._private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_encode(signature)}"


def test_oidc_authorization_code_pkce_login_and_state_replay(tmp_path: Path) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(_seed_oidc_user, runtime, UserStatus.ACTIVE)

        started = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert started.status_code == 307
        assert started.headers["cache-control"] == "no-store"
        authorization_url = started.headers["location"]
        query = parse_qs(urlsplit(authorization_url).query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [CLIENT_ID]
        assert query["scope"] == ["openid profile email"]
        assert query["code_challenge_method"] == ["S256"]
        state = query["state"][0]
        provider.nonce = query["nonce"][0]

        completed = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": state, "code": "authorization-code"},
        )
        assert completed.status_code == 200, completed.text
        payload = completed.json()
        assert payload["principal"]["id"] == str(user.id)
        assert str(payload["access_token"]).startswith("lps_")
        assert "provider-subject-123" not in completed.text
        assert provider.last_code == "authorization-code"
        assert provider.last_redirect_uri == ("http://127.0.0.1:8443/api/v1/auth/oidc/callback")
        assert provider.last_code_verifier is not None
        expected_challenge = _encode(
            hashlib.sha256(provider.last_code_verifier.encode("ascii")).digest()
        )
        assert query["code_challenge"] == [expected_challenge]

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["principal"]["display_name"] == "Alice OIDC"

        replay = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": state, "code": "replayed-code"},
        )
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "OIDC_LOGIN_FAILED"
        assert provider.exchange_count == 1


def test_oidc_browser_intent_redirects_safely_while_legacy_callback_stays_json(
    tmp_path: Path,
) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime), follow_redirects=False) as client:
        assert client.portal is not None
        client.portal.call(_seed_oidc_user, runtime, UserStatus.ACTIVE)

        started = client.get(
            "/api/v1/auth/oidc/login",
            params={"browser": "true", "return_to": "/operations/current?tab=logs"},
        )
        query = parse_qs(urlsplit(started.headers["location"]).query)
        provider.nonce = query["nonce"][0]
        browser_cookie = next(
            cookie for cookie in client.cookies.jar if cookie.name == "lab_oidc_browser"
        )
        assert isinstance(browser_cookie.value, str)
        cookie_payload = json.loads(
            base64.urlsafe_b64decode(
                browser_cookie.value + ("=" * (-len(browser_cookie.value) % 4))
            )
        )
        cookie_payload["return_to"] = "/cookie-controlled"
        client.cookies.set(
            "lab_oidc_browser",
            base64.urlsafe_b64encode(json.dumps(cookie_payload, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode(),
            domain=browser_cookie.domain,
            path=browser_cookie.path,
        )
        completed = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": query["state"][0], "code": "browser-code"},
        )
        assert completed.status_code == 303
        assert completed.headers["location"] == "/operations/current?tab=logs"
        assert completed.text == ""
        assert client.cookies.get("lab_session") is not None
        assert client.cookies.get("lab_oidc_browser") is None

        legacy_start = client.get("/api/v1/auth/oidc/login")
        legacy_query = parse_qs(urlsplit(legacy_start.headers["location"]).query)
        provider.nonce = legacy_query["nonce"][0]
        legacy_callback = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": legacy_query["state"][0], "code": "legacy-code"},
        )
        assert legacy_callback.status_code == 200
        assert str(legacy_callback.json()["access_token"]).startswith("lps_")


def test_oidc_browser_callback_fails_closed_when_initiation_cookie_is_missing(
    tmp_path: Path,
) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime), follow_redirects=False) as client:
        assert client.portal is not None
        client.portal.call(_seed_oidc_user, runtime, UserStatus.ACTIVE)
        started = client.get(
            "/api/v1/auth/oidc/login",
            params={"browser": "true", "return_to": "/benches"},
        )
        query = parse_qs(urlsplit(started.headers["location"]).query)
        provider.nonce = query["nonce"][0]
        client.cookies.delete("lab_oidc_browser")

        completed = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": query["state"][0], "code": "browser-code"},
        )
        assert completed.status_code == 401
        assert completed.json()["error"]["code"] == "OIDC_LOGIN_FAILED"
        assert "access_token" not in completed.text
        assert "lps_" not in completed.text
        assert client.cookies.get("lab_session") is None
        assert provider.exchange_count == 0


def test_oidc_overlapping_browser_logins_never_downgrade_the_older_callback(
    tmp_path: Path,
) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime), follow_redirects=False) as client:
        assert client.portal is not None
        client.portal.call(_seed_oidc_user, runtime, UserStatus.ACTIVE)
        first = client.get(
            "/api/v1/auth/oidc/login",
            params={"browser": "true", "return_to": "/operations/first"},
        )
        first_query = parse_qs(urlsplit(first.headers["location"]).query)
        second = client.get(
            "/api/v1/auth/oidc/login",
            params={"browser": "true", "return_to": "/operations/second"},
        )
        second_query = parse_qs(urlsplit(second.headers["location"]).query)

        provider.nonce = first_query["nonce"][0]
        displaced = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": first_query["state"][0], "code": "first-code"},
        )
        assert displaced.status_code == 401
        assert "access_token" not in displaced.text
        assert "lps_" not in displaced.text
        assert provider.exchange_count == 0

        provider.nonce = second_query["nonce"][0]
        completed = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": second_query["state"][0], "code": "second-code"},
        )
        assert completed.status_code == 303
        assert completed.headers["location"] == "/operations/second"
        assert client.cookies.get("lab_session") is not None
        assert provider.exchange_count == 1


def test_oidc_state_keeps_same_username_mapped_to_selected_tenant(tmp_path: Path) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        default_user = client.portal.call(_seed_oidc_user, runtime, UserStatus.ACTIVE)
        second_user = client.portal.call(_seed_second_oidc_user, runtime)

        started = client.get(
            "/api/v1/auth/oidc/login",
            params={"organisation_slug": "SECOND"},
            follow_redirects=False,
        )
        assert started.status_code == 307
        query = parse_qs(urlsplit(started.headers["location"]).query)
        provider.nonce = query["nonce"][0]
        completed = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": query["state"][0], "code": "second-tenant-code"},
        )
        assert completed.status_code == 200, completed.text
        payload = completed.json()
        assert payload["principal"]["id"] == str(second_user.id)
        assert payload["principal"]["id"] != str(default_user.id)
        assert payload["organisation"]["id"] == str(second_user.organisation_id)


def test_oidc_rejects_unknown_and_disabled_mapped_users(tmp_path: Path) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path, provider=provider)
    with TestClient(create_app(runtime)) as client:
        unknown = _start_and_complete(client, provider)
        assert unknown.status_code == 403
        assert unknown.json()["error"]["code"] == "OIDC_IDENTITY_NOT_MAPPED"
        assert client.portal is not None
        assert client.portal.call(_list_users, runtime) == []
        failures = client.portal.call(_list_oidc_failures, runtime)
        assert len(failures) == 1
        assert failures[0].outcome is AuditOutcome.FAILED
        assert failures[0].metadata == {"authentication_source": "OIDC"}

        provider.username = "local-user"
        client.portal.call(_seed_local_user, runtime)
        local = _start_and_complete(client, provider)
        assert local.status_code == 403
        assert local.json()["error"]["code"] == "OIDC_IDENTITY_NOT_MAPPED"

        provider.username = "alice"
        client.portal.call(_seed_oidc_user, runtime, UserStatus.DISABLED)
        disabled = _start_and_complete(client, provider)
        assert disabled.status_code == 403
        assert disabled.json()["error"]["code"] == "USER_DISABLED"


def test_oidc_disabled_and_provider_error_are_explicit(tmp_path: Path) -> None:
    disabled_runtime = _runtime_oidc_api(tmp_path / "disabled", enabled=False)
    with TestClient(create_app(disabled_runtime)) as client:
        response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "OIDC_CONFIGURATION_INVALID"

    provider = FakeOidcProvider()
    runtime = _runtime_oidc_api(tmp_path / "provider-error", provider=provider)
    with TestClient(create_app(runtime)) as client:
        state = _start(client, provider)
        response = client.get(
            "/api/v1/auth/oidc/callback",
            params={"state": state, "error": "access_denied"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "OIDC_LOGIN_FAILED"
        assert "access_denied" not in response.text
        assert provider.exchange_count == 0
        assert client.portal is not None
        failures = client.portal.call(_list_oidc_failures, runtime)
        assert len(failures) == 1
        assert failures[0].reason == "The identity provider did not complete the login."


def _runtime_oidc_api(
    tmp_path: Path, *, provider: FakeOidcProvider | None = None, enabled: bool = True
) -> ControlPlaneRuntime:
    return make_control_plane_runtime(
        tmp_path,
        development_enabled=True,
        overrides={
            "identity": {
                "oidc": {
                    "enabled": enabled,
                    "issuer_url": ISSUER if enabled else None,
                    "client_id": CLIENT_ID if enabled else None,
                    "client_secret_env": "TEST_OIDC_SECRET" if enabled else None,
                    "clock_skew_seconds": 0,
                }
            }
        },
        oidc_provider=provider,
    )


async def _seed_oidc_user(
    runtime: ControlPlaneRuntime,
    status: UserStatus,
) -> User:
    organisation = await runtime.identity_repository.get_organisation_by_slug("default")
    assert organisation is not None
    user = User(
        organisation_id=organisation.id,
        username="alice",
        display_name="Alice OIDC",
        email="alice@example.test",
        status=status,
        authentication_source=AuthenticationSource.OIDC,
    )
    return await runtime.identity_repository.create_user(user)


async def _seed_second_oidc_user(runtime: ControlPlaneRuntime) -> User:
    organisation = await runtime.identity_repository.create_organisation(
        Organisation(slug="second", name="Second Lab")
    )
    return await runtime.identity_repository.create_user(
        User(
            organisation_id=organisation.id,
            username="alice",
            display_name="Second Alice",
            authentication_source=AuthenticationSource.OIDC,
        )
    )


async def _seed_local_user(runtime: ControlPlaneRuntime) -> User:
    organisation = await runtime.identity_repository.get_organisation_by_slug("default")
    assert organisation is not None
    return await runtime.identity_repository.create_user(
        User(
            organisation_id=organisation.id,
            username="local-user",
            display_name="Local User",
            authentication_source=AuthenticationSource.LOCAL,
        )
    )


async def _list_users(runtime: ControlPlaneRuntime) -> list[User]:
    organisation = await runtime.identity_repository.get_organisation_by_slug("default")
    assert organisation is not None
    return await runtime.identity_repository.list_users(organisation.id)


async def _list_oidc_failures(runtime: ControlPlaneRuntime) -> list[AuditEvent]:
    organisation = await runtime.identity_repository.get_organisation_by_slug("default")
    assert organisation is not None
    return await runtime.identity_repository.list_audit_events(
        organisation.id,
        action="USER_LOGIN_FAILED",
    )


def _start(client: TestClient, provider: FakeOidcProvider) -> str:
    response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 307
    query = parse_qs(urlsplit(response.headers["location"]).query)
    provider.nonce = query["nonce"][0]
    return query["state"][0]


def _start_and_complete(client: TestClient, provider: FakeOidcProvider) -> Response:
    return client.get(
        "/api/v1/auth/oidc/callback",
        params={"state": _start(client, provider), "code": "authorization-code"},
    )


def _encode_json(value: dict[str, object]) -> str:
    return _encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _encode_int(value: int) -> str:
    return _encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _runtime_browser_auth(tmp_path: Path) -> ControlPlaneRuntime:
    return make_control_plane_runtime(
        tmp_path,
        overrides={
            "control_plane": {
                "host": "0.0.0.0",
                "public_url": "https://lab.example.test",
                "tls_certificate_path": tmp_path / "server.crt",
                "tls_private_key_path": tmp_path / "server.key",
            },
            "development": {},
        },
    )


async def _seed_user_browser_auth(runtime: ControlPlaneRuntime) -> User:
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
    runtime = _runtime_browser_auth(tmp_path)
    with TestClient(create_app(runtime), base_url="https://lab.example.test") as client:
        assert client.portal is not None
        client.portal.call(_seed_user_browser_auth, runtime)
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
    runtime = _runtime_browser_auth(tmp_path)
    with TestClient(create_app(runtime), base_url="https://lab.example.test") as client:
        assert client.portal is not None
        client.portal.call(_seed_user_browser_auth, runtime)

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
