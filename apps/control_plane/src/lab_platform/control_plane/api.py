import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.agent_protocol import PROTOCOL_VERSION
from lab_platform.control_plane.artifact_access import ArtifactContent
from lab_platform.control_plane.dashboard_api import (
    create_dashboard_router,
    enrich_benches,
    reservation_action_permissions,
)
from lab_platform.control_plane.identity_admin_api import create_identity_admin_router
from lab_platform.control_plane.identity_api import (
    authenticate_identity_token,
    create_identity_router,
    extract_bearer_or_cookie_token,
    is_phase6_identity_token,
)
from lab_platform.control_plane.rate_limits import (
    InMemoryRateLimiter,
    RateLimitDecision,
    RateLimitPolicy,
)
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane.web import create_web_router
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiCreateRequest,
    DistributedCiStartRequest,
)
from lab_platform.control_plane_core.errors import (
    AgentNotFoundError,
    AgentOfflineError,
    RemoteCommandNotFoundError,
    ReservationLeaseInvalidError,
)
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowRequest,
)
from lab_platform.core import (
    API_VERSION,
    PLUGIN_API_VERSION,
    VERSION,
    build_metadata,
    build_test_results,
    render_junit_xml,
)
from lab_platform.core.errors import (
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    AuthenticationFailedError,
    AuthenticationRequiredError,
    BenchNotFoundError,
    CapabilityNotSupportedError,
    CiSessionNotFoundError,
    OperationNotCancellableError,
    PermissionDeniedError,
    PlatformError,
    RequestBodyTooLargeError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.workflows import WorkflowNotFoundError
from lab_platform.models import (
    ActorContext,
    AgentStatus,
    AgentTimelineSeverity,
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    AuthenticationContext,
    AuthorisationResource,
    AuthorisationSnapshot,
    BenchRequest,
    CiProvider,
    CiSession,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationStatus,
    Principal,
    PrincipalType,
    RemoteCommand,
    RemoteCommandType,
    Reservation,
    ReservationOwner,
    ReservationStatus,
    ResourceType,
    SerialReadRequest,
    UserStatus,
    WorkflowDefinition,
    WorkflowStepResult,
)
from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException
from starlette.types import Message

_BEARER = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def _artifact_content_response(content: ArtifactContent) -> StreamingResponse:
    return StreamingResponse(
        content.stream,
        media_type=content.record.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(content.record.name)}",
            "Content-Length": str(content.record.size_bytes),
        },
    )


def _request_rate_limit(
    runtime: ControlPlaneRuntime,
    request: Request,
    limiter: InMemoryRateLimiter,
) -> RateLimitDecision | None:
    configured = runtime.config.security.api_rate_limits
    path = request.url.path
    method = request.method
    category: str | None = None
    policy = None
    if method in {"POST", "PUT"} and (
        path == "/api/v1/artifacts"
        or path.startswith("/api/v1/artifact-transfers/")
        and path.endswith("/content")
    ):
        category, policy = "artifact_upload", configured.artifact_upload
    elif method == "POST" and (path == "/api/v1/workflows" or path.endswith("/runs")):
        category, policy = "workflow_creation", configured.workflow_creation
    elif method == "POST" and path == "/api/v1/agents/enroll":
        category, policy = "agent_enrollment", configured.agent_enrollment
    elif method == "GET" and path in {
        "/api/v1/agents",
        "/api/v1/artifacts",
        "/api/v1/audit-events",
        "/api/v1/benches",
        "/api/v1/operations",
    }:
        category, policy = "expensive_search", configured.expensive_search
    if category is None or policy is None:
        return None
    authorization = request.headers.get("Authorization")
    if authorization:
        client_key = "credential:" + hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    else:
        client_key = "client:" + (request.client.host if request.client is not None else "unknown")
    return limiter.check(
        category,
        client_key,
        RateLimitPolicy(
            requests=policy.requests,
            window_seconds=policy.window_seconds,
        ),
    )


def _apply_rate_limit_headers(response: Response, decision: RateLimitDecision) -> None:
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    if not decision.allowed:
        response.headers["Retry-After"] = str(decision.retry_after_seconds)


_LOGGER = logging.getLogger("lab-platform.control-plane.http")

AuthenticatedActor = ApiToken | AuthenticationContext
Phase6ResourceKind = Literal[
    "organisation",
    "agent",
    "bench",
    "workflow",
    "operation",
    "operation-agent",
    "reservation",
]

_PHASE6_SCOPE_PERMISSIONS: dict[ApiTokenScope, str] = {
    ApiTokenScope.AGENTS_READ: "agents:read",
    ApiTokenScope.AGENTS_ADMIN: "agents:manage",
    ApiTokenScope.BENCHES_READ: "benches:read",
    ApiTokenScope.RESERVATIONS_WRITE: "benches:reserve",
    ApiTokenScope.WORKFLOWS_RUN: "workflows:run",
    ApiTokenScope.OPERATIONS_READ: "operations:read",
    ApiTokenScope.ARTIFACTS_READ: "artifacts:read",
    ApiTokenScope.ARTIFACTS_WRITE: "artifacts:write",
    ApiTokenScope.CI_SESSIONS: "ci:sessions:read",
}


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnrollmentTokenRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None
    expires_in_seconds: int | None = Field(default=3600, ge=1, le=604_800)
    allowed_labels: dict[str, str] = Field(default_factory=dict, max_length=128)

    @model_validator(mode="after")
    def one_expiry_source(self) -> "EnrollmentTokenRequest":
        if self.expires_at is not None and self.expires_in_seconds not in {None, 3600}:
            raise ValueError("Set expires_at or expires_in_seconds, not both")
        return self


class AgentEnrollmentRequest(ApiModel):
    enrollment_token: str = Field(min_length=1, max_length=512)
    request_id: UUID = Field(default_factory=uuid4)
    agent_version: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(default=PROTOCOL_VERSION, min_length=1, max_length=100)
    location: str | None = Field(default=None, min_length=1, max_length=200)


class AgentCredentialRotationRequest(ApiModel):
    current_credential: str = Field(min_length=32, max_length=512)


class DrainRequest(ApiModel):
    cancel_queued_work: bool = False


class ApiTokenRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    scopes: set[ApiTokenScope] = Field(min_length=1)
    expires_at: datetime | None = None


class ArtifactTransferRequest(ApiModel):
    agent_id: UUID


class OperationCancelRequest(ApiModel):
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)


class BenchActionRequest(ApiModel):
    owner: str | None = Field(default=None, min_length=1, max_length=200)


class SerialReadActionRequest(BenchActionRequest):
    timeout_seconds: float = Field(default=10, gt=0, le=3600)
    until_pattern: str | None = Field(default=None, max_length=2000)
    max_lines: int | None = Field(default=500, ge=1, le=100_000)


class ReservationCreateRequest(ApiModel):
    bench_id: str = Field(min_length=1, max_length=200)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=500)
    reservation_duration_seconds: int | None = Field(default=None, gt=0, le=86_400)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)
    starts_at: AwareDatetime | None = None
    description: str | None = Field(default=None, min_length=1, max_length=2_000)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=128)


class ReservationRenewRequest(ApiModel):
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    expected_lease_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=500)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)


class ReservationReleaseRequest(ApiModel):
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    expected_lease_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=500)


class ReservationRevokeRequest(ApiModel):
    expected_lease_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=500)


class WorkflowRunRequest(ApiModel):
    version: int | None = Field(default=None, ge=1)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=500)
    inputs: dict[str, object] = Field(default_factory=dict)
    bench_id: str | None = Field(default=None, min_length=1, max_length=200)
    kind: GlobalBenchKind | None = None
    location: str | None = Field(default=None, min_length=1, max_length=200)
    bench_labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    agent_labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    reservation_id: UUID | None = None
    release_reservation_after: bool | None = None
    reservation_duration_seconds: int | None = Field(default=None, gt=0, le=86_400)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)
    command_timeout_seconds: int = Field(default=3_600, gt=0, le=86_400)


class CiSessionCreateRequest(ApiModel):
    provider: CiProvider
    external_run_id: str = Field(min_length=1, max_length=500)
    requested_by: str | None = Field(default=None, min_length=1, max_length=200)
    repository: str | None = Field(default=None, min_length=1, max_length=500)
    ref: str | None = Field(default=None, min_length=1, max_length=500)
    commit_sha: str | None = Field(default=None, min_length=1, max_length=128)
    actor: str | None = Field(default=None, min_length=1, max_length=200)
    bench_request: BenchRequest = Field(default_factory=BenchRequest)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=500)
    timeout_seconds: int | None = Field(default=None, gt=0, le=86_400)


class CiSessionRunRequest(ApiModel):
    workflow_name: str = Field(min_length=1, max_length=200)
    inputs: dict[str, object] = Field(default_factory=dict)
    workflow_version: int | None = Field(
        default=None,
        validation_alias=AliasChoices("workflow_version", "version"),
        ge=1,
    )
    location: str | None = Field(default=None, min_length=1, max_length=200)
    agent_labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)
    command_timeout_seconds: int | None = Field(default=None, gt=0, le=86_400)


