from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.core.errors import AuthenticationRequiredError
from lab_platform.models import ApiToken, ApiTokenScope

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Phase 4 scoped API token.",
)


def require_scopes(
    agent: LabAgent,
    *required: ApiTokenScope,
) -> Callable[..., object]:
    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)] = None,
    ) -> ApiToken:
        return await agent.token_service.authenticate(
            _credential_token(credentials),
            required,
        )

    return dependency


def require_legacy_scopes(
    agent: LabAgent,
    *required: ApiTokenScope,
) -> Callable[..., object]:
    """Protect pre-Phase-4 routes once token authentication is bootstrapped.

    A fresh local installation has no administrator identity with which to create
    its first token.  The legacy API therefore retains its historical local-only
    behavior while the token store is empty.  As soon as a token record exists,
    all callers of this dependency must authenticate and possess the requested
    scopes.  Supplying an Authorization header always opts into validation,
    including during bootstrap.
    """

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)] = None,
    ) -> ApiToken | None:
        if credentials is None and not await agent.token_service.list():
            return None
        return await agent.token_service.authenticate(
            _credential_token(credentials),
            required,
        )

    return dependency


def bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise AuthenticationRequiredError("An Authorization bearer token is required.")
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not value.strip():
        raise AuthenticationRequiredError(
            "Authorization must use the Bearer authentication scheme."
        )
    return value.strip()


def _credential_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        return bearer_token(None)
    return bearer_token(f"{credentials.scheme} {credentials.credentials}")
