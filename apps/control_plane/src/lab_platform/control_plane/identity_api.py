import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, Security, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core.authorisation import ALL_PERMISSIONS
from lab_platform.core.errors import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from lab_platform.core.identity import IssuedSession
from lab_platform.models import (
    AuthenticationContext,
    AuthorisationResource,
    Organisation,
    OrganisationRole,
    Principal,
    PrincipalType,
    ResourceType,
    RoleName,
    UserSession,
)
from pydantic import BaseModel, ConfigDict, Field

_IDENTITY_BEARER = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
SESSION_COOKIE_NAME = "lab_session"
CSRF_COOKIE_NAME = "lab_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
AUTH_MODE_HEADER_NAME = "X-Lab-Auth-Mode"
OIDC_BROWSER_COOKIE_NAME = "lab_oidc_browser"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_MAX_CSRF_TOKEN_LENGTH = 256


class IdentityApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(IdentityApiModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=4096, repr=False)
    organisation_slug: str | None = Field(default=None, min_length=1, max_length=100)
    browser: bool = False


class WebClientConfig(IdentityApiModel):
    public_url: str
    api_base_url: str
    live_updates: dict[str, object]
    uploads: dict[str, object]
    features: dict[str, object]
    branding: dict[str, object]


class AuthenticationConfigResponse(IdentityApiModel):
    local_enabled: bool
    local_auth_enabled: bool
    oidc_enabled: bool
    browser_session_enabled: bool
    oidc_login_url: str | None
    default_organisation_slug: str
    web: WebClientConfig


class CurrentIdentityResponse(IdentityApiModel):
    principal: Principal
    organisation: Organisation
    session_id: UUID | None
    credential_id: UUID | None
    permissions: list[str]
    roles: list[RoleName]
    membership_role: OrganisationRole | None


@dataclass(frozen=True, slots=True)
class RequestIdentityCredential:
    token: str = field(repr=False)
    cookie_authenticated: bool = False


@dataclass(frozen=True, slots=True)
class _BrowserOidcIntent:
    return_to: str
    expires_at: datetime


