from __future__ import annotations

import asyncio
import json
from types import TracebackType
from urllib.error import URLError
from urllib.request import Request

import pytest
from lab_platform.control_plane.oidc_provider import HttpOidcProvider
from lab_platform.core.errors import (
    OidcConfigurationInvalidError,
    OidcLoginFailedError,
)

ISSUER = "https://identity.example.test"


class FakeResponse:
    def __init__(
        self,
        document: object,
        *,
        content_length: str | None = None,
        raw: bytes | None = None,
    ) -> None:
        self._content = (
            raw if raw is not None else json.dumps(document, separators=(",", ":")).encode("utf-8")
        )
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, maximum_bytes: int) -> bytes:
        return self._content[:maximum_bytes]


def test_http_provider_discovers_exchanges_and_fetches_jwks_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 3.0
        requests.append(request)
        if request.full_url.endswith("openid-configuration"):
            return FakeResponse(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                }
            )
        if request.full_url.endswith("/token"):
            return FakeResponse(
                {
                    "access_token": "provider-access-secret",
                    "id_token": "signed.id.token",
                }
            )
        assert request.full_url.endswith("/jwks")
        return FakeResponse({"keys": [{"kid": "test-key"}]})

    monkeypatch.setattr(
        "lab_platform.control_plane.oidc_provider.urlopen",
        fake_urlopen,
    )
    provider = HttpOidcProvider(
        issuer_url=ISSUER,
        client_id="lab platform",
        client_secret="not logged secret",
        timeout_seconds=3.0,
    )

    async def scenario() -> None:
        metadata = await provider.metadata()
        assert metadata.issuer == ISSUER
        assert await provider.metadata() is metadata
        assert (
            await provider.exchange_code(
                code="authorization-code",
                redirect_uri="https://lab.example.test/api/v1/auth/oidc/callback",
                code_verifier="pkce-verifier",
            )
            == "signed.id.token"
        )
        assert await provider.jwks() == {"keys": [{"kid": "test-key"}]}

    asyncio.run(scenario())

    assert len(requests) == 3
    token_request = requests[1]
    assert isinstance(token_request.data, bytes)
    assert b"code=authorization-code" in token_request.data
    assert b"code_verifier=pkce-verifier" in token_request.data
    assert b"not+logged+secret" not in token_request.data
    assert "not logged secret" not in token_request.full_url
    assert token_request.get_header("Authorization", "").startswith("Basic ")


def test_http_provider_rejects_empty_secret_and_invalid_provider_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OidcConfigurationInvalidError, match="environment variable"):
        HttpOidcProvider(issuer_url=ISSUER, client_id="client", client_secret="")

    monkeypatch.setattr(
        "lab_platform.control_plane.oidc_provider.urlopen",
        lambda request, timeout: FakeResponse({"issuer": ISSUER}),
    )
    provider = HttpOidcProvider(
        issuer_url=ISSUER,
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(OidcConfigurationInvalidError, match="discovery document"):
        asyncio.run(provider.metadata())


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({}, raw=b"not-json"),
        FakeResponse([], raw=b"[]"),
        FakeResponse({}, content_length=str(2 * 1024 * 1024)),
    ],
)
def test_http_provider_rejects_invalid_or_oversized_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse,
) -> None:
    monkeypatch.setattr(
        "lab_platform.control_plane.oidc_provider.urlopen",
        lambda request, timeout: response,
    )
    provider = HttpOidcProvider(
        issuer_url=ISSUER,
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(OidcLoginFailedError):
        asyncio.run(provider.metadata())


def test_http_provider_hides_network_error_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_request: Request, *, timeout: float) -> FakeResponse:
        raise URLError(f"secret-bearing provider failure at {timeout}")

    monkeypatch.setattr("lab_platform.control_plane.oidc_provider.urlopen", fail)
    provider = HttpOidcProvider(
        issuer_url=ISSUER,
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(OidcLoginFailedError) as captured:
        asyncio.run(provider.metadata())
    assert "secret-bearing" not in captured.value.message


def test_http_provider_requires_an_id_token_from_the_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        if request.full_url.endswith("openid-configuration"):
            return FakeResponse(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                }
            )
        return FakeResponse({"access_token": "must-not-be-returned"})

    monkeypatch.setattr("lab_platform.control_plane.oidc_provider.urlopen", fake_urlopen)
    provider = HttpOidcProvider(
        issuer_url=ISSUER,
        client_id="client",
        client_secret="secret",
    )
    with pytest.raises(OidcLoginFailedError, match="ID token"):
        asyncio.run(
            provider.exchange_code(
                code="code",
                redirect_uri="https://lab.example.test/callback",
                code_verifier="verifier",
            )
        )