def create_app(runtime: ControlPlaneRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="Lab Platform Control Plane API",
        version=VERSION,
        description="Central API and authenticated Agent gateway for distributed lab benches.",
        lifespan=lifespan,
    )
    rate_limiter = InMemoryRateLimiter()

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = request.headers.get("X-Request-ID", str(uuid4()))
        started = time.perf_counter()
        rate_limit = _request_rate_limit(runtime, request, rate_limiter)
        if rate_limit is not None and not rate_limit.allowed:
            limited_response = _error_response(
                request,
                code="API_RATE_LIMIT_EXCEEDED",
                message="The request rate limit was exceeded.",
                details={"retry_after_seconds": rate_limit.retry_after_seconds},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            _apply_rate_limit_headers(limited_response, rate_limit)
            limited_response.headers["X-Request-ID"] = request.state.request_id
            limited_response.headers["X-Response-Time-Ms"] = (
                f"{(time.perf_counter() - started) * 1000:.3f}"
            )
            _apply_security_headers(request, limited_response)
            _observe_request(
                runtime,
                request,
                limited_response,
                started,
                route="/__rate_limited__",
            )
            return limited_response
        maximum_bytes = runtime.config.control_plane.max_request_body_size_mb * 1024 * 1024
        if request.method == "POST" and request.url.path == "/api/v1/artifacts":
            # Multipart framing and metadata have their own small overhead beyond
            # the configured immutable artifact byte limit.
            maximum_bytes = runtime.maximum_artifact_size_bytes + 1024 * 1024
        elif (
            request.method == "POST"
            and request.url.path.startswith("/api/v1/benches/")
            and request.url.path.endswith("/actions/flash")
        ):
            # The firmware file limit is enforced again while streaming below.
            # Keep a bounded allowance here for multipart framing and form fields.
            firmware_limit_mb = min(
                runtime.config.web.uploads.maximum_firmware_size_mb,
                runtime.maximum_artifact_size_bytes // (1024 * 1024),
            )
            maximum_bytes = firmware_limit_mb * 1024 * 1024 + 1024 * 1024
        elif (
            request.method == "PUT"
            and request.url.path.startswith("/api/v1/artifact-transfers/")
            and request.url.path.endswith("/content")
        ):
            maximum_bytes = runtime.maximum_artifact_size_bytes

        raw_length = request.headers.get("Content-Length")
        if raw_length is not None and raw_length.isdecimal() and int(raw_length) > maximum_bytes:
            error_response = await _platform_error_handler(
                request,
                RequestBodyTooLargeError(
                    "Request body exceeds the configured size limit.",
                    maximum_request_bytes=maximum_bytes,
                ),
            )
            error_response.headers["X-Request-ID"] = request.state.request_id
            error_response.headers["X-Response-Time-Ms"] = (
                f"{(time.perf_counter() - started) * 1000:.3f}"
            )
            _apply_security_headers(request, error_response)
            _observe_request(runtime, request, error_response, started, route="/__rejected__")
            return error_response

        original_receive = request.receive
        received_bytes = 0
        request_too_large = False

        async def receive_with_limit() -> Message:
            nonlocal received_bytes, request_too_large
            message = await original_receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > maximum_bytes:
                    request_too_large = True
                    raise RequestBodyTooLargeError(
                        "Request body exceeds the configured size limit.",
                        maximum_request_bytes=maximum_bytes,
                    )
            return message

        request._receive = receive_with_limit
        with runtime.authorisation.request_scope():
            response = await call_next(request)
        if request_too_large:
            response = await _platform_error_handler(
                request,
                RequestBodyTooLargeError(
                    "Request body exceeds the configured size limit.",
                    maximum_request_bytes=maximum_bytes,
                ),
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.3f}"
        if rate_limit is not None:
            _apply_rate_limit_headers(response, rate_limit)
        _apply_security_headers(request, response)
        _observe_request(runtime, request, response, started)
        return response

    app.add_exception_handler(PlatformError, _platform_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _internal_error_handler)
    app.include_router(runtime.gateway.router())
    if runtime.config.identity.enabled:
        app.include_router(create_identity_router(runtime))
        app.include_router(create_identity_admin_router(runtime))

    agents_read = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_READ,
        phase6_permissions=(),
    )
    metrics_read = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_READ,
        phase6_permissions=("agents:read",),
    )
    agent_read = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_READ,
        phase6_resource="agent",
    )
    agents_admin = _require_scopes(runtime, ApiTokenScope.AGENTS_ADMIN)
    agent_manage = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("agents:manage",),
        phase6_resource="agent",
    )
    agent_refresh = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("agents:manage",),
        phase6_resource="agent",
        persist_authorisation_snapshot=True,
    )
    agent_drain = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("agents:drain",),
        phase6_resource="agent",
        persist_authorisation_snapshot=True,
    )
    agents_admin_bootstrap = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        allow_bootstrap=True,
        phase6_permissions=("credentials:create",),
    )
    legacy_tokens_read = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("credentials:create",),
    )
    legacy_tokens_revoke = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("credentials:revoke",),
    )
    benches_read = _require_scopes(
        runtime,
        ApiTokenScope.BENCHES_READ,
        phase6_permissions=(),
    )
    bench_read = _require_scopes(
        runtime,
        ApiTokenScope.BENCHES_READ,
        phase6_resource="bench",
    )
    bench_operate = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("benches:operate",),
        phase6_resource="bench",
    )
    bench_reset = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("benches:reset",),
        phase6_resource="bench",
    )
    bench_serial = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("benches:serial",),
        phase6_resource="bench",
    )
    bench_flash = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        ApiTokenScope.ARTIFACTS_WRITE,
        phase6_permissions=("artifacts:write", "benches:flash"),
        phase6_resource="bench",
    )
    operations_read = _require_scopes(
        runtime,
        ApiTokenScope.OPERATIONS_READ,
        phase6_permissions=(),
    )
    operation_read = _require_scopes(
        runtime,
        ApiTokenScope.OPERATIONS_READ,
        phase6_resource="operation",
    )
    operation_cancel = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("operations:cancel",),
        phase6_resource="operation",
        persist_authorisation_snapshot=True,
    )
    operation_reconcile = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        phase6_permissions=("agents:manage",),
        phase6_resource="operation-agent",
        persist_authorisation_snapshot=True,
    )
    # The protected artifact application service authorises Phase 6 principals
    # against each artifact's durable parent. Legacy tokens retain their Phase
    # 4/5 scope checks here and cross that boundary through an explicit escape.
    artifacts_read = _require_scopes(
        runtime,
        ApiTokenScope.ARTIFACTS_READ,
        phase6_permissions=(),
    )
    artifacts_write = _require_scopes(
        runtime,
        ApiTokenScope.ARTIFACTS_WRITE,
        phase6_permissions=(),
    )
    artifacts_delete = _require_scopes(
        runtime,
        ApiTokenScope.ARTIFACTS_WRITE,
        phase6_permissions=(),
    )
    reservations_write = _require_scopes(
        runtime,
        ApiTokenScope.RESERVATIONS_WRITE,
        phase6_permissions=(),
    )
    reservation_create = _require_reservation_creation(runtime)
    reservation_read = _require_scopes(
        runtime,
        ApiTokenScope.RESERVATIONS_WRITE,
        phase6_permissions=("benches:read",),
        phase6_resource="reservation",
    )
    reservation_write = _require_scopes(
        runtime,
        ApiTokenScope.RESERVATIONS_WRITE,
        phase6_resource="reservation",
    )
    reservation_manage = _require_scopes(
        runtime,
        ApiTokenScope.RESERVATIONS_WRITE,
        phase6_permissions=("benches:manage",),
        phase6_resource="reservation",
    )
    workflows_read = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=(),
    )
    workflow_read = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("workflows:read",),
        phase6_resource="workflow",
    )
    workflow_run = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("workflows:run",),
        phase6_resource="workflow",
    )
    workflow_manage = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        phase6_permissions=("workflows:manage",),
    )
    ci_sessions_read = _require_scopes(
        runtime,
        ApiTokenScope.CI_SESSIONS,
        phase6_permissions=(),
    )
    ci_sessions_create = _require_scopes(
        runtime,
        ApiTokenScope.CI_SESSIONS,
        phase6_permissions=(),
    )
    ci_sessions_cancel = _require_scopes(
        runtime,
        ApiTokenScope.CI_SESSIONS,
        phase6_permissions=(),
    )

    # Dashboard aggregation and live-update routes are registered before the
    # path-style bench detail route so `/benches/{id}/timeline` remains
    # reachable for globally-qualified bench IDs containing a slash.
    app.include_router(
        create_dashboard_router(
            runtime,
            collection_auth=benches_read,
            bench_auth=bench_read,
            operation_auth=operation_read,
        )
    )

    @app.get("/api/v1/version")
    async def version() -> dict[str, object]:
        build = build_metadata()
        return {
            "version": VERSION,
            "api_version": API_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "plugin_api_version": PLUGIN_API_VERSION,
            "release_channel": build.release_channel,
            "edition": runtime.feature_provider.edition,
            "build": build.as_dict(),
        }

    @app.get("/health/live")
    async def liveness() -> dict[str, object]:
        return {"status": "live", "version": VERSION}

    @app.get("/health/ready")
    async def readiness() -> Response:
        report = await runtime.readiness()
        return JSONResponse(
            report,
            status_code=(
                status.HTTP_200_OK
                if report["ready"] is True
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )

    @app.get("/api/v1/health")
    async def health() -> Response:
        """Compatibility alias for dependency-aware readiness."""

        report = await runtime.readiness()
        raw_checks = report.get("checks")
        checks = raw_checks if isinstance(raw_checks, dict) else {}
        component_states = {
            name: (
                "healthy" if isinstance(value, dict) and value.get("ready") is True else "unhealthy"
            )
            for name, value in checks.items()
            if name != "runtime"
        }
        return JSONResponse(
            {
                "status": "healthy" if report["ready"] is True else "starting",
                "ready": report["ready"],
                "version": VERSION,
                "components": component_states,
                "checks": checks,
            },
            status_code=(
                status.HTTP_200_OK
                if report["ready"] is True
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(
        token: Annotated[AuthenticatedActor | None, Depends(metrics_read)],
    ) -> str:
        values = await runtime.metrics(
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return runtime.observability.render_prometheus(values)

    @app.post("/api/v1/tokens", status_code=status.HTTP_201_CREATED)
    async def create_api_token(
        body: ApiTokenRequest,
        _token: Annotated[ApiToken | None, Depends(agents_admin_bootstrap)],
    ) -> dict[str, object]:
        _require_legacy_token_compatibility(runtime)
        if _token is None and not set(ApiTokenScope).issubset(body.scopes):
            raise PermissionDeniedError(
                "The first bootstrap token must grant every supported scope."
            )
        issued = await runtime.token_service.issue(
            name=body.name,
            owner=body.owner,
            scopes=body.scopes,
            expires_at=body.expires_at,
        )
        return {**_public_api_token(issued.record), "token": issued.plaintext}

    @app.get("/api/v1/tokens")
    async def list_api_tokens(
        _token: Annotated[ApiToken | None, Depends(legacy_tokens_read)],
    ) -> dict[str, object]:
        _require_legacy_token_compatibility(runtime)
        return {"items": [_public_api_token(token) for token in await runtime.token_service.list()]}

    @app.post("/api/v1/tokens/{token_id:uuid}/revoke")
    async def revoke_api_token(
        token_id: UUID,
        _token: Annotated[ApiToken | None, Depends(legacy_tokens_revoke)],
    ) -> dict[str, object]:
        _require_legacy_token_compatibility(runtime)
        records = await runtime.token_service.list()
        target = next((record for record in records if record.id == token_id), None)
        if target is not None and _is_active_control_plane_administrator(target):
            remaining = [
                record
                for record in records
                if record.id != token_id and _is_active_control_plane_administrator(record)
            ]
            if not remaining:
                raise PermissionDeniedError(
                    "The last active control-plane administrator cannot be revoked."
                )
        return _public_api_token(await runtime.token_service.revoke(token_id))

    @app.get("/api/v1/agents")
    async def list_agents(
        token: Annotated[AuthenticatedActor | None, Depends(agents_read)],
        agent_status: Annotated[AgentStatus | None, Query(alias="status")] = None,
        location: str | None = None,
        label: Annotated[list[str] | None, Query()] = None,
        version_filter: Annotated[str | None, Query(alias="version")] = None,
    ) -> dict[str, object]:
        agents = await runtime.operational_access.list_agents(
            status=agent_status,
            location=location,
            labels=_parse_labels(label or []),
            version=version_filter,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        benches = await runtime.operational_access.list_visible_benches(
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        counts: dict[UUID, int] = {}
        for bench in benches:
            counts[bench.agent_id] = counts.get(bench.agent_id, 0) + 1
        items: list[dict[str, object]] = []
        for agent in agents:
            upgrade = runtime.assess_agent_compatibility(
                agent.version,
                agent.protocol_version,
            )
            items.append(
                {
                    **agent.model_dump(mode="json"),
                    "bench_count": counts.get(agent.id, 0),
                    "upgrade_status": upgrade.status.value,
                    "upgrade": upgrade.as_dict(),
                }
            )
        return {"items": items}

    @app.get("/api/v1/agents/{agent_id:uuid}")
    async def get_agent(
        agent_id: UUID,
        _token: Annotated[AuthenticatedActor | None, Depends(agent_read)],
    ) -> dict[str, object]:
        agent = await runtime.operational_access.get_agent(
            agent_id,
            authentication_context=(_token if isinstance(_token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(_token, ApiToken),
        )
        connection = await runtime.presence.active_connection(agent_id)
        visible_benches = await runtime.operational_access.list_visible_benches(
            agent_id=agent_id,
            authentication_context=(_token if isinstance(_token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(_token, ApiToken),
        )
        benches = [bench.model_dump(mode="json") for bench in visible_benches]
        operations: list[DistributedOperation] = []
        if not isinstance(_token, ApiToken) or ApiTokenScope.OPERATIONS_READ in _token.scopes:
            try:
                operations = await runtime.operational_access.list_operations(
                    agent_id=agent_id,
                    limit=100,
                    authentication_context=(
                        _token if isinstance(_token, AuthenticationContext) else None
                    ),
                    allow_legacy_authorisation=isinstance(_token, ApiToken),
                )
            except PermissionDeniedError:
                operations = []
        reservations = await runtime.reservation_repository.list(
            organisation_id=_actor_organisation_id(_token),
            agent_id=agent_id,
            limit=500,
        )
        visible_bench_ids = {bench.id for bench in visible_benches}
        reservations = [
            record for record in reservations if record.reservation.bench_id in visible_bench_ids
        ]
        recent_errors = await runtime.timeline.list(
            agent_id,
            severity=AgentTimelineSeverity.ERROR,
            limit=20,
        )
        active_operation_statuses = {
            DistributedOperationStatus.CREATED,
            DistributedOperationStatus.DISPATCHED,
            DistributedOperationStatus.ACCEPTED,
            DistributedOperationStatus.RUNNING,
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.RECONCILING,
        }
        active_reservation_states = {
            ReservationLeaseState.ACTIVATING,
            ReservationLeaseState.ACTIVE,
            ReservationLeaseState.RENEWING,
            ReservationLeaseState.UNKNOWN,
        }
        upgrade = runtime.assess_agent_compatibility(
            agent.version,
            agent.protocol_version,
        )
        return {
            **agent.model_dump(mode="json"),
            "upgrade_status": upgrade.status.value,
            "upgrade": upgrade.as_dict(),
            "connection": (
                connection.connection.model_dump(mode="json") if connection is not None else None
            ),
            "benches": benches,
            "workload": {
                "bench_count": len(benches),
                "active_operations": sum(
                    operation.status in active_operation_statuses for operation in operations
                ),
                "queued_operations": sum(
                    operation.status is DistributedOperationStatus.CREATED
                    for operation in operations
                ),
                "active_reservations": sum(
                    record.state in active_reservation_states for record in reservations
                ),
            },
            "active_operations": [
                operation.model_dump(mode="json", exclude={"result"})
                for operation in operations
                if operation.status in active_operation_statuses
            ],
            "recent_errors": [entry.model_dump(mode="json") for entry in recent_errors],
        }

    @app.post("/api/v1/agents/enrollment-tokens", status_code=status.HTTP_201_CREATED)
    async def create_enrollment_token(
        body: EnrollmentTokenRequest,
        token: Annotated[AuthenticatedActor | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        expiry = body.expires_at or now + timedelta(seconds=body.expires_in_seconds or 3600)
        issued = await runtime.enrollment.issue_token(
            name=body.name,
            expires_at=expiry,
            allowed_labels=body.allowed_labels,
            organisation_id=_actor_organisation_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "AGENT_ENROLLMENT_TOKEN_CREATED",
            resource_type="ENROLLMENT_TOKEN",
            resource_id=str(issued.token.id),
            metadata={"name": issued.token.name},
        )
        return {
            **_dataclass_payload(issued.token),
            "token": issued.plaintext.get_secret_value(),
        }

    @app.get("/api/v1/agents/enrollment-tokens")
    async def list_enrollment_tokens(
        token: Annotated[AuthenticatedActor | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        return {
            "items": [
                _dataclass_payload(item)
                for item in await runtime.enrollment.list_tokens(
                    organisation_id=_actor_organisation_id(token),
                    authentication_context=(
                        token if isinstance(token, AuthenticationContext) else None
                    ),
                    allow_legacy_authorisation=isinstance(token, ApiToken),
                )
            ]
        }

    @app.delete("/api/v1/agents/enrollment-tokens/{token_id}", status_code=204)
    async def revoke_enrollment_token(
        token_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(agents_admin)],
    ) -> Response:
        revoked = await runtime.enrollment.revoke_token(
            token_id,
            organisation_id=_actor_organisation_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "AGENT_ENROLLMENT_TOKEN_REVOKED",
            resource_type="ENROLLMENT_TOKEN",
            resource_id=str(revoked.id),
            metadata={"name": revoked.name},
        )
        return Response(status_code=204)

    @app.post("/api/v1/agents/enroll", status_code=status.HTTP_201_CREATED)
    async def enroll_agent(body: AgentEnrollmentRequest) -> dict[str, object]:
        enrolled = await runtime.enrollment.enroll(
            plaintext_token=body.enrollment_token,
            request_id=body.request_id,
            agent_version=body.agent_version,
            protocol_version=body.protocol_version,
            location=body.location,
        )
        await runtime.authorisation.audit_system_success(
            enrolled.agent.organisation_id,
            "AGENT_ENROLLED",
            resource_type="AGENT",
            resource_id=str(enrolled.agent.id),
            metadata={
                "agent_slug": enrolled.agent.slug,
                "agent_version": enrolled.agent.version,
                "protocol_version": enrolled.agent.protocol_version,
            },
        )
        return {
            "agent": enrolled.agent.model_dump(mode="json"),
            "credential_id": str(enrolled.credential_id),
            "credential_version": enrolled.credential_version,
            "credential": enrolled.plaintext.get_secret_value(),
            "gateway_url": (f"{runtime.config.agent_gateway_url}/{enrolled.agent.id}"),
        }

    @app.post("/api/v1/agents/{agent_id:uuid}/revoke")
    async def revoke_agent(
        agent_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(agent_manage)],
    ) -> object:
        revoked = await runtime.revoke_agent(
            agent_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "AGENT_REVOKED",
            resource_type="AGENT",
            resource_id=str(agent_id),
        )
        return revoked.model_dump(mode="json")

    @app.post("/api/v1/agents/{agent_id:uuid}/credentials/rotate")
    async def rotate_agent_credential(
        agent_id: UUID,
        body: AgentCredentialRotationRequest,
    ) -> dict[str, object]:
        issued = await runtime.rotate_agent_credential(
            agent_id,
            current_secret=body.current_credential,
        )
        return {
            "agent_id": str(issued.agent_id),
            "credential_id": str(issued.credential_id),
            "credential_version": issued.credential_version,
            "credential": issued.plaintext.get_secret_value(),
        }

    @app.post("/api/v1/agents/{agent_id:uuid}/drain")
    async def drain_agent(
        agent_id: UUID,
        body: DrainRequest,
        _token: Annotated[AuthenticatedActor | None, Depends(agent_drain)],
    ) -> dict[str, object]:
        result = await runtime.drain_agent(
            agent_id,
            cancel_queued_work=body.cancel_queued_work,
            authentication_context=(_token if isinstance(_token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(_token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            _token,
            "AGENT_DRAINED",
            resource_type="AGENT",
            resource_id=str(agent_id),
            metadata={"cancelled_queued_work": result.cancelled_queued_work},
        )
        return {
            "agent": result.agent.model_dump(mode="json"),
            "workload": _dataclass_payload(result.workload),
            "cancelled_queued_work": result.cancelled_queued_work,
        }

    @app.post("/api/v1/agents/{agent_id:uuid}/undrain")
    async def undrain_agent(
        agent_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(agent_drain)],
    ) -> object:
        restored = await runtime.undrain_agent(
            agent_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "AGENT_UNDRAINED",
            resource_type="AGENT",
            resource_id=str(agent_id),
        )
        return restored.model_dump(mode="json")

    @app.post("/api/v1/agents/{agent_id:uuid}/actions/refresh-inventory", status_code=202)
    async def refresh_inventory(
        agent_id: UUID,
        _token: Annotated[AuthenticatedActor | None, Depends(agent_refresh)],
    ) -> dict[str, str]:
        if not await runtime.hub.is_connected(agent_id):
            raise AgentOfflineError("Agent is not connected.", agent_id=str(agent_id))
        request_id = await runtime.refresh_inventory(
            agent_id,
            authentication_context=(_token if isinstance(_token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(_token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            _token,
            "INVENTORY_REFRESH_REQUESTED",
            resource_type="AGENT",
            resource_id=str(agent_id),
            metadata={"request_id": str(request_id)},
        )
        return {"request_id": str(request_id)}

    @app.get("/api/v1/agents/{agent_id:uuid}/timeline")
    async def agent_timeline(
        agent_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(agent_read)],
        severity: AgentTimelineSeverity | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        await runtime.operational_access.get_agent(
            agent_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        entries = await runtime.timeline.list(
            agent_id,
            severity=severity,
            event_type=event_type,
            since=since,
            limit=limit,
        )
        return {"items": [entry.model_dump(mode="json") for entry in entries]}

    @app.get("/api/v1/benches")
    async def list_benches(
        token: Annotated[AuthenticatedActor | None, Depends(benches_read)],
        agent_id: UUID | None = None,
        location: str | None = None,
        capability: str | None = None,
        label: Annotated[list[str] | None, Query()] = None,
        agent_label: Annotated[list[str] | None, Query()] = None,
        bench_status: Annotated[GlobalBenchStatus | None, Query(alias="status")] = None,
        kind: GlobalBenchKind | None = None,
        health_filter: Annotated[HealthStatus | None, Query(alias="health")] = None,
        online: bool | None = None,
    ) -> dict[str, object]:
        benches = await runtime.operational_access.list_benches(
            agent_id=agent_id,
            location=location,
            agent_labels=_parse_labels(agent_label or []),
            status=bench_status,
            kind=kind,
            health=health_filter,
            capability=capability,
            labels=_parse_labels(label or []),
            online=online,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        items = await enrich_benches(runtime, token, benches)
        return {"items": items, "total": len(items)}

    @app.post("/api/v1/benches/{bench_id:path}/actions/probe", status_code=202)
    async def probe_bench(
        bench_id: str,
        body: BenchActionRequest,
        token: Annotated[AuthenticatedActor | None, Depends(bench_operate)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        bench = await _action_bench(
            runtime,
            bench_id,
            capability="probe",
            organisation_id=organisation_id,
        )
        owner, _owner_principal = _reservation_identity(token, body.owner)
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.PROBE,
            payload={"owner": owner},
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            idempotency_key=idempotency_key or f"bench-probe:{uuid4()}",
            operation_type=RemoteCommandType.PROBE.value,
            actor_context=_actor_context(token),
            authorisation_snapshot_id=_authorisation_snapshot_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        response = _remote_operation_payload(command, operation)
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_PROBE_REQUESTED",
            resource_type="BENCH",
            resource_id=bench.id,
            metadata={"command_id": str(command.id), "operation_id": response["operation_id"]},
        )
        return response

    @app.post("/api/v1/benches/{bench_id:path}/actions/reset", status_code=202)
    async def reset_bench(
        bench_id: str,
        body: BenchActionRequest,
        token: Annotated[AuthenticatedActor | None, Depends(bench_reset)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        bench = await _action_bench(
            runtime,
            bench_id,
            capability="reset",
            organisation_id=organisation_id,
        )
        owner, owner_principal = _reservation_identity(token, body.owner)
        reservation = await _action_reservation(
            runtime,
            bench.id,
            owner,
            owner_principal=owner_principal,
            organisation_id=organisation_id,
            actor=token,
            required_permission="benches:reset",
        )
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.RESET,
            payload={"owner": owner},
            expires_at=min(
                datetime.now(UTC) + timedelta(minutes=5),
                reservation.lease.valid_until,
            ),
            idempotency_key=idempotency_key or f"bench-reset:{uuid4()}",
            reservation_lease=reservation.lease,
            operation_type=RemoteCommandType.RESET.value,
            actor_context=_actor_context(token),
            authorisation_snapshot_id=_authorisation_snapshot_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        response = _remote_operation_payload(command, operation)
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_RESET_REQUESTED",
            resource_type="BENCH",
            resource_id=bench.id,
            metadata={"command_id": str(command.id), "operation_id": response["operation_id"]},
        )
        return response

    @app.post("/api/v1/benches/{bench_id:path}/actions/read-serial", status_code=202)
    async def read_bench_serial(
        bench_id: str,
        body: SerialReadActionRequest,
        token: Annotated[AuthenticatedActor | None, Depends(bench_serial)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        bench = await _action_bench(
            runtime,
            bench_id,
            capability="serial",
            organisation_id=_actor_organisation_id(token),
        )
        serial_request = SerialReadRequest(
            timeout_seconds=body.timeout_seconds,
            until_pattern=body.until_pattern,
            max_lines=body.max_lines,
        )
        owner, _owner_principal = _reservation_identity(token, body.owner)
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.READ_SERIAL,
            payload={
                "owner": owner,
                "request": serial_request.model_dump(mode="json"),
            },
            expires_at=(
                datetime.now(UTC)
                + timedelta(seconds=max(60.0, serial_request.timeout_seconds + 30.0))
            ),
            idempotency_key=idempotency_key or f"bench-read-serial:{uuid4()}",
            operation_type=RemoteCommandType.READ_SERIAL.value,
            actor_context=_actor_context(token),
            authorisation_snapshot_id=_authorisation_snapshot_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        response = _remote_operation_payload(command, operation)
        await _audit_protected_success(
            runtime,
            token,
            "SERIAL_ACCESS_REQUESTED",
            resource_type="BENCH",
            resource_id=bench.id,
            metadata={"command_id": str(command.id), "operation_id": response["operation_id"]},
        )
        return response

    @app.post("/api/v1/benches/{bench_id:path}/actions/flash", status_code=202)
    async def flash_bench(
        bench_id: str,
        firmware: Annotated[UploadFile, File()],
        token: Annotated[AuthenticatedActor | None, Depends(bench_flash)],
        owner: Annotated[str | None, Form(min_length=1, max_length=200)] = None,
        version: Annotated[str | None, Form(min_length=1, max_length=200)] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        bench = await _action_bench(
            runtime,
            bench_id,
            capability="firmware",
            organisation_id=organisation_id,
        )
        owner, owner_principal = _reservation_identity(token, owner)
        reservation = await _action_reservation(
            runtime,
            bench.id,
            owner,
            owner_principal=owner_principal,
            organisation_id=organisation_id,
            actor=token,
            required_permission="benches:flash",
        )
        command_key = idempotency_key or f"bench-flash:{uuid4()}"
        existing = await runtime.command_repository.get_by_idempotency_key(
            bench.agent_id,
            command_key,
            organisation_id=organisation_id,
        )
        if existing is not None:
            operation = await runtime.command_repository.get_operation_for_command(
                existing.id,
                organisation_id=organisation_id,
            )
            return _remote_operation_payload(existing, operation)

        maximum_firmware_bytes = (
            min(
                runtime.config.web.uploads.maximum_firmware_size_mb,
                runtime.config.artifacts.max_upload_size_mb,
            )
            * 1024
            * 1024
        )

        async def chunks() -> AsyncIterator[bytes]:
            received_bytes = 0
            while chunk := await firmware.read(1024 * 1024):
                received_bytes += len(chunk)
                if received_bytes > maximum_firmware_bytes:
                    raise ArtifactTooLargeError(
                        "Firmware exceeds the configured upload limit.",
                        maximum_size_bytes=maximum_firmware_bytes,
                    )
                yield chunk

        operation_id = uuid4()
        artifact = await runtime.artifact_access.upload_for_operation_route(
            chunks(),
            operation_id=operation_id,
            agent_id=bench.agent_id,
            bench_id=bench.id,
            name=firmware.filename or "firmware.bin",
            artifact_type="firmware",
            content_type=firmware.content_type,
            metadata={"bench_id": bench.id, "owner": owner},
            idempotency_key=f"{command_key}:firmware",
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        descriptor = await runtime.workflow_artifacts.issue_download(
            agent_id=bench.agent_id,
            input_name="firmware",
            artifact_id=artifact.id,
            target_path=f"artifacts/{artifact.id}",
            idempotency_key=f"{command_key}:transfer",
            organisation_id=organisation_id,
            allow_internal_authorisation=True,
        )
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.FLASH,
            payload={
                "owner": owner,
                "version": version,
                "artifact": descriptor.as_payload(),
            },
            expires_at=min(
                datetime.now(UTC) + timedelta(hours=1),
                reservation.lease.valid_until,
            ),
            idempotency_key=command_key,
            reservation_lease=reservation.lease,
            operation_type=RemoteCommandType.FLASH.value,
            operation_id=operation_id,
            actor_context=_actor_context(token),
            authorisation_snapshot_id=_authorisation_snapshot_id(token),
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        response = _remote_operation_payload(command, operation)
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_FLASH_REQUESTED",
            resource_type="BENCH",
            resource_id=bench.id,
            metadata={
                "command_id": str(command.id),
                "operation_id": response["operation_id"],
                "artifact_id": str(artifact.id),
            },
        )
        response["input_artifact"] = artifact.model_dump(mode="json")
        return response

    @app.get("/api/v1/benches/{bench_id:path}")
    async def get_bench(
        bench_id: str,
        token: Annotated[AuthenticatedActor | None, Depends(bench_read)],
    ) -> object:
        bench = await runtime.operational_access.get_bench(
            bench_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return (await enrich_benches(runtime, token, [bench]))[0]

    @app.post("/api/v1/reservations", status_code=status.HTTP_201_CREATED)
    async def create_reservation(
        body: ReservationCreateRequest,
        token: Annotated[AuthenticatedActor | None, Depends(reservation_create)],
    ) -> dict[str, object]:
        bench = await runtime.inventory_repository.get(
            body.bench_id,
            organisation_id=_actor_organisation_id(token),
        )
        if bench is None:
            raise BenchNotFoundError("Bench does not exist.", bench_id=body.bench_id)
        owner, owner_principal = _reservation_identity(token, body.owner)
        metadata = dict(body.metadata)
        if body.description is not None:
            metadata["description"] = body.description
        if body.starts_at is not None:
            record: CoordinatedReservationLease | Reservation = await runtime.reservations.schedule(
                agent_id=bench.agent_id,
                bench_id=bench.id,
                owner=owner,
                owner_principal=owner_principal,
                starts_at=body.starts_at,
                idempotency_key=body.idempotency_key,
                reservation_duration_seconds=body.reservation_duration_seconds,
                lease_ttl_seconds=body.lease_ttl_seconds,
                metadata=metadata,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
            )
        else:
            record = await runtime.reservations.grant(
                agent_id=bench.agent_id,
                bench_id=bench.id,
                owner=owner,
                owner_principal=owner_principal,
                idempotency_key=body.idempotency_key,
                reservation_duration_seconds=body.reservation_duration_seconds,
                lease_ttl_seconds=body.lease_ttl_seconds,
                metadata=metadata,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
            )
            _require_active_reservation(record)
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_RESERVED",
            resource_type="BENCH",
            resource_id=bench.id,
            metadata={"reservation_id": str(_reservation_value(record).id)},
        )
        return await _reservation_presentation_payload(runtime, token, record)

    @app.get("/api/v1/reservations")
    async def list_reservations(
        token: Annotated[AuthenticatedActor | None, Depends(reservations_write)],
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        owner: str | None = None,
        lease_state: Annotated[
            list[ReservationLeaseState] | None,
            Query(alias="state"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        records = await runtime.reservation_repository.list(
            organisation_id=_actor_organisation_id(token),
            agent_id=agent_id,
            states=lease_state,
            limit=limit,
        )
        scheduled = (
            await runtime.reservations.list_scheduled(
                organisation_id=_actor_organisation_id(token),
                bench_id=bench_id,
                owner=owner,
                limit=limit,
            )
            if lease_state is None
            else []
        )
        scheduled_agent_ids: dict[UUID, UUID] = {}
        visible_scheduled: list[Reservation] = []
        for reservation in scheduled:
            scheduled_bench = await runtime.inventory_repository.get(
                reservation.bench_id,
                organisation_id=_actor_organisation_id(token),
            )
            if scheduled_bench is None or (
                agent_id is not None and scheduled_bench.agent_id != agent_id
            ):
                continue
            scheduled_agent_ids[reservation.id] = scheduled_bench.agent_id
            visible_scheduled.append(reservation)
        scheduled = visible_scheduled
        if bench_id is not None:
            records = [record for record in records if record.reservation.bench_id == bench_id]
        if owner is not None:
            records = [record for record in records if record.reservation.owner == owner]
        combined: list[CoordinatedReservationLease | Reservation] = [*records, *scheduled]
        allowed = await _phase6_allowed_resources(
            runtime,
            token,
            "benches:read",
            [
                AuthorisationResource(
                    type=ResourceType.BENCH,
                    id=_reservation_value(record).bench_id,
                    organisation_id=_reservation_value(record).organisation_id,
                    parent_agent_id=(
                        record.lease.agent_id
                        if isinstance(record, CoordinatedReservationLease)
                        else scheduled_agent_ids[record.id]
                    ),
                )
                for record in combined
            ],
        )
        combined = [record for record, visible in zip(combined, allowed, strict=True) if visible]
        combined.sort(
            key=lambda item: (
                _reservation_value(item).starts_at or _reservation_value(item).created_at
            ),
            reverse=True,
        )
        presented = await asyncio.gather(
            *(
                _reservation_presentation_payload(
                    runtime,
                    token,
                    record,
                    parent_agent_id=scheduled_agent_ids.get(_reservation_value(record).id),
                )
                for record in combined[:limit]
            )
        )
        return {"items": list(presented)}

    @app.get("/api/v1/reservations/{reservation_id}")
    async def get_reservation(
        reservation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(reservation_read)],
    ) -> dict[str, object]:
        record = await runtime.reservation_repository.get(
            reservation_id,
            organisation_id=_actor_organisation_id(token),
        )
        if record is None:
            uncoordinated = await runtime.reservations.get_uncoordinated(
                reservation_id,
                organisation_id=_actor_organisation_id(token),
            )
            if uncoordinated is None:
                raise ReservationNotFoundError("Reservation does not exist.")
            return await _reservation_presentation_payload(runtime, token, uncoordinated)
        return await _reservation_presentation_payload(runtime, token, record)

    @app.post("/api/v1/reservations/{reservation_id}/renew")
    async def renew_reservation(
        reservation_id: UUID,
        body: ReservationRenewRequest,
        token: Annotated[AuthenticatedActor | None, Depends(reservation_write)],
    ) -> dict[str, object]:
        owner, owner_principal = _reservation_identity(token, body.owner)
        record = await runtime.reservations.renew(
            reservation_id,
            owner=owner,
            owner_principal=owner_principal,
            expected_lease_version=body.expected_lease_version,
            idempotency_key=body.idempotency_key,
            lease_ttl_seconds=body.lease_ttl_seconds,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        _require_active_reservation(record)
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_RESERVATION_RENEWED",
            resource_type="BENCH",
            resource_id=record.reservation.bench_id,
            metadata={
                "reservation_id": str(record.reservation.id),
                "lease_version": record.lease.lease_version,
            },
        )
        return _coordinated_reservation_payload(record)

    @app.post("/api/v1/reservations/{reservation_id}/release")
    async def release_reservation(
        reservation_id: UUID,
        body: ReservationReleaseRequest,
        token: Annotated[AuthenticatedActor | None, Depends(reservation_write)],
    ) -> dict[str, object]:
        owner, owner_principal = _reservation_identity(token, body.owner)
        scheduled = await runtime.reservations.get_uncoordinated(
            reservation_id,
            organisation_id=_actor_organisation_id(token),
        )
        if scheduled is not None and scheduled.status in {
            ReservationStatus.SCHEDULED,
            ReservationStatus.CANCELLED,
        }:
            cancelled = await runtime.reservations.cancel_scheduled(
                reservation_id,
                owner=owner,
                owner_principal=owner_principal,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
            )
            await _audit_protected_success(
                runtime,
                token,
                "BENCH_RELEASED",
                resource_type="BENCH",
                resource_id=cancelled.bench_id,
                metadata={"reservation_id": str(cancelled.id), "scheduled": True},
            )
            return _reservation_payload(cancelled)
        if body.expected_lease_version is None:
            raise ReservationLeaseInvalidError(
                "expected_lease_version is required after a reservation activates.",
                reservation_id=str(reservation_id),
            )
        record = await runtime.reservations.release(
            reservation_id,
            owner=owner,
            owner_principal=owner_principal,
            expected_lease_version=body.expected_lease_version,
            idempotency_key=body.idempotency_key,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_RELEASED",
            resource_type="BENCH",
            resource_id=record.reservation.bench_id,
            metadata={"reservation_id": str(record.reservation.id)},
        )
        return _coordinated_reservation_payload(record)

    @app.post("/api/v1/reservations/{reservation_id}/revoke")
    async def revoke_reservation(
        reservation_id: UUID,
        body: ReservationRevokeRequest,
        token: Annotated[AuthenticatedActor | None, Depends(reservation_manage)],
    ) -> dict[str, object]:
        scheduled = await runtime.reservations.get_uncoordinated(
            reservation_id,
            organisation_id=_actor_organisation_id(token),
        )
        if scheduled is not None and scheduled.status in {
            ReservationStatus.SCHEDULED,
            ReservationStatus.CANCELLED,
        }:
            cancelled = await runtime.reservations.cancel_scheduled(
                reservation_id,
                administrator=True,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
            )
            await _audit_protected_success(
                runtime,
                token,
                "BENCH_RESERVATION_REVOKED",
                resource_type="BENCH",
                resource_id=cancelled.bench_id,
                metadata={"reservation_id": str(cancelled.id), "scheduled": True},
            )
            return _reservation_payload(cancelled)
        if body.expected_lease_version is None:
            raise ReservationLeaseInvalidError(
                "expected_lease_version is required after a reservation activates.",
                reservation_id=str(reservation_id),
            )
        record = await runtime.reservations.revoke(
            reservation_id,
            expected_lease_version=body.expected_lease_version,
            idempotency_key=body.idempotency_key,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "BENCH_RESERVATION_REVOKED",
            resource_type="BENCH",
            resource_id=record.reservation.bench_id,
            metadata={"reservation_id": str(record.reservation.id)},
        )
        return _coordinated_reservation_payload(record)

    @app.post("/api/v1/workflows", status_code=status.HTTP_201_CREATED)
    async def register_workflow(
        definition: WorkflowDefinition,
        token: Annotated[AuthenticatedActor | None, Depends(workflow_manage)],
    ) -> object:
        stored = await runtime.operational_access.save_workflow(
            definition,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return stored.model_dump(mode="json")

    @app.get("/api/v1/workflows")
    async def list_workflows(
        token: Annotated[AuthenticatedActor | None, Depends(workflows_read)],
    ) -> dict[str, object]:
        definitions = await runtime.operational_access.list_workflows(
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return {"items": [definition.model_dump(mode="json") for definition in definitions]}

    @app.get("/api/v1/workflows/{workflow_name}")
    async def get_workflow(
        workflow_name: str,
        token: Annotated[AuthenticatedActor | None, Depends(workflow_read)],
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> object:
        definition = await runtime.operational_access.get_workflow(
            workflow_name,
            version,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return definition.model_dump(mode="json")

    @app.post("/api/v1/workflows/{workflow_name}/runs", status_code=202)
    async def run_workflow(
        workflow_name: str,
        body: WorkflowRunRequest,
        token: Annotated[AuthenticatedActor | None, Depends(workflow_run)],
    ) -> dict[str, object]:
        runtime.require_workflow_capacity()
        if (
            isinstance(token, ApiToken)
            and body.release_reservation_after is True
            and ApiTokenScope.RESERVATIONS_WRITE not in token.scopes
        ):
            raise PermissionDeniedError(
                "Releasing an existing reservation requires reservations:write scope."
            )
        definition = await runtime.workflow_repository.get_definition(
            workflow_name,
            body.version,
            organisation_id=_actor_organisation_id(token),
        )
        if definition is None:
            raise WorkflowNotFoundError(
                "Workflow does not exist.",
                workflow_name=workflow_name,
                workflow_version=body.version,
            )
        owner, owner_principal = _reservation_identity(token, body.owner)
        dispatch = await runtime.workflows.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner=owner,
                idempotency_key=body.idempotency_key,
                owner_principal=owner_principal,
                actor_context=_actor_context(token),
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                organisation_id=_actor_organisation_id(token),
                allow_legacy_authorisation=isinstance(token, ApiToken),
                inputs=body.inputs,
                bench_id=body.bench_id,
                kind=body.kind,
                location=body.location,
                bench_labels=body.bench_labels,
                agent_labels=body.agent_labels,
                reservation_id=body.reservation_id,
                release_reservation_after=body.release_reservation_after,
                reservation_duration_seconds=body.reservation_duration_seconds,
                lease_ttl_seconds=body.lease_ttl_seconds,
                command_timeout_seconds=body.command_timeout_seconds,
            )
        )
        return {
            "agent": dispatch.agent.model_dump(mode="json"),
            "bench": dispatch.bench.model_dump(mode="json"),
            "definition": dispatch.definition.model_dump(mode="json"),
            "inputs": dispatch.inputs,
            "reservation": _coordinated_reservation_payload(dispatch.reservation),
            "artifact_transfers": [
                {
                    "input_name": transfer.input_name,
                    "artifact_id": str(transfer.artifact_id),
                    "sha256": transfer.sha256,
                    "size_bytes": transfer.size_bytes,
                    "target_path": transfer.target_path,
                }
                for transfer in dispatch.artifact_transfers
            ],
            "command": dispatch.command.model_dump(mode="json", exclude={"payload"}),
            "operation": dispatch.operation.model_dump(mode="json"),
        }

    @app.get("/api/v1/workflow-runs/{operation_id:uuid}")
    async def get_distributed_workflow_run(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_read)],
    ) -> dict[str, object]:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        command = await runtime.command_records.get(
            operation.remote_command_id,
            organisation_id=operation.organisation_id,
        )
        return _distributed_workflow_payload(operation, command)

    @app.post("/api/v1/workflow-runs/{operation_id:uuid}/cancel", status_code=202)
    async def cancel_distributed_workflow_run(
        operation_id: UUID,
        body: OperationCancelRequest,
        token: Annotated[AuthenticatedActor | None, Depends(operation_cancel)],
    ) -> dict[str, object]:
        return await _cancel_distributed_operation(
            runtime,
            operation_id,
            actor=token,
            requested_owner=body.owner,
            reason=body.reason,
            audit_action="WORKFLOW_CANCELLED",
        )

    @app.get("/api/v1/workflow-runs/{operation_id:uuid}/results")
    async def distributed_workflow_results(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_read)],
    ) -> dict[str, object]:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        workflow, steps = _distributed_workflow_result(operation)
        results = build_test_results(steps)
        return {
            "workflow_run_id": str(operation.id),
            "local_workflow_run_id": workflow.get("id"),
            "workflow_name": workflow.get("workflow_name") or operation.operation_type,
            "status": _distributed_workflow_status(operation),
            "results": [item.model_dump(mode="json") for item in results],
            "steps": [step.model_dump(mode="json") for step in steps],
        }

    @app.get(
        "/api/v1/workflow-runs/{operation_id:uuid}/results/junit",
        response_class=Response,
    )
    async def distributed_workflow_results_junit(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_read)],
    ) -> Response:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        workflow, steps = _distributed_workflow_result(operation)
        suite_name = str(workflow.get("workflow_name") or operation.operation_type)
        return Response(
            content=render_junit_xml(suite_name, build_test_results(steps)),
            media_type="application/xml",
        )

    @app.post("/api/v1/ci/sessions", status_code=status.HTTP_201_CREATED)
    async def create_ci_session(
        body: CiSessionCreateRequest,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_create)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        runtime.require_ci_capacity()
        owner_principal: ReservationOwner | None = None
        actor_context: ActorContext | None = None
        requested_by: str | None
        if isinstance(token, AuthenticationContext):
            principal = token.principal
            requested_by = principal.display_name
            owner_principal = ReservationOwner(
                principal_id=principal.id,
                principal_type=principal.type,
                display_name=principal.display_name,
            )
            actor_context = _actor_context(token)
        else:
            requested_by = body.requested_by or _principal_display_name(token)
        if requested_by is None:
            requested_by = body.actor or f"ci:{body.provider.value}:{body.external_run_id}"
        session = await runtime.ci.create(
            DistributedCiCreateRequest(
                provider=body.provider,
                external_run_id=body.external_run_id,
                requested_by=requested_by,
                owner_principal=owner_principal,
                actor_context=actor_context,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
                bench_request=body.bench_request,
                repository=body.repository,
                ref=body.ref,
                commit_sha=body.commit_sha,
                actor=body.actor,
                idempotency_key=idempotency_key or body.idempotency_key,
                timeout_seconds=body.timeout_seconds,
            )
        )
        await _audit_protected_success(
            runtime,
            token,
            "CI_SESSION_CREATED",
            resource_type="CI_SESSION",
            resource_id=str(session.id),
            metadata={"provider": session.provider.value, "status": session.status.value},
        )
        return await _ci_session_payload(
            runtime,
            session,
            organisation_id=_actor_organisation_id(token),
        )

    @app.get("/api/v1/ci/sessions")
    async def list_ci_sessions(
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_read)],
        session_status: Annotated[CiSessionStatus | None, Query(alias="status")] = None,
        provider: CiProvider | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        sessions = await runtime.operational_access.list_ci_sessions(
            status=session_status,
            provider=provider,
            limit=limit,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return {
            "items": [
                await _ci_session_payload(
                    runtime,
                    session,
                    organisation_id=_actor_organisation_id(token),
                )
                for session in sessions
            ]
        }

    @app.get("/api/v1/ci/sessions/{session_id:uuid}")
    async def get_ci_session(
        session_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_read)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        session = await runtime.operational_access.get_ci_session(
            session_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return await _ci_session_payload(
            runtime,
            session,
            details=True,
            organisation_id=organisation_id,
        )

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/run", status_code=202)
    async def run_ci_session(
        session_id: UUID,
        body: CiSessionRunRequest,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_create)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        existing = await runtime.ci.get(
            session_id,
            synchronize=False,
            organisation_id=organisation_id,
        )
        await _require_ci_session_access(
            runtime,
            token,
            existing,
            "ci:sessions:create",
        )
        session = await runtime.ci.start(
            session_id,
            DistributedCiStartRequest(
                workflow_name=body.workflow_name,
                inputs=body.inputs,
                workflow_version=body.workflow_version,
                location=body.location,
                agent_labels=body.agent_labels,
                lease_ttl_seconds=body.lease_ttl_seconds,
                command_timeout_seconds=body.command_timeout_seconds,
            ),
            organisation_id=organisation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        binding = await runtime.ci_repository.get_distributed_workflow(
            session.id,
            organisation_id=organisation_id,
        )
        await _audit_protected_success(
            runtime,
            token,
            "CI_SESSION_STARTED",
            resource_type="CI_SESSION",
            resource_id=str(session.id),
            metadata={
                "status": session.status.value,
                "remote_command_id": (
                    str(binding.remote_command_id) if binding is not None else None
                ),
                "operation_id": str(binding.operation_id) if binding is not None else None,
            },
        )
        return await _ci_session_payload(
            runtime,
            session,
            details=True,
            organisation_id=organisation_id,
        )

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/heartbeat")
    async def heartbeat_ci_session(
        session_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_create)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        existing = await runtime.ci.get(
            session_id,
            synchronize=False,
            organisation_id=organisation_id,
        )
        await _require_ci_session_access(
            runtime,
            token,
            existing,
            "ci:sessions:create",
        )
        session = await runtime.ci.heartbeat(
            session_id,
            organisation_id=organisation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "CI_SESSION_HEARTBEAT_RECORDED",
            resource_type="CI_SESSION",
            resource_id=str(session.id),
            metadata={"status": session.status.value},
        )
        return await _ci_session_payload(
            runtime,
            session,
            organisation_id=organisation_id,
        )

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/cancel")
    async def cancel_ci_session(
        session_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_cancel)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        existing = await runtime.ci.get(
            session_id,
            synchronize=False,
            organisation_id=organisation_id,
        )
        await _require_ci_session_access(
            runtime,
            token,
            existing,
            "ci:sessions:cancel",
        )
        session = await runtime.ci.cancel(
            session_id,
            organisation_id=organisation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "CI_SESSION_CANCELLED",
            resource_type="CI_SESSION",
            resource_id=str(session.id),
            metadata={"status": session.status.value, "outcome": session.outcome.value},
        )
        return await _ci_session_payload(
            runtime,
            session,
            details=True,
            organisation_id=organisation_id,
        )

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/finalize")
    async def finalize_ci_session(
        session_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_cancel)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        existing = await runtime.ci.get(
            session_id,
            synchronize=False,
            organisation_id=organisation_id,
        )
        await _require_ci_session_access(
            runtime,
            token,
            existing,
            "ci:sessions:cancel",
        )
        session = await runtime.ci.cleanup(
            session_id,
            organisation_id=organisation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "CI_SESSION_FINALIZED",
            resource_type="CI_SESSION",
            resource_id=str(session.id),
            metadata={
                "status": session.status.value,
                "cleanup_status": session.cleanup_status.value,
            },
        )
        return await _ci_session_payload(
            runtime,
            session,
            details=True,
            organisation_id=organisation_id,
        )

    @app.get("/api/v1/ci/sessions/{session_id:uuid}/artifacts")
    async def list_ci_session_artifacts(
        session_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(ci_sessions_read)],
    ) -> dict[str, object]:
        organisation_id = _actor_organisation_id(token)
        await runtime.operational_access.get_ci_session(
            session_id,
            synchronize=False,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        binding = await runtime.ci_repository.get_distributed_workflow(
            session_id,
            organisation_id=organisation_id,
        )
        collection = await runtime.artifact_access.list(
            owner_type=ArtifactOwnerType.CI_SESSION,
            owner_id=session_id,
            command_id=(binding.remote_command_id if binding is not None else None),
            include_remote=binding is not None,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        items = [item.model_dump(mode="json", exclude={"path"}) for item in collection.platform]
        items.extend(item.model_dump(mode="json") for item in collection.remote)
        items.sort(key=lambda item: str(item.get("created_at", "")))
        return {"items": items}

    @app.get("/api/v1/operations")
    async def list_operations(
        token: Annotated[AuthenticatedActor | None, Depends(operations_read)],
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        operation_status: Annotated[
            DistributedOperationStatus | None, Query(alias="status")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        operations = await runtime.operational_access.list_operations(
            agent_id=agent_id,
            bench_id=bench_id,
            status=operation_status,
            limit=limit,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return {"items": [item.model_dump(mode="json", exclude={"result"}) for item in operations]}

    @app.get("/api/v1/operations/{operation_id}")
    async def get_operation(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_read)],
    ) -> object:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        return operation.model_dump(mode="json")

    @app.get("/api/v1/operations/{operation_id}/artifacts/serial")
    async def get_operation_serial_output(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_read)],
    ) -> dict[str, str]:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        result = operation.result or {}
        raw_lines = result.get("lines")
        if not isinstance(raw_lines, list):
            raise ArtifactNotFoundError(
                "The distributed operation has no synchronized serial output.",
                operation_id=str(operation_id),
            )
        lines = [str(item.get("text", "")) for item in raw_lines if isinstance(item, dict)]
        return {"text": "".join(f"{line}\n" for line in lines)}

    @app.post("/api/v1/operations/{operation_id}/cancel", status_code=202)
    async def cancel_operation(
        operation_id: UUID,
        body: OperationCancelRequest,
        token: Annotated[AuthenticatedActor | None, Depends(operation_cancel)],
    ) -> dict[str, object]:
        return await _cancel_distributed_operation(
            runtime,
            operation_id,
            actor=token,
            requested_owner=body.owner,
            reason=body.reason,
            audit_action="OPERATION_CANCELLED",
        )

    @app.post("/api/v1/operations/{operation_id}/reconcile", status_code=202)
    async def reconcile_operation(
        operation_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(operation_reconcile)],
    ) -> dict[str, str]:
        operation = await runtime.operation_records.get(
            operation_id,
            organisation_id=_actor_organisation_id(token),
        )
        if operation is None:
            raise RemoteCommandNotFoundError("Distributed operation does not exist.")
        if not await runtime.hub.is_connected(operation.agent_id):
            raise AgentOfflineError("Agent is not connected.", agent_id=str(operation.agent_id))
        request_id = await runtime.request_reconciliation(
            operation.agent_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "OPERATION_RECONCILIATION_REQUESTED",
            resource_type="OPERATION",
            resource_id=str(operation.id),
            metadata={"agent_id": str(operation.agent_id), "request_id": str(request_id)},
        )
        return {"request_id": str(request_id)}

    @app.post("/api/v1/artifacts", status_code=status.HTTP_201_CREATED)
    async def upload_artifact(
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_write)],
        file: Annotated[UploadFile, File()],
        owner_type: Annotated[ArtifactOwnerType, Form()],
        owner_id: Annotated[UUID, Form()],
        artifact_type: Annotated[str, Form(min_length=1, max_length=200)],
        expected_sha256: Annotated[str | None, Form()] = None,
        idempotency_key: Annotated[str | None, Form(max_length=500)] = None,
    ) -> object:
        async def chunks() -> AsyncIterator[bytes]:
            received = 0
            log_limit_mb = runtime.config.resource_limits.maximum_log_artifact_size_mb
            log_limit_bytes = log_limit_mb * 1024 * 1024 if log_limit_mb is not None else None
            while chunk := await file.read(1024 * 1024):
                received += len(chunk)
                if (
                    log_limit_bytes is not None
                    and "log" in artifact_type.casefold().replace("-", "_")
                    and received > log_limit_bytes
                ):
                    raise ArtifactTooLargeError(
                        "Log artifact exceeds the configured upload limit.",
                        maximum_size_bytes=log_limit_bytes,
                    )
                yield chunk

        record = await runtime.artifact_access.upload(
            chunks(),
            owner_type=owner_type,
            owner_id=owner_id,
            name=file.filename or "artifact.bin",
            artifact_type=artifact_type,
            content_type=file.content_type,
            expected_sha256=expected_sha256,
            idempotency_key=idempotency_key,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "ARTIFACT_UPLOADED",
            resource_type="ARTIFACT",
            resource_id=str(record.id),
            metadata={"owner_type": owner_type.value, "owner_id": str(owner_id)},
        )
        return record.model_dump(mode="json", exclude={"path"})

    @app.get("/api/v1/artifacts")
    async def list_artifacts(
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_read)],
        owner_type: ArtifactOwnerType | None = None,
        owner_id: UUID | None = None,
        agent_id: UUID | None = None,
        command_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        if (owner_type is None) != (owner_id is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="owner_type and owner_id must be supplied together",
            )
        collection = await runtime.artifact_access.list(
            owner_type=owner_type,
            owner_id=owner_id,
            agent_id=agent_id,
            command_id=command_id,
            limit=limit,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        items = [item.model_dump(mode="json", exclude={"path"}) for item in collection.platform]
        items.extend(item.model_dump(mode="json") for item in collection.remote)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {"items": items[:limit]}

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_read)],
        download: bool = False,
    ) -> object:
        record = await runtime.artifact_access.get(
            artifact_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        if download:
            content = await runtime.artifact_access.content(
                artifact_id,
                authentication_context=(
                    token if isinstance(token, AuthenticationContext) else None
                ),
                allow_legacy_authorisation=isinstance(token, ApiToken),
            )
            await _audit_protected_success(
                runtime,
                token,
                "ARTIFACT_DOWNLOADED",
                resource_type="ARTIFACT",
                resource_id=str(record.id),
            )
            return _artifact_content_response(content)
        return record.model_dump(mode="json", exclude={"path"})

    @app.get("/api/v1/artifacts/{artifact_id}/content")
    async def download_artifact_content(
        artifact_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_read)],
    ) -> StreamingResponse:
        content = await runtime.artifact_access.content(
            artifact_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "ARTIFACT_DOWNLOADED",
            resource_type="ARTIFACT",
            resource_id=str(content.record.id),
        )
        return _artifact_content_response(content)

    @app.post("/api/v1/artifacts/{artifact_id}/transfers", status_code=201)
    async def issue_artifact_download(
        artifact_id: UUID,
        body: ArtifactTransferRequest,
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_read)],
    ) -> dict[str, object]:
        issued = await runtime.artifact_access.issue_download(
            artifact_id,
            agent_id=body.agent_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "ARTIFACT_TRANSFER_ISSUED",
            resource_type="ARTIFACT",
            resource_id=str(artifact_id),
            metadata={
                "transfer_id": str(issued.transfer.id),
                "agent_id": str(body.agent_id),
            },
        )
        return {
            "transfer": issued.transfer.model_dump(mode="json", exclude={"token_hash"}),
            "token": issued.plaintext_token.get_secret_value(),
            "download_url": (
                f"{runtime.config.control_plane.public_url}/api/v1/"
                f"artifact-transfers/{issued.transfer.id}/content"
            ),
        }

    @app.delete("/api/v1/artifacts/{artifact_id}", status_code=204)
    async def delete_artifact(
        artifact_id: UUID,
        token: Annotated[AuthenticatedActor | None, Depends(artifacts_delete)],
    ) -> Response:
        record = await runtime.artifact_access.delete(
            artifact_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        await _audit_protected_success(
            runtime,
            token,
            "ARTIFACT_DELETED",
            resource_type="ARTIFACT",
            resource_id=str(artifact_id),
            metadata={
                "owner_type": record.owner_type.value,
                "owner_id": str(record.owner_id),
            },
        )
        return Response(status_code=204)

    @app.put("/api/v1/artifact-transfers/{transfer_id}/content")
    async def upload_transfer_content(
        transfer_id: UUID,
        request: Request,
    ) -> object:
        token = _bearer_value(request.headers.get("Authorization"))
        raw_length = request.headers.get("Content-Length")
        content_length = int(raw_length) if raw_length is not None else None
        artifact = await runtime.artifacts.upload(
            transfer_id,
            token,
            request.stream(),
            content_length=content_length,
        )
        return artifact.model_dump(mode="json")

    @app.get("/api/v1/artifact-transfers/{transfer_id}/content")
    async def download_transfer_content(
        transfer_id: UUID,
        request: Request,
    ) -> StreamingResponse:
        token = _bearer_value(request.headers.get("Authorization"))
        stream = await runtime.artifacts.download_stream(transfer_id, token)
        return StreamingResponse(
            stream,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{transfer_id}"',
            },
        )

    # The SPA fallback must be the final router so it can never shadow API,
    # documentation, metrics, health, or Agent gateway paths.
    app.include_router(create_web_router(runtime.config.web))
    return app


async def _action_bench(
    runtime: ControlPlaneRuntime,
    bench_id: str,
    *,
    capability: str,
    organisation_id: UUID | None = None,
) -> GlobalBenchRecord:
    bench = await runtime.inventory_repository.get(
        bench_id,
        organisation_id=organisation_id,
    )
    if bench is None:
        raise BenchNotFoundError("Bench does not exist.", bench_id=bench_id)
    if capability not in bench.capabilities:
        raise CapabilityNotSupportedError(
            "Bench does not support the requested action.",
            bench_id=bench.id,
            required_capability=capability,
        )
    return bench


def _ci_session_is_owned_by(session: CiSession, principal: Principal) -> bool:
    return (
        session.organisation_id == principal.organisation_id
        and session.requested_by_principal_id == principal.id
        and session.requested_by_principal_type is principal.type
    )


async def _action_reservation(
    runtime: ControlPlaneRuntime,
    bench_id: str,
    owner: str,
    *,
    required_permission: str = "benches:operate",
    owner_principal: ReservationOwner | None = None,
    organisation_id: UUID | None = None,
    actor: AuthenticatedActor | None = None,
) -> CoordinatedReservationLease:
    reservation = await runtime.reservation_repository.get_current_for_bench(
        bench_id,
        organisation_id=organisation_id,
    )
    if reservation is None:
        raise ReservationNotActiveError(
            "Mutating remote bench actions require an active reservation.",
            bench_id=bench_id,
        )
    stored = reservation.reservation
    if owner_principal is not None and stored.owner_principal_id is not None:
        owner_mismatch = (
            stored.owner_principal_id != owner_principal.principal_id
            or stored.owner_principal_type != owner_principal.principal_type.value
        )
    else:
        owner_mismatch = stored.owner != owner
    if owner_mismatch:
        await _audit_ownership_denial(
            runtime,
            actor,
            required_permission,
            resource_type=ResourceType.BENCH.value,
            resource_id=bench_id,
            reason="The active reservation belongs to another principal.",
        )
        raise ReservationOwnerMismatchError(
            "The active reservation belongs to another owner.",
            bench_id=bench_id,
            reservation_id=str(reservation.reservation.id),
        )
    if reservation.state is not ReservationLeaseState.ACTIVE:
        raise ReservationNotActiveError(
            "The remote reservation lease is not active.",
            bench_id=bench_id,
            reservation_id=str(reservation.reservation.id),
            lease_state=reservation.state.value,
        )
    return reservation


def _remote_operation_payload(
    command: RemoteCommand,
    operation: DistributedOperation | None,
) -> dict[str, object]:
    if operation is None:
        raise RuntimeError("Remote bench action did not create a distributed operation")
    return {
        "operation_id": str(operation.id),
        "command_id": str(command.id),
        "status": operation.status.value,
    }


async def _distributed_operation(
    runtime: ControlPlaneRuntime,
    operation_id: UUID,
    *,
    organisation_id: UUID | None = None,
) -> DistributedOperation:
    operation = await runtime.operation_records.get(
        operation_id,
        organisation_id=organisation_id,
    )
    if operation is None:
        raise RemoteCommandNotFoundError("Distributed operation does not exist.")
    return operation


def _distributed_workflow_status(operation: DistributedOperation) -> str:
    if operation.status in {
        DistributedOperationStatus.CREATED,
        DistributedOperationStatus.DISPATCHED,
        DistributedOperationStatus.ACCEPTED,
    }:
        return "pending"
    if operation.status is DistributedOperationStatus.RUNNING:
        return "running"
    if operation.status is DistributedOperationStatus.UNKNOWN:
        return "unknown"
    if operation.status is DistributedOperationStatus.RECONCILING:
        return "reconciling"
    return operation.status.value.casefold()


def _distributed_workflow_payload(
    operation: DistributedOperation,
    command: RemoteCommand | None = None,
) -> dict[str, object]:
    raw_workflow = (operation.result or {}).get("workflow_run")
    workflow = dict(raw_workflow) if isinstance(raw_workflow, dict) else {}
    raw_definition = command.payload.get("definition") if command is not None else None
    definition = dict(raw_definition) if isinstance(raw_definition, dict) else {}
    workflow.setdefault(
        "workflow_name",
        workflow.get("name") or definition.get("name") or operation.operation_type,
    )
    if "version" not in workflow and definition.get("version") is not None:
        workflow["version"] = definition["version"]
    if "steps" not in workflow and isinstance(definition.get("steps"), list):
        workflow["steps"] = definition["steps"]
    local_workflow_run_id = workflow.get("id")
    workflow.update(
        {
            "id": str(operation.id),
            "local_workflow_run_id": local_workflow_run_id,
            "bench_id": operation.bench_id,
            "agent_id": str(operation.agent_id),
            "reservation_id": (
                str(operation.reservation_id) if operation.reservation_id is not None else None
            ),
            "status": _distributed_workflow_status(operation),
            "operation_status": operation.status.value,
            "progress": operation.progress,
            "message": operation.message,
            "created_at": operation.created_at.isoformat(),
            "started_at": (
                operation.started_at.isoformat() if operation.started_at is not None else None
            ),
            "completed_at": (
                operation.completed_at.isoformat() if operation.completed_at is not None else None
            ),
            "last_agent_update_at": (
                operation.last_agent_update_at.isoformat()
                if operation.last_agent_update_at is not None
                else None
            ),
            "connection_state": (
                "RECONNECTING"
                if operation.status
                in {
                    DistributedOperationStatus.UNKNOWN,
                    DistributedOperationStatus.RECONCILING,
                }
                else "LIVE"
                if operation.status
                not in {
                    DistributedOperationStatus.SUCCEEDED,
                    DistributedOperationStatus.FAILED,
                    DistributedOperationStatus.CANCELLED,
                }
                else "COMPLETE"
            ),
            "error_code": operation.error_code,
            "error_message": operation.error_message,
        }
    )
    return workflow


def _distributed_workflow_result(
    operation: DistributedOperation,
) -> tuple[dict[str, object], list[WorkflowStepResult]]:
    result = operation.result or {}
    raw_workflow = result.get("workflow_run")
    workflow = dict(raw_workflow) if isinstance(raw_workflow, dict) else {}
    raw_steps = result.get("steps", [])
    steps = (
        [WorkflowStepResult.model_validate(item) for item in raw_steps]
        if isinstance(raw_steps, list)
        else []
    )
    return workflow, steps


async def _cancel_distributed_operation(
    runtime: ControlPlaneRuntime,
    operation_id: UUID,
    *,
    actor: AuthenticatedActor | None,
    requested_owner: str | None,
    reason: str | None,
    audit_action: str,
) -> dict[str, object]:
    organisation_id = _actor_organisation_id(actor)
    operation = await _distributed_operation(
        runtime,
        operation_id,
        organisation_id=organisation_id,
    )
    if operation.status in {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }:
        raise OperationNotCancellableError(
            "Distributed operation is already terminal.",
            operation_id=str(operation_id),
            operation_status=operation.status.value,
        )
    command = await runtime.command_repository.get_command(
        operation.remote_command_id,
        organisation_id=organisation_id,
    )
    if command is None:
        raise RemoteCommandNotFoundError("Remote command does not exist.")
    owner, owner_principal = _reservation_identity(actor, requested_owner)
    if (
        command.actor_context is not None
        and owner_principal is not None
        and (
            command.actor_context.principal_id != owner_principal.principal_id
            or command.actor_context.principal_type is not owner_principal.principal_type
        )
    ):
        await _audit_ownership_denial(
            runtime,
            actor,
            "operations:cancel",
            resource_type=ResourceType.BENCH.value,
            resource_id=operation.bench_id,
            reason="The operation belongs to another authenticated principal.",
        )
        raise ReservationOwnerMismatchError(
            "Operation owner does not match the authenticated principal.",
            operation_id=str(operation_id),
        )
    expected_owner = command.payload.get("owner")
    if not isinstance(expected_owner, str) and operation.reservation_id is not None:
        reservation = await runtime.reservation_repository.get(
            operation.reservation_id,
            organisation_id=organisation_id,
        )
        if reservation is None:
            raise ReservationNotFoundError("Reservation does not exist.")
        expected_owner = reservation.reservation.owner
    if isinstance(expected_owner, str) and expected_owner != owner:
        await _audit_ownership_denial(
            runtime,
            actor,
            "operations:cancel",
            resource_type=ResourceType.BENCH.value,
            resource_id=operation.bench_id,
            reason="The legacy operation owner does not match the cancellation request.",
        )
        raise ReservationOwnerMismatchError(
            "Operation owner does not match the cancellation request.",
            operation_id=str(operation_id),
        )
    actor_context = _actor_context(actor)
    snapshot_id = _authorisation_snapshot_id(actor)
    intent_metadata: dict[str, object] = {
        "command_id": str(command.id),
        "operation_id": str(operation_id),
    }
    if actor_context is not None:
        intent_metadata["actor_context"] = actor_context.model_dump(mode="json")
        intent_metadata["authorisation_snapshot_id"] = (
            str(snapshot_id) if snapshot_id is not None else None
        )
    await runtime.record_timeline(
        command.agent_id,
        "REMOTE_COMMAND_CANCELLATION_REQUESTED",
        "Cancellation of a remote command was requested.",
        correlation_id=command.id,
        metadata=intent_metadata,
        deduplication_key=(
            f"control-intent:cancel:{command.id}:{snapshot_id}" if snapshot_id is not None else None
        ),
    )
    await runtime.commands.request_cancel(
        command.id,
        reason=reason,
        authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
        allow_legacy_authorisation=isinstance(actor, ApiToken),
    )
    current = await _distributed_operation(
        runtime,
        operation_id,
        organisation_id=organisation_id,
    )
    await _audit_protected_success(
        runtime,
        actor,
        audit_action,
        resource_type="BENCH",
        resource_id=operation.bench_id,
        metadata={"command_id": str(command.id), "operation_id": str(operation_id)},
    )
    return {
        "operation_id": str(current.id),
        "status": current.status.value,
        "cancellation_requested": True,
    }


async def _ci_session_payload(
    runtime: ControlPlaneRuntime,
    session: CiSession,
    *,
    details: bool = False,
    organisation_id: UUID | None = None,
) -> dict[str, object]:
    payload = (
        await runtime.ci.details(session.id, organisation_id=organisation_id)
        if details
        else session.model_dump(mode="json")
    )
    payload["cleanup_timeout_seconds"] = runtime.config.artifacts.finalization_timeout_seconds
    binding = await runtime.ci_repository.get_distributed_workflow(
        session.id,
        organisation_id=organisation_id,
    )
    payload["distributed_workflow"] = (
        binding.model_dump(mode="json") if binding is not None else None
    )
    if binding is not None:
        payload.update(
            {
                "agent_id": str(binding.agent_id),
                "remote_command_id": str(binding.remote_command_id),
                "operation_id": str(binding.operation_id),
            }
        )
    return payload


async def _ci_session_is_allowed(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    session: CiSession,
    permission: str,
) -> bool:
    if not isinstance(actor, AuthenticationContext):
        return True
    principal = actor.principal
    if (
        session.organisation_id == principal.organisation_id
        and session.requested_by_principal_id == principal.id
        and session.requested_by_principal_type is principal.type
    ):
        return await runtime.authorisation.is_allowed_anywhere(
            principal,
            permission,
            credential_restrictions=actor.permission_restrictions,
        )
    return await runtime.authorisation.is_allowed(
        principal,
        permission,
        AuthorisationResource(
            type=ResourceType.ORGANISATION,
            id=str(principal.organisation_id),
            organisation_id=principal.organisation_id,
        ),
        credential_restrictions=actor.permission_restrictions,
    )


async def _require_ci_session_access(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    session: CiSession,
    permission: str,
) -> None:
    if not isinstance(actor, AuthenticationContext):
        return
    principal = actor.principal
    try:
        if _ci_session_is_owned_by(session, principal):
            await runtime.authorisation.require_anywhere(
                principal,
                permission,
                credential_restrictions=actor.permission_restrictions,
            )
        else:
            await runtime.authorisation.require(
                principal,
                permission,
                AuthorisationResource(
                    type=ResourceType.ORGANISATION,
                    id=str(principal.organisation_id),
                    organisation_id=principal.organisation_id,
                ),
                credential_restrictions=actor.permission_restrictions,
            )
    except PermissionDeniedError:
        if runtime.config.authorisation.hide_unauthorised_resources:
            raise CiSessionNotFoundError(
                "CI session does not exist.",
                ci_session_id=str(session.id),
            ) from None
        raise


def _require_scopes(
    runtime: ControlPlaneRuntime,
    *required: ApiTokenScope,
    allow_bootstrap: bool = False,
    phase6_permissions: tuple[str, ...] | None = None,
    phase6_resource: Phase6ResourceKind = "organisation",
    persist_authorisation_snapshot: bool = False,
) -> Callable[..., object]:
    async def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)] = None,
    ) -> AuthenticatedActor | None:
        credential = extract_bearer_or_cookie_token(request, credentials)
        context: AuthenticationContext | None = None
        if (
            credential is None
            and allow_bootstrap
            and runtime.config.authorisation.legacy_token_compatibility_enabled
            and not await runtime.token_service.list()
            and await _legacy_bootstrap_allowed(runtime)
        ):
            # The explicit loopback-only legacy bootstrap must remain reachable on a
            # fresh database even when development auto-login names a user that has
            # not been created yet.
            return None
        if credential is None:
            context = await _development_auto_login_context(runtime)
        if credential is None and context is None:
            raise AuthenticationRequiredError("A bearer token or browser session is required.")
        token = credential.token if credential is not None else None
        if (
            context is None
            and token is not None
            and runtime.config.identity.enabled
            and is_phase6_identity_token(token)
        ):
            context = await authenticate_identity_token(
                runtime,
                token,
                source_ip=request.client.host if request.client is not None else None,
            )
        if context is not None:
            resource = await _phase6_authorisation_resource(
                runtime,
                request,
                context,
                phase6_resource,
            )
            required_permissions = (
                phase6_permissions
                if phase6_permissions is not None
                else tuple(_PHASE6_SCOPE_PERMISSIONS[scope] for scope in required)
            )
            for index, permission in enumerate(required_permissions):
                context = await _require_phase6_permission(
                    runtime,
                    context,
                    permission,
                    resource,
                    resource_kind=phase6_resource,
                    persist_snapshot=(
                        persist_authorisation_snapshot and index == len(required_permissions) - 1
                    ),
                )
            return context
        assert token is not None
        if not runtime.config.authorisation.legacy_token_compatibility_enabled:
            raise AuthenticationFailedError("Legacy API token authentication is disabled.")
        return await runtime.token_service.authenticate(token, required)

    return dependency


def _require_reservation_creation(
    runtime: ControlPlaneRuntime,
) -> Callable[..., object]:
    """Authorize a reservation against the trusted bench parsed from its body."""

    async def dependency(
        request: Request,
        body: ReservationCreateRequest,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)] = None,
    ) -> AuthenticatedActor:
        credential = extract_bearer_or_cookie_token(request, credentials)
        context = await _development_auto_login_context(runtime) if credential is None else None
        if credential is None and context is None:
            raise AuthenticationRequiredError("A bearer token or browser session is required.")
        token = credential.token if credential is not None else None
        if (
            context is None
            and token is not None
            and runtime.config.identity.enabled
            and is_phase6_identity_token(token)
        ):
            context = await authenticate_identity_token(
                runtime,
                token,
                source_ip=request.client.host if request.client is not None else None,
            )
        if context is None:
            assert token is not None
            if not runtime.config.authorisation.legacy_token_compatibility_enabled:
                raise AuthenticationFailedError("Legacy API token authentication is disabled.")
            return await runtime.token_service.authenticate(
                token,
                (ApiTokenScope.RESERVATIONS_WRITE,),
            )
        bench = await runtime.inventory_repository.get(
            body.bench_id,
            organisation_id=context.principal.organisation_id,
        )
        if bench is None:
            raise BenchNotFoundError("Bench does not exist.", bench_id=body.bench_id)
        return await _require_phase6_permission(
            runtime,
            context,
            "benches:reserve",
            _bench_authorisation_resource(context.principal.organisation_id, bench),
            resource_kind="bench",
            persist_snapshot=False,
        )

    return dependency


async def _development_auto_login_context(
    runtime: ControlPlaneRuntime,
) -> AuthenticationContext | None:
    """Resolve the explicitly configured loopback-only development principal."""

    username = runtime.config.development.auto_login_user
    if username is None:
        return None
    # ControlPlaneConfig rejects this combination unless both bind and public
    # hosts are loopback. Keep the runtime guard as defence in depth for callers
    # that may construct a config model without the normal validation path.
    if not runtime.config.development.enabled:
        raise AuthenticationFailedError("Development auto-login is not safely configured.")
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    if organisation is None or organisation.status is not OrganisationStatus.ACTIVE:
        raise AuthenticationFailedError("The development auto-login organisation is unavailable.")
    user = await runtime.identity_repository.get_user_by_username(organisation.id, username)
    if user is None or user.status is not UserStatus.ACTIVE:
        raise AuthenticationFailedError("The development auto-login user is unavailable.")
    return AuthenticationContext(
        principal=Principal(
            id=user.id,
            type=PrincipalType.USER,
            organisation_id=organisation.id,
            display_name=user.display_name,
        )
    )


async def _require_phase6_permission(
    runtime: ControlPlaneRuntime,
    context: AuthenticationContext,
    permission: str,
    resource: AuthorisationResource,
    *,
    resource_kind: Phase6ResourceKind,
    persist_snapshot: bool,
) -> AuthenticationContext:
    decision = await runtime.authorisation.evaluate(
        context.principal,
        permission,
        resource,
        credential_restrictions=context.permission_restrictions,
    )
    if not decision.allowed:
        # ``require`` owns consistent denial auditing and the public error shape.
        try:
            await runtime.authorisation.require(
                context.principal,
                permission,
                resource,
                credential_restrictions=context.permission_restrictions,
            )
        except PermissionDeniedError:
            if runtime.config.authorisation.hide_unauthorised_resources:
                _raise_hidden_resource(resource_kind, resource)
            raise
        raise AssertionError("AuthorisationService.require must reject a denied decision")
    if not persist_snapshot:
        return context
    snapshot = await runtime.identity_repository.create_authorisation_snapshot(
        context.principal.organisation_id,
        AuthorisationSnapshot(
            principal_id=context.principal.id,
            permission=permission,
            resource_type=resource.type,
            resource_id=resource.id,
            granted_by_assignments=sorted(decision.granting_assignment_ids, key=str),
        ),
    )
    return context.model_copy(update={"authorisation_snapshot_id": snapshot.id})


async def _phase6_allowed_resources(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    permission: str,
    resources: list[AuthorisationResource],
) -> list[bool]:
    """Filter collection items without creating denial audit noise for hidden rows."""

    if not isinstance(actor, AuthenticationContext):
        return [True] * len(resources)
    if not resources:
        await runtime.authorisation.require(
            actor.principal,
            permission,
            AuthorisationResource(
                type=ResourceType.ORGANISATION,
                id=str(actor.principal.organisation_id),
                organisation_id=actor.principal.organisation_id,
            ),
            credential_restrictions=actor.permission_restrictions,
        )
        return []
    return list(
        await asyncio.gather(
            *(
                runtime.authorisation.is_allowed(
                    actor.principal,
                    permission,
                    resource,
                    credential_restrictions=actor.permission_restrictions,
                )
                for resource in resources
            )
        )
    )


def _raise_hidden_resource(
    kind: Phase6ResourceKind,
    resource: AuthorisationResource,
) -> None:
    """Collapse inaccessible named resources to the same shape as missing ones."""

    if kind == "agent":
        raise AgentNotFoundError("Agent does not exist.", agent_id=resource.id)
    if kind == "bench":
        raise BenchNotFoundError("Bench does not exist.", bench_id=resource.id)
    if kind == "workflow":
        raise WorkflowNotFoundError("Workflow does not exist.", workflow_name=resource.id)
    if kind == "reservation":
        raise ReservationNotFoundError("Reservation does not exist.")
    if kind in {"operation", "operation-agent"}:
        raise RemoteCommandNotFoundError("Distributed operation does not exist.")


async def _phase6_authorisation_resource(
    runtime: ControlPlaneRuntime,
    request: Request,
    context: AuthenticationContext,
    kind: Phase6ResourceKind,
) -> AuthorisationResource:
    organisation_id = context.principal.organisation_id
    if kind == "organisation":
        return AuthorisationResource(
            type=ResourceType.ORGANISATION,
            id=str(organisation_id),
            organisation_id=organisation_id,
        )
    if kind == "agent":
        agent_id = _path_uuid(request, "agent_id")
        agent = await runtime.presence.get_agent(
            agent_id,
            organisation_id=organisation_id,
        )
        return AuthorisationResource(
            type=ResourceType.AGENT,
            id=str(agent.id),
            organisation_id=organisation_id,
        )
    if kind == "bench":
        bench_id = str(request.path_params["bench_id"])
        bench = await runtime.inventory_repository.get(
            bench_id,
            organisation_id=organisation_id,
        )
        if bench is None:
            raise BenchNotFoundError("Bench does not exist.", bench_id=bench_id)
        return _bench_authorisation_resource(organisation_id, bench)
    if kind == "workflow":
        return AuthorisationResource(
            type=ResourceType.WORKFLOW,
            id=str(request.path_params["workflow_name"]),
            organisation_id=organisation_id,
        )
    if kind in {"operation", "operation-agent"}:
        operation_id = _path_uuid(request, "operation_id")
        operation = await runtime.operation_records.get(
            operation_id,
            organisation_id=organisation_id,
        )
        if operation is None:
            raise RemoteCommandNotFoundError("Distributed operation does not exist.")
        if kind == "operation-agent":
            return AuthorisationResource(
                type=ResourceType.AGENT,
                id=str(operation.agent_id),
                organisation_id=organisation_id,
            )
        return AuthorisationResource(
            type=ResourceType.BENCH,
            id=operation.bench_id,
            organisation_id=organisation_id,
            parent_agent_id=operation.agent_id,
        )
    reservation = await runtime.reservation_repository.get(
        _path_uuid(request, "reservation_id"),
        organisation_id=organisation_id,
    )
    if reservation is None:
        uncoordinated = await runtime.reservations.get_uncoordinated(
            _path_uuid(request, "reservation_id"),
            organisation_id=organisation_id,
        )
        if uncoordinated is None:
            raise ReservationNotFoundError("Reservation does not exist.")
        bench = await runtime.inventory_repository.get(
            uncoordinated.bench_id,
            organisation_id=organisation_id,
        )
        if bench is None:
            raise ReservationNotFoundError("Reservation does not exist.")
        return AuthorisationResource(
            type=ResourceType.BENCH,
            id=uncoordinated.bench_id,
            organisation_id=organisation_id,
            parent_agent_id=bench.agent_id,
        )
    return AuthorisationResource(
        type=ResourceType.BENCH,
        id=reservation.reservation.bench_id,
        organisation_id=organisation_id,
        parent_agent_id=reservation.lease.agent_id,
    )


def _bench_authorisation_resource(
    organisation_id: UUID,
    bench: GlobalBenchRecord,
) -> AuthorisationResource:
    return AuthorisationResource(
        type=ResourceType.BENCH,
        id=bench.id,
        organisation_id=organisation_id,
        parent_agent_id=bench.agent_id,
    )


def _path_uuid(request: Request, name: str) -> UUID:
    try:
        return UUID(str(request.path_params[name]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name} must be a UUID",
        ) from exc


def _public_api_token(token: ApiToken) -> dict[str, object]:
    payload = token.model_dump(mode="json")
    payload.pop("token_hash", None)
    return payload


def _principal_display_name(actor: AuthenticatedActor | None) -> str | None:
    if isinstance(actor, ApiToken):
        return actor.owner
    return actor.principal.display_name if actor is not None else None


def _actor_organisation_id(actor: AuthenticatedActor | None) -> UUID | None:
    return actor.principal.organisation_id if isinstance(actor, AuthenticationContext) else None


def _reservation_identity(
    actor: AuthenticatedActor | None,
    requested_owner: str | None,
) -> tuple[str, ReservationOwner | None]:
    """Resolve reservation ownership from authentication, never Phase 6 request text."""

    if isinstance(actor, AuthenticationContext):
        principal = actor.principal
        return principal.display_name, ReservationOwner(
            principal_id=principal.id,
            principal_type=principal.type,
            display_name=principal.display_name,
        )
    if requested_owner is None:
        raise PermissionDeniedError("Legacy API requests must provide an owner value.")
    return requested_owner, None


def _actor_context(actor: AuthenticatedActor | None) -> ActorContext | None:
    if not isinstance(actor, AuthenticationContext):
        return None
    principal = actor.principal
    return ActorContext(
        principal_id=principal.id,
        principal_type=principal.type,
        display_name=principal.display_name,
        organisation_id=principal.organisation_id,
        authorisation_snapshot_id=actor.authorisation_snapshot_id,
    )


def _authorisation_snapshot_id(actor: AuthenticatedActor | None) -> UUID | None:
    return actor.authorisation_snapshot_id if isinstance(actor, AuthenticationContext) else None


async def _audit_protected_success(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    action: str,
    *,
    resource_type: str,
    resource_id: str | None,
    metadata: dict[str, object] | None = None,
) -> None:
    if not isinstance(actor, AuthenticationContext):
        return
    await runtime.authorisation.audit_success(
        actor.principal,
        action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
    )


async def _audit_ownership_denial(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    permission: str,
    *,
    resource_type: str,
    resource_id: str | None,
    reason: str,
) -> None:
    if not isinstance(actor, AuthenticationContext):
        return
    await runtime.authorisation.audit_permission_denied(
        actor.principal,
        permission,
        resource_type=resource_type,
        resource_id=resource_id,
        reason=reason,
    )


def _apply_security_headers(request: Request, response: Response) -> None:
    """Apply a conservative browser baseline without changing API caching globally."""

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    if request.url.path.startswith("/api/v1/auth") or "/credentials" in request.url.path:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")


async def _legacy_bootstrap_allowed(runtime: ControlPlaneRuntime) -> bool:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    return (
        organisation is None
        or await runtime.identity_repository.count_organisation_owners(organisation.id) == 0
    )


def _require_legacy_token_compatibility(runtime: ControlPlaneRuntime) -> None:
    if not runtime.config.authorisation.legacy_token_compatibility_enabled:
        raise PermissionDeniedError("Legacy API token compatibility is disabled.")


def _is_active_control_plane_administrator(token: ApiToken) -> bool:
    now = datetime.now(UTC)
    return (
        token.revoked_at is None
        and (token.expires_at is None or token.expires_at > now)
        and ApiTokenScope.AGENTS_ADMIN in token.scopes
    )


def _parse_labels(values: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values:
        key, separator, label_value = value.replace(":", "=", 1).partition("=")
        if not separator or not key.strip() or not label_value.strip():
            raise ValueError("Labels must use key=value syntax")
        labels[key.strip()] = label_value.strip()
    return labels


def _bearer_value(value: str | None) -> str:
    if value is None:
        raise AuthenticationRequiredError("A transfer bearer token is required.")
    scheme, separator, credential = value.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credential.strip():
        raise AuthenticationRequiredError("Transfer authentication must use Bearer.")
    return credential.strip()


def _dataclass_payload(value: object) -> dict[str, object]:
    fields = getattr(value, "__dataclass_fields__", None)
    if not isinstance(fields, dict):
        raise TypeError("Expected a dataclass response")
    payload: dict[str, object] = {}
    for field_name in fields:
        item = getattr(value, field_name)
        if isinstance(item, (datetime, UUID)):
            payload[field_name] = str(item)
        else:
            payload[field_name] = item
    return payload


def _coordinated_reservation_payload(
    value: CoordinatedReservationLease,
) -> dict[str, object]:
    reservation = value.reservation
    lease = value.lease
    state = value.state
    return {
        "reservation": reservation.model_dump(mode="json"),
        "lease": lease.model_dump(mode="json"),
        "state": state.value,
        "revision": value.revision,
        "unknown_since": value.unknown_since.isoformat()
        if value.unknown_since is not None
        else None,
        "reconciliation_deadline": value.reconciliation_deadline.isoformat()
        if value.reconciliation_deadline is not None
        else None,
    }


def _reservation_value(value: CoordinatedReservationLease | Reservation) -> Reservation:
    return value.reservation if isinstance(value, CoordinatedReservationLease) else value


async def _reservation_presentation_payload(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    value: CoordinatedReservationLease | Reservation,
    *,
    parent_agent_id: UUID | None = None,
) -> dict[str, object]:
    payload = _reservation_payload(value)
    permissions = await reservation_action_permissions(
        runtime,
        actor,
        value,
        parent_agent_id=parent_agent_id,
    )
    payload["permissions"] = permissions.model_dump(mode="json")
    return payload


def _reservation_payload(
    value: CoordinatedReservationLease | Reservation,
) -> dict[str, object]:
    if isinstance(value, CoordinatedReservationLease):
        return _coordinated_reservation_payload(value)
    return {
        "reservation": value.model_dump(mode="json"),
        "lease": None,
        "state": value.status.value.upper(),
        "revision": 0,
        "unknown_since": None,
        "reconciliation_deadline": None,
    }


def _require_active_reservation(value: CoordinatedReservationLease) -> None:
    if value.state is not ReservationLeaseState.ACTIVE:
        raise ReservationLeaseInvalidError(
            "The Agent did not confirm the reservation lease.",
            reservation_id=str(value.reservation.id),
            lease_version=value.lease.lease_version,
            lease_state=value.state.value,
        )


def _observe_request(
    runtime: ControlPlaneRuntime,
    request: Request,
    response: Response,
    started: float,
    *,
    route: str | None = None,
) -> None:
    duration_seconds = max(0.0, time.perf_counter() - started)
    route_object = request.scope.get("route")
    route_template = route or getattr(route_object, "path", None)
    if not isinstance(route_template, str) or not route_template.startswith("/"):
        route_template = "/__unmatched__"
    runtime.observability.observe_http(
        method=request.method,
        route=route_template,
        status_code=response.status_code,
        duration_seconds=duration_seconds,
    )
    _LOGGER.info(
        "HTTP request completed",
        extra={
            "request_id": request.state.request_id,
            "method": request.method,
            "path": route_template,
            "status_code": response.status_code,
            "duration_ms": round(duration_seconds * 1000, 3),
        },
    )


_ERROR_STATUS = {
    "AUTHENTICATION_REQUIRED": 401,
    "AUTHENTICATION_FAILED": 401,
    "INVALID_CREDENTIALS": 401,
    "SESSION_EXPIRED": 401,
    "SESSION_REVOKED": 401,
    "TOKEN_EXPIRED": 401,
    "TOKEN_REVOKED": 401,
    "INVALID_API_TOKEN": 401,
    "PERMISSION_DENIED": 403,
    "USER_NOT_FOUND": 404,
    "USER_DISABLED": 403,
    "USER_LOCKED": 423,
    "USERNAME_ALREADY_EXISTS": 409,
    "SERVICE_ACCOUNT_NOT_FOUND": 404,
    "SERVICE_ACCOUNT_DISABLED": 403,
    "TEAM_NOT_FOUND": 404,
    "TEAM_ALREADY_EXISTS": 409,
    "TEAM_MEMBERSHIP_NOT_FOUND": 404,
    "ROLE_ASSIGNMENT_NOT_FOUND": 404,
    "ROLE_ASSIGNMENT_CONFLICT": 409,
    "ROLE_NOT_ALLOWED": 403,
    "RESOURCE_ACCESS_DENIED": 403,
    "ORGANISATION_NOT_FOUND": 404,
    "ORGANISATION_SUSPENDED": 403,
    "CROSS_ORGANISATION_ACCESS_DENIED": 403,
    "AUDIT_EVENT_NOT_FOUND": 404,
    "LOGIN_RATE_LIMIT_EXCEEDED": 429,
    "OIDC_CONFIGURATION_INVALID": 503,
    "OIDC_LOGIN_FAILED": 401,
    "OIDC_IDENTITY_NOT_MAPPED": 403,
    "AGENT_NOT_FOUND": 404,
    "AGENT_OFFLINE": 409,
    "AGENT_DEGRADED": 409,
    "AGENT_REVOKED": 409,
    "AGENT_DRAINING": 409,
    "AGENT_INCOMPATIBLE": 409,
    "AGENT_ENROLLMENT_TOKEN_INVALID": 400,
    "AGENT_ENROLLMENT_TOKEN_EXPIRED": 400,
    "AGENT_ENROLLMENT_TOKEN_USED": 409,
    "AGENT_AUTHENTICATION_FAILED": 401,
    "BENCH_NOT_FOUND": 404,
    "CAPABILITY_NOT_SUPPORTED": 409,
    "BENCH_ALREADY_RESERVED": 409,
    "BENCH_AGENT_MISMATCH": 409,
    "GLOBAL_BENCH_ID_CONFLICT": 409,
    "REMOTE_COMMAND_NOT_FOUND": 404,
    "REMOTE_COMMAND_REJECTED": 409,
    "REMOTE_COMMAND_DELIVERY_FAILED": 503,
    "REMOTE_OPERATION_STATE_UNKNOWN": 409,
    "RESERVATION_LEASE_INVALID": 409,
    "RESERVATION_LEASE_EXPIRED": 409,
    "RESERVATION_LEASE_VERSION_MISMATCH": 409,
    "RESERVATION_NOT_FOUND": 404,
    "RESERVATION_NOT_ACTIVE": 409,
    "RESERVATION_TIME_CONFLICT": 409,
    "RESERVATION_OWNER_MISMATCH": 403,
    "RESERVATION_MAX_DURATION_EXCEEDED": 400,
    "QUEUE_ENTRY_NOT_FOUND": 404,
    "QUEUE_OWNER_MISMATCH": 403,
    "QUEUE_DISABLED": 409,
    "NO_COMPATIBLE_BENCH": 409,
    "WORKFLOW_NOT_FOUND": 404,
    "WORKFLOW_INVALID": 400,
    "WORKFLOW_CAPABILITY_MISMATCH": 409,
    "CI_SESSION_NOT_FOUND": 404,
    "CI_SESSION_CONFLICT": 409,
    "OPERATION_NOT_CANCELLABLE": 409,
    "ARTIFACT_NOT_FOUND": 404,
    "ARTIFACT_TOO_LARGE": 413,
    "REQUEST_BODY_TOO_LARGE": 413,
    "RESOURCE_LIMIT_EXCEEDED": 429,
    "ARTIFACT_CHECKSUM_MISMATCH": 400,
    "ARTIFACT_TRANSFER_FAILED": 400,
    "ARTIFACT_TRANSFER_TOKEN_EXPIRED": 401,
    "INVENTORY_SYNC_FAILED": 409,
}


async def _platform_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlatformError)
    return _error_response(
        request,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        status_code=_ERROR_STATUS.get(exc.code, 500),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return _error_response(
        request,
        code="VALIDATION_ERROR",
        message="The request did not pass validation.",
        details={"errors": [_validation_detail(error) for error in exc.errors()]},
        status_code=422,
    )


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    return _error_response(
        request,
        code="RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
        message=str(exc.detail),
        details={},
        status_code=exc.status_code,
    )


async def _internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(
        request,
        code="INTERNAL_ERROR",
        message="An unexpected internal error occurred.",
        details={},
        status_code=500,
    )


def _error_response(
    request: Request,
    *,
    code: str,
    message: str,
    details: dict[str, object],
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": str(getattr(request.state, "request_id", uuid4())),
            }
        },
    )


def _validation_detail(error: dict[str, object]) -> dict[str, object]:
    location = error.get("loc", ())
    return {
        "type": str(error.get("type", "validation_error")),
        "loc": list(location) if isinstance(location, (tuple, list)) else [str(location)],
        "msg": str(error.get("msg", "Invalid value")),
    }
