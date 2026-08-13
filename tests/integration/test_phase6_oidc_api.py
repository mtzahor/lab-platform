from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from httpx2 import Response
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.core.oidc import OidcProviderMetadata
from lab_platform.models import (
    AuditEvent,
    AuditOutcome,
    AuthenticationSource,
    Organisation,
    User,
    UserStatus,
)

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
    runtime = _runtime(tmp_path, provider=provider)
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


def test_oidc_state_keeps_same_username_mapped_to_selected_tenant(tmp_path: Path) -> None:
    provider = FakeOidcProvider()
    runtime = _runtime(tmp_path, provider=provider)
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
    runtime = _runtime(tmp_path, provider=provider)
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
    disabled_runtime = _runtime(tmp_path / "disabled", enabled=False)
    with TestClient(create_app(disabled_runtime)) as client:
        response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "OIDC_CONFIGURATION_INVALID"

    provider = FakeOidcProvider()
    runtime = _runtime(tmp_path / "provider-error", provider=provider)
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


def _runtime(
    tmp_path: Path,
    *,
    provider: FakeOidcProvider | None = None,
    enabled: bool = True,
) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "artifacts": {"directory": tmp_path / "artifacts"},
                "identity": {
                    "oidc": {
                        "enabled": enabled,
                        "issuer_url": ISSUER if enabled else None,
                        "client_id": CLIENT_ID if enabled else None,
                        "client_secret_env": "TEST_OIDC_SECRET" if enabled else None,
                        "clock_skew_seconds": 0,
                    }
                },
                "development": {
                    "enabled": True,
                    "allow_insecure_agent_transport": True,
                },
            }
        ),
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
