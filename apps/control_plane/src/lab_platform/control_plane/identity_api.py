from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, Security, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core.errors import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from lab_platform.core.identity import IssuedSession
from lab_platform.models import AuthenticationContext, PrincipalType, UserSession
from pydantic import BaseModel, ConfigDict, Field

_IDENTITY_BEARER = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


class IdentityApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(IdentityApiModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=4096, repr=False)
    organisation_slug: str | None = Field(default=None, min_length=1, max_length=100)


def create_identity_router(runtime: ControlPlaneRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["identity"])

    async def require_identity(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> AuthenticationContext:
        if credentials is None:
            raise AuthenticationRequiredError("An Authorization bearer token is required.")
        return await authenticate_identity_token(
            runtime,
            credentials.credentials,
            source_ip=_source_ip(request),
        )

    @router.get("/oidc/login", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    async def oidc_login(
        organisation_slug: Annotated[
            str | None,
            Query(min_length=1, max_length=100),
        ] = None,
    ) -> RedirectResponse:
        started = await runtime.oidc.start_login(
            organisation_slug=(
                organisation_slug or runtime.config.identity.default_organisation_slug
            ),
            redirect_uri=_oidc_redirect_uri(runtime),
        )
        return RedirectResponse(
            started.authorization_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/oidc/callback")
    async def oidc_callback(
        request: Request,
        state_value: Annotated[
            str,
            Query(alias="state", min_length=1, max_length=1024),
        ],
        code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
        error: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> dict[str, object]:
        issued = await runtime.oidc.complete_login(
            state=state_value,
            code=code,
            provider_error=error,
            ip_address=_source_ip(request),
            user_agent=request.headers.get("User-Agent"),
            request_id=_request_id(request),
        )
        return _issued_session_payload(issued)

    @router.post("/login")
    async def login(body: LoginRequest, request: Request) -> dict[str, object]:
        if not runtime.config.identity.local_auth.enabled:
            raise AuthenticationFailedError("Local authentication is disabled.")
        issued = await runtime.identity.login(
            organisation_slug=(
                body.organisation_slug or runtime.config.identity.default_organisation_slug
            ),
            username=body.username,
            password=body.password,
            ip_address=_source_ip(request),
            user_agent=request.headers.get("User-Agent"),
            request_id=_request_id(request),
        )
        return _issued_session_payload(issued)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> Response:
        if credentials is None:
            raise AuthenticationRequiredError("An Authorization bearer token is required.")
        await runtime.identity.logout(
            credentials.credentials,
            request_id=_request_id(request),
            source_ip=_source_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/refresh")
    async def refresh(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> dict[str, object]:
        if credentials is None:
            raise AuthenticationRequiredError("An Authorization bearer token is required.")
        return _issued_session_payload(
            await runtime.identity.refresh_session(credentials.credentials)
        )

    @router.get("/me")
    async def me(
        context: Annotated[AuthenticationContext, Security(require_identity)],
    ) -> dict[str, object]:
        organisation = await runtime.identity_repository.get_organisation(
            context.principal.organisation_id
        )
        if organisation is None:
            raise AuthenticationFailedError("The principal organisation is unavailable.")
        return {
            "principal": context.principal.model_dump(mode="json"),
            "organisation": organisation.model_dump(mode="json"),
            "session_id": str(context.session_id) if context.session_id is not None else None,
            "credential_id": (
                str(context.credential_id) if context.credential_id is not None else None
            ),
        }

    @router.get("/sessions")
    async def sessions(
        context: Annotated[AuthenticationContext, Security(require_identity)],
    ) -> dict[str, object]:
        await _require_user_session_context(runtime, context, resource_id=None)
        return {
            "items": [
                _public_session(session)
                for session in await runtime.identity.list_sessions(context)
            ]
        }

    @router.delete("/sessions/{session_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_session(
        session_id: UUID,
        request: Request,
        context: Annotated[AuthenticationContext, Security(require_identity)],
    ) -> Response:
        await _require_user_session_context(
            runtime,
            context,
            resource_id=str(session_id),
        )
        await runtime.identity.revoke_session(
            context,
            session_id,
            request_id=_request_id(request),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


async def authenticate_identity_token(
    runtime: ControlPlaneRuntime,
    token: str,
    *,
    source_ip: str | None,
) -> AuthenticationContext:
    if token.startswith("lps_"):
        return await runtime.identity.authenticate_session(token)
    if is_phase6_api_credential(token):
        return await runtime.identity.authenticate_api_credential(token, source_ip=source_ip)
    raise AuthenticationFailedError("The bearer token is not an identity credential.")


def is_phase6_identity_token(token: str) -> bool:
    return token.startswith("lps_") or is_phase6_api_credential(token)


def is_phase6_api_credential(token: str) -> bool:
    prefix, separator, remainder = token.partition("_")
    identifier, secret_separator, secret = remainder.partition("_")
    return (
        prefix == "lp"
        and bool(separator)
        and bool(secret_separator)
        and len(identifier) == 32
        and all(character in "0123456789abcdefABCDEF" for character in identifier)
        and bool(secret)
    )


def _issued_session_payload(issued: IssuedSession) -> dict[str, object]:
    return {
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_at": issued.session.expires_at.isoformat(),
        "principal": issued.principal.model_dump(mode="json"),
        "organisation": issued.organisation.model_dump(mode="json"),
        "session": _public_session(issued.session),
    }


def _public_session(session: UserSession) -> dict[str, object]:
    return session.model_dump(mode="json", exclude={"secret_hash"})


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _request_id(request: Request) -> UUID | None:
    try:
        return UUID(str(request.state.request_id))
    except (AttributeError, ValueError):
        return None


def _oidc_redirect_uri(runtime: ControlPlaneRuntime) -> str:
    return runtime.config.control_plane.public_url.rstrip("/") + "/api/v1/auth/oidc/callback"


async def _require_user_session_context(
    runtime: ControlPlaneRuntime,
    context: AuthenticationContext,
    *,
    resource_id: str | None,
) -> None:
    if context.principal.type is not PrincipalType.USER or context.session_id is None:
        permission = "users:read"
        await runtime.authorisation.audit_permission_denied(
            context.principal,
            permission,
            resource_type="SESSION",
            resource_id=resource_id,
            reason="Only a user session may manage user sessions.",
        )
        raise PermissionDeniedError(
            "Only a user session may manage user sessions.",
            required_permission=permission,
            resource_type="SESSION",
            resource_id=resource_id,
        )


__all__ = [
    "authenticate_identity_token",
    "create_identity_router",
    "is_phase6_identity_token",
]