def create_identity_router(runtime: ControlPlaneRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["identity"])

    async def require_identity(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> AuthenticationContext:
        return await authenticate_request_identity(runtime, request, credentials)

    @router.get("/config", response_model=AuthenticationConfigResponse)
    async def authentication_config() -> AuthenticationConfigResponse:
        web = runtime.config.web
        public_url = web.public_url or runtime.config.control_plane.public_url
        return AuthenticationConfigResponse(
            local_enabled=runtime.config.identity.local_auth.enabled,
            local_auth_enabled=runtime.config.identity.local_auth.enabled,
            oidc_enabled=runtime.config.identity.oidc.enabled,
            browser_session_enabled=True,
            oidc_login_url=(
                "/api/v1/auth/oidc/login?browser=true"
                if runtime.config.identity.oidc.enabled
                else None
            ),
            default_organisation_slug=runtime.config.identity.default_organisation_slug,
            web=WebClientConfig(
                public_url=public_url,
                api_base_url=web.api_base_url,
                live_updates=web.live_updates.model_dump(mode="json"),
                uploads=web.uploads.model_dump(mode="json"),
                features=web.features.model_dump(mode="json"),
                branding=web.branding.model_dump(mode="json"),
            ),
        )

    @router.get("/oidc/login", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    async def oidc_login(
        request: Request,
        organisation_slug: Annotated[
            str | None,
            Query(min_length=1, max_length=100),
        ] = None,
        browser: Annotated[bool, Query()] = False,
        return_to: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    ) -> RedirectResponse:
        browser_mode = _browser_mode_requested(request, browser)
        browser_return_to = safe_spa_return_to(return_to) if browser_mode else None
        started = await runtime.oidc.start_login(
            organisation_slug=(
                organisation_slug or runtime.config.identity.default_organisation_slug
            ),
            redirect_uri=_oidc_redirect_uri(runtime),
            browser_return_to=browser_return_to,
        )
        response = RedirectResponse(
            started.authorization_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Cache-Control": "no-store"},
        )
        if browser_mode:
            assert browser_return_to is not None
            _set_browser_oidc_cookie(
                response,
                runtime,
                state_digest=_oidc_authorization_state_digest(started.authorization_url),
                return_to=browser_return_to,
                expires_at=started.expires_at,
            )
        return response

    @router.get("/oidc/callback", response_model=None)
    async def oidc_callback(
        request: Request,
        state_value: Annotated[
            str,
            Query(alias="state", min_length=1, max_length=1024),
        ],
        code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
        error: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ) -> dict[str, object] | RedirectResponse:
        browser_intent = _browser_oidc_intent_from_cookie(request, state_value)
        completed = await runtime.oidc.complete_login_with_intent(
            state=state_value,
            code=code,
            provider_error=error,
            browser_state_digest=(
                _state_digest(state_value) if browser_intent is not None else None
            ),
            ip_address=_source_ip(request),
            user_agent=request.headers.get("User-Agent"),
            request_id=_request_id(request),
        )
        issued = completed.issued_session
        if completed.browser_return_to is not None:
            response = RedirectResponse(
                completed.browser_return_to,
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Cache-Control": "no-store"},
            )
            _clear_browser_oidc_cookie(response, runtime)
            _set_browser_session_cookies(response, runtime, issued)
            return response
        return _issued_session_payload(issued)

    @router.post("/login")
    async def login(
        body: LoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
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
        if _browser_mode_requested(request, body.browser):
            _set_browser_session_cookies(response, runtime, issued)
            return _browser_session_payload(issued)
        return _issued_session_payload(issued)

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> Response:
        credential = extract_bearer_or_cookie_token(request, credentials)
        if credential is None:
            raise AuthenticationRequiredError("A bearer token or browser session is required.")
        await runtime.identity.logout(
            credential.token,
            request_id=_request_id(request),
            source_ip=_source_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        if credential.cookie_authenticated or SESSION_COOKIE_NAME in request.cookies:
            _clear_browser_session_cookies(response, runtime)
        return response

    @router.post("/refresh")
    async def refresh(
        request: Request,
        response: Response,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_IDENTITY_BEARER),
        ] = None,
    ) -> dict[str, object]:
        credential = extract_bearer_or_cookie_token(request, credentials)
        if credential is None:
            raise AuthenticationRequiredError("A bearer token or browser session is required.")
        issued = await runtime.identity.refresh_session(credential.token)
        if credential.cookie_authenticated:
            _set_browser_session_cookies(response, runtime, issued)
            return _browser_session_payload(issued)
        return _issued_session_payload(issued)

    @router.get("/me", response_model=CurrentIdentityResponse)
    async def me(
        context: Annotated[AuthenticationContext, Security(require_identity)],
    ) -> CurrentIdentityResponse:
        organisation = await runtime.identity_repository.get_organisation(
            context.principal.organisation_id
        )
        if organisation is None:
            raise AuthenticationFailedError("The principal organisation is unavailable.")
        permissions, roles, membership_role = await _effective_identity_access(runtime, context)
        return CurrentIdentityResponse(
            principal=context.principal,
            organisation=organisation,
            session_id=context.session_id,
            credential_id=context.credential_id,
            permissions=sorted(permissions),
            roles=sorted(roles, key=lambda role: role.value),
            membership_role=membership_role,
        )

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


def extract_bearer_or_cookie_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
) -> RequestIdentityCredential | None:
    """Prefer an explicit bearer credential, otherwise validate a browser cookie.

    The explicit Authorization header remains exactly the API-client path and never
    acquires a CSRF requirement merely because the same request also carries cookies.
    """

    if credentials is not None:
        return RequestIdentityCredential(token=credentials.credentials)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    _require_cookie_csrf(request)
    return RequestIdentityCredential(token=token, cookie_authenticated=True)


async def authenticate_request_identity(
    runtime: ControlPlaneRuntime,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
) -> AuthenticationContext:
    credential = extract_bearer_or_cookie_token(request, credentials)
    if credential is None:
        raise AuthenticationRequiredError("A bearer token or browser session is required.")
    return await authenticate_identity_token(
        runtime,
        credential.token,
        source_ip=_source_ip(request),
    )


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


def _browser_session_payload(issued: IssuedSession) -> dict[str, object]:
    """Return browser bootstrap data without reflecting either session secret."""

    return {
        "authentication_mode": "cookie",
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


def _browser_mode_requested(request: Request, explicit: bool) -> bool:
    if explicit:
        return True
    return request.headers.get(AUTH_MODE_HEADER_NAME, "").strip().casefold() in {
        "browser",
        "cookie",
    }


def _set_browser_session_cookies(
    response: Response,
    runtime: ControlPlaneRuntime,
    issued: IssuedSession,
) -> None:
    secure = urlsplit(runtime.config.control_plane.public_url).scheme == "https"
    max_age = max(
        0,
        int((issued.session.maximum_expires_at - datetime.now(UTC)).total_seconds()),
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.access_token,
        max_age=max_age,
        expires=issued.session.maximum_expires_at,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        max_age=max_age,
        expires=issued.session.maximum_expires_at,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _clear_browser_session_cookies(
    response: Response,
    runtime: ControlPlaneRuntime,
) -> None:
    secure = urlsplit(runtime.config.control_plane.public_url).scheme == "https"
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def _require_cookie_csrf(request: Request) -> None:
    if request.method.upper() in _SAFE_METHODS:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    cookie_value = (
        cookie_token
        if cookie_token is not None and 0 < len(cookie_token) <= _MAX_CSRF_TOKEN_LENGTH
        else "\x00"
    )
    header_value = (
        header_token
        if header_token is not None and 0 < len(header_token) <= _MAX_CSRF_TOKEN_LENGTH
        else "\x01"
    )
    cookie_valid = cookie_token is not None and 0 < len(cookie_token) <= _MAX_CSRF_TOKEN_LENGTH
    header_valid = header_token is not None and 0 < len(header_token) <= _MAX_CSRF_TOKEN_LENGTH
    matches = hmac.compare_digest(cookie_value, header_value)
    if not cookie_valid or not header_valid or not matches:
        raise PermissionDeniedError("The browser CSRF token is missing or invalid.")


def safe_spa_return_to(value: str | None) -> str:
    """Reduce an untrusted post-login target to a same-origin dashboard route."""

    if value is None or not value or len(value) > 2048:
        return "/"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return "/"
    decoded = unquote(value)
    if "\\" in decoded or not decoded.startswith("/") or decoded.startswith("//"):
        return "/"
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc:
        return "/"
    first_segment = parsed.path.lstrip("/").partition("/")[0].casefold()
    if first_segment in {"api", "docs", "redoc", "openapi.json", ".well-known"}:
        return "/"
    return value


def _oidc_authorization_state_digest(authorization_url: str) -> str:
    states = parse_qs(urlsplit(authorization_url).query).get("state", [])
    if len(states) != 1 or not states[0]:
        raise AuthenticationFailedError("The OIDC login state could not be created.")
    return _state_digest(states[0])


def _state_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _set_browser_oidc_cookie(
    response: Response,
    runtime: ControlPlaneRuntime,
    *,
    state_digest: str,
    return_to: str,
    expires_at: datetime,
) -> None:
    payload = json.dumps(
        {
            "state": state_digest,
            "return_to": return_to,
            "expires_at": expires_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    value = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        OIDC_BROWSER_COOKIE_NAME,
        value,
        max_age=max_age,
        expires=expires_at,
        path="/api/v1/auth/oidc",
        secure=urlsplit(runtime.config.control_plane.public_url).scheme == "https",
        httponly=True,
        samesite="lax",
    )


def _clear_browser_oidc_cookie(
    response: Response,
    runtime: ControlPlaneRuntime,
) -> None:
    response.delete_cookie(
        OIDC_BROWSER_COOKIE_NAME,
        path="/api/v1/auth/oidc",
        secure=urlsplit(runtime.config.control_plane.public_url).scheme == "https",
        httponly=True,
        samesite="lax",
    )


def _browser_oidc_intent_from_cookie(
    request: Request,
    state_value: str,
) -> _BrowserOidcIntent | None:
    encoded = request.cookies.get(OIDC_BROWSER_COOKIE_NAME)
    if encoded is None or not encoded or len(encoded) > 4096:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        state_digest = payload.get("state")
        return_to = payload.get("return_to")
        expires_value = payload.get("expires_at")
        if not all(isinstance(item, str) for item in (state_digest, return_to, expires_value)):
            return None
        assert isinstance(state_digest, str)
        assert isinstance(return_to, str)
        assert isinstance(expires_value, str)
        if not hmac.compare_digest(state_digest, _state_digest(state_value)):
            return None
        expires_at = datetime.fromisoformat(expires_value)
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            return None
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= datetime.now(UTC):
            return None
        return _BrowserOidcIntent(
            return_to=safe_spa_return_to(return_to),
            expires_at=expires_at,
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def _effective_identity_access(
    runtime: ControlPlaneRuntime,
    context: AuthenticationContext,
) -> tuple[set[str], set[RoleName], OrganisationRole | None]:
    permissions: set[str] = set()
    roles: set[RoleName] = set()
    granting_assignment_ids: set[UUID] = set()
    restrictions = context.permission_restrictions
    for permission in ALL_PERMISSIONS:
        decision = await runtime.authorisation.evaluate_anywhere(
            context.principal,
            permission,
            credential_restrictions=restrictions,
        )
        if decision.allowed:
            permissions.add(permission)
            granting_assignment_ids.update(decision.granting_assignment_ids)

    organisation_decision = await runtime.authorisation.evaluate(
        context.principal,
        "organisation:read",
        _organisation_resource(context),
        credential_restrictions=restrictions,
    )
    roles.update(organisation_decision.roles)
    if granting_assignment_ids:
        assignments = await runtime.identity_repository.list_role_assignments(
            context.principal.organisation_id,
            None,
        )
        roles.update(
            assignment.role
            for assignment in assignments
            if assignment.id in granting_assignment_ids
        )

    membership_role: OrganisationRole | None = None
    if context.principal.type is PrincipalType.USER:
        membership = await runtime.identity_repository.get_organisation_membership(
            context.principal.organisation_id,
            context.principal.id,
        )
        if membership is not None:
            membership_role = membership.role
    return permissions, roles, membership_role


def _organisation_resource(context: AuthenticationContext) -> AuthorisationResource:
    return AuthorisationResource(
        type=ResourceType.ORGANISATION,
        id=str(context.principal.organisation_id),
        organisation_id=context.principal.organisation_id,
    )


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
    "AUTH_MODE_HEADER_NAME",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "SESSION_COOKIE_NAME",
    "RequestIdentityCredential",
    "authenticate_request_identity",
    "authenticate_identity_token",
    "create_identity_router",
    "extract_bearer_or_cookie_token",
    "is_phase6_identity_token",
    "safe_spa_return_to",
]
