from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from lab_platform.core.errors import (
    OidcConfigurationInvalidError,
    OidcLoginFailedError,
)
from lab_platform.core.oidc import OidcProviderMetadata

_MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024


class HttpOidcProvider:
    """Small stdlib OIDC provider client.

    Provider responses and exception bodies are never logged or included in errors,
    because token endpoint payloads may contain credentials.
    """

    def __init__(
        self,
        *,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not client_secret:
            raise OidcConfigurationInvalidError(
                "The configured OIDC client secret environment variable is empty."
            )
        self._issuer_url = issuer_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout_seconds = timeout_seconds
        self._metadata: OidcProviderMetadata | None = None
        self._metadata_lock = asyncio.Lock()

    async def metadata(self) -> OidcProviderMetadata:
        async with self._metadata_lock:
            if self._metadata is not None:
                return self._metadata
            document = await asyncio.to_thread(
                self._get_json,
                f"{self._issuer_url}/.well-known/openid-configuration",
            )
            try:
                metadata = OidcProviderMetadata(
                    issuer=_required_string(document, "issuer"),
                    authorization_endpoint=_required_string(
                        document,
                        "authorization_endpoint",
                    ),
                    token_endpoint=_required_string(document, "token_endpoint"),
                    jwks_uri=_required_string(document, "jwks_uri"),
                )
            except ValueError:
                raise OidcConfigurationInvalidError(
                    "The identity provider discovery document is invalid."
                ) from None
            self._metadata = metadata
            return metadata

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> str:
        metadata = await self.metadata()
        form = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "code_verifier": code_verifier,
            }
        ).encode("ascii")
        basic_value = f"{quote_plus(self._client_id)}:{quote_plus(self._client_secret)}".encode()
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(basic_value).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        document = await asyncio.to_thread(
            self._request_json,
            metadata.token_endpoint,
            form,
            headers,
        )
        id_token = document.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcLoginFailedError("The identity provider did not return an ID token.")
        return id_token

    async def jwks(self) -> Mapping[str, object]:
        metadata = await self.metadata()
        return await asyncio.to_thread(self._get_json, metadata.jwks_uri)

    def _get_json(self, url: str) -> Mapping[str, object]:
        return self._request_json(url, None, {"Accept": "application/json"})

    def _request_json(
        self,
        url: str,
        data: bytes | None,
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        request = Request(url, data=data, headers=dict(headers))
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > _MAX_PROVIDER_RESPONSE_BYTES:
                    raise OidcLoginFailedError("The identity provider response is too large.")
                content = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise OidcLoginFailedError("The identity provider request failed.") from None
        if len(content) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise OidcLoginFailedError("The identity provider response is too large.")
        try:
            document = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OidcLoginFailedError("The identity provider response is invalid.") from None
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            raise OidcLoginFailedError("The identity provider response is invalid.")
        return document


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {key}")
    return value


__all__ = ["HttpOidcProvider"]
