import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiCreateRequest,
    DistributedCiStartRequest,
)
from lab_platform.control_plane_core.errors import (
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
from lab_platform.core import VERSION, build_test_results, render_junit_xml
from lab_platform.core.errors import (
    ArtifactNotFoundError,
    AuthenticationRequiredError,
    BenchNotFoundError,
    CapabilityNotSupportedError,
    OperationNotCancellableError,
    PermissionDeniedError,
    PlatformError,
    RequestBodyTooLargeError,
    ReservationNotActiveError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.workflows import WorkflowNotFoundError
from lab_platform.models import (
    AgentStatus,
    AgentTimelineSeverity,
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
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
    RemoteCommand,
    RemoteCommandType,
    SerialReadRequest,
    WorkflowDefinition,
    WorkflowStepResult,
)
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException
from starlette.types import Message

_BEARER = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


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
    protocol_version: str = Field(default="1.0", min_length=1, max_length=100)
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
    owner: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)


class BenchActionRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=200)


class SerialReadActionRequest(BenchActionRequest):
    timeout_seconds: float = Field(default=10, gt=0, le=3600)
    until_pattern: str | None = Field(default=None, max_length=2000)
    max_lines: int | None = Field(default=500, ge=1, le=100_000)


class ReservationCreateRequest(ApiModel):
    bench_id: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=500)
    reservation_duration_seconds: int | None = Field(default=None, gt=0, le=86_400)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=128)


class ReservationRenewRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=200)
    expected_lease_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=500)
    lease_ttl_seconds: int | None = Field(default=None, gt=0, le=3_600)


class ReservationReleaseRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=200)
    expected_lease_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=500)


class WorkflowRunRequest(ApiModel):
    version: int | None = Field(default=None, ge=1)
    owner: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=500)
    inputs: dict[str, object] = Field(default_factory=dict)
    bench_id: str | None = Field(default=None, min_length=1, max_length=200)
    kind: GlobalBenchKind | None = None
    location: str | None = Field(default=None, min_length=1, max_length=200)
    bench_labels: dict[str, str] = Field(default_factory=dict, max_length=128)
    agent_labels: dict[str, str] = Field(default_factory=dict, max_length=128)
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

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = request.headers.get("X-Request-ID", str(uuid4()))
        started = time.perf_counter()
        maximum_bytes = runtime.config.control_plane.max_request_body_size_mb * 1024 * 1024
        if request.method == "POST" and request.url.path == "/api/v1/artifacts":
            # Multipart framing and metadata have their own small overhead beyond
            # the configured immutable artifact byte limit.
            maximum_bytes = runtime.config.artifacts.max_upload_size_mb * 1024 * 1024 + 1024 * 1024
        elif (
            request.method == "PUT"
            and request.url.path.startswith("/api/v1/artifact-transfers/")
            and request.url.path.endswith("/content")
        ):
            maximum_bytes = runtime.config.artifacts.max_upload_size_mb * 1024 * 1024

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
        return response

    app.add_exception_handler(PlatformError, _platform_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _internal_error_handler)
    app.include_router(runtime.gateway.router())

    agents_read = _require_scopes(runtime, ApiTokenScope.AGENTS_READ)
    agents_admin = _require_scopes(runtime, ApiTokenScope.AGENTS_ADMIN)
    agents_admin_bootstrap = _require_scopes(
        runtime,
        ApiTokenScope.AGENTS_ADMIN,
        allow_bootstrap=True,
    )
    benches_read = _require_scopes(runtime, ApiTokenScope.BENCHES_READ)
    operations_read = _require_scopes(runtime, ApiTokenScope.OPERATIONS_READ)
    artifacts_read = _require_scopes(runtime, ApiTokenScope.ARTIFACTS_READ)
    artifacts_write = _require_scopes(runtime, ApiTokenScope.ARTIFACTS_WRITE)
    reservations_write = _require_scopes(runtime, ApiTokenScope.RESERVATIONS_WRITE)
    workflows_run = _require_scopes(runtime, ApiTokenScope.WORKFLOWS_RUN)
    firmware_write = _require_scopes(
        runtime,
        ApiTokenScope.WORKFLOWS_RUN,
        ApiTokenScope.ARTIFACTS_WRITE,
    )
    ci_sessions = _require_scopes(runtime, ApiTokenScope.CI_SESSIONS)

    @app.get("/api/v1/version")
    async def version() -> dict[str, str]:
        return {"version": VERSION, "protocol_version": "1.0"}

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return {
            "status": "healthy" if runtime.started else "starting",
            "version": VERSION,
            "components": {
                "database": "healthy" if runtime.started else "starting",
                "agent_gateway": await runtime.hub.metrics(),
            },
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(
        _token: Annotated[ApiToken | None, Depends(agents_read)],
    ) -> str:
        values = await runtime.metrics()
        return "".join(f"{name} {value}\n" for name, value in sorted(values.items()))

    @app.post("/api/v1/tokens", status_code=status.HTTP_201_CREATED)
    async def create_api_token(
        body: ApiTokenRequest,
        _token: Annotated[ApiToken | None, Depends(agents_admin_bootstrap)],
    ) -> dict[str, object]:
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
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        return {"items": [_public_api_token(token) for token in await runtime.token_service.list()]}

    @app.post("/api/v1/tokens/{token_id:uuid}/revoke")
    async def revoke_api_token(
        token_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, object]:
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
        _token: Annotated[ApiToken | None, Depends(agents_read)],
        agent_status: Annotated[AgentStatus | None, Query(alias="status")] = None,
        location: str | None = None,
        label: Annotated[list[str] | None, Query()] = None,
        version_filter: Annotated[str | None, Query(alias="version")] = None,
    ) -> dict[str, object]:
        agents = await runtime.presence.list_agents(
            status=agent_status,
            location=location,
            labels=_parse_labels(label or []),
            version=version_filter,
        )
        benches = await runtime.inventory.list_benches()
        counts: dict[UUID, int] = {}
        for bench in benches:
            counts[bench.agent_id] = counts.get(bench.agent_id, 0) + 1
        return {
            "items": [
                {**agent.model_dump(mode="json"), "bench_count": counts.get(agent.id, 0)}
                for agent in agents
            ]
        }

    @app.get("/api/v1/agents/{agent_id:uuid}")
    async def get_agent(
        agent_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_read)],
    ) -> dict[str, object]:
        agent = await runtime.presence.get_agent(agent_id)
        connection = await runtime.presence.active_connection(agent_id)
        benches = [
            bench.model_dump(mode="json")
            for bench in await runtime.inventory.list_benches(agent_id=agent_id)
        ]
        return {
            **agent.model_dump(mode="json"),
            "connection": (
                connection.connection.model_dump(mode="json") if connection is not None else None
            ),
            "benches": benches,
        }

    @app.post("/api/v1/agents/enrollment-tokens", status_code=status.HTTP_201_CREATED)
    async def create_enrollment_token(
        body: EnrollmentTokenRequest,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        expiry = body.expires_at or now + timedelta(seconds=body.expires_in_seconds or 3600)
        issued = await runtime.enrollment.issue_token(
            name=body.name,
            expires_at=expiry,
            allowed_labels=body.allowed_labels,
        )
        return {
            **_dataclass_payload(issued.token),
            "token": issued.plaintext.get_secret_value(),
        }

    @app.get("/api/v1/agents/enrollment-tokens")
    async def list_enrollment_tokens(
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        return {
            "items": [_dataclass_payload(item) for item in await runtime.enrollment.list_tokens()]
        }

    @app.delete("/api/v1/agents/enrollment-tokens/{token_id}", status_code=204)
    async def revoke_enrollment_token(
        token_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> Response:
        await runtime.enrollment.revoke_token(token_id)
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
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> object:
        return (await runtime.revoke_agent(agent_id)).model_dump(mode="json")

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
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, object]:
        result = await runtime.drain_agent(
            agent_id,
            cancel_queued_work=body.cancel_queued_work,
        )
        return {
            "agent": result.agent.model_dump(mode="json"),
            "workload": _dataclass_payload(result.workload),
            "cancelled_queued_work": result.cancelled_queued_work,
        }

    @app.post("/api/v1/agents/{agent_id:uuid}/undrain")
    async def undrain_agent(
        agent_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> object:
        return (await runtime.undrain_agent(agent_id)).model_dump(mode="json")

    @app.post("/api/v1/agents/{agent_id:uuid}/actions/refresh-inventory", status_code=202)
    async def refresh_inventory(
        agent_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, str]:
        if not await runtime.hub.is_connected(agent_id):
            raise AgentOfflineError("Agent is not connected.", agent_id=str(agent_id))
        return {"request_id": str(await runtime.refresh_inventory(agent_id))}

    @app.get("/api/v1/agents/{agent_id:uuid}/timeline")
    async def agent_timeline(
        agent_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_read)],
        severity: AgentTimelineSeverity | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        await runtime.presence.get_agent(agent_id)
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
        _token: Annotated[ApiToken | None, Depends(benches_read)],
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
        selected_agent_ids: set[UUID] | None = None
        parsed_agent_labels = _parse_labels(agent_label or [])
        if location is not None or parsed_agent_labels:
            selected_agent_ids = {
                agent.id
                for agent in await runtime.presence.list_agents(
                    location=location,
                    labels=parsed_agent_labels,
                )
            }
        benches = await runtime.inventory.list_benches(
            agent_id=agent_id,
            status=bench_status,
            kind=kind,
            health=health_filter,
            capability=capability,
            labels=_parse_labels(label or []),
            online=online,
        )
        if selected_agent_ids is not None:
            benches = [bench for bench in benches if bench.agent_id in selected_agent_ids]
        return {"items": [bench.model_dump(mode="json") for bench in benches]}

    @app.post("/api/v1/benches/{bench_id:path}/actions/probe", status_code=202)
    async def probe_bench(
        bench_id: str,
        body: BenchActionRequest,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        bench = await _action_bench(runtime, bench_id, capability="probe")
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.PROBE,
            payload={"owner": body.owner},
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            idempotency_key=idempotency_key or f"bench-probe:{uuid4()}",
            operation_type=RemoteCommandType.PROBE.value,
        )
        return _remote_operation_payload(command, operation)

    @app.post("/api/v1/benches/{bench_id:path}/actions/reset", status_code=202)
    async def reset_bench(
        bench_id: str,
        body: BenchActionRequest,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        bench = await _action_bench(runtime, bench_id, capability="reset")
        reservation = await _action_reservation(runtime, bench.id, body.owner)
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.RESET,
            payload={"owner": body.owner},
            expires_at=min(
                datetime.now(UTC) + timedelta(minutes=5),
                reservation.lease.valid_until,
            ),
            idempotency_key=idempotency_key or f"bench-reset:{uuid4()}",
            reservation_lease=reservation.lease,
            operation_type=RemoteCommandType.RESET.value,
        )
        return _remote_operation_payload(command, operation)

    @app.post("/api/v1/benches/{bench_id:path}/actions/read-serial", status_code=202)
    async def read_bench_serial(
        bench_id: str,
        body: SerialReadActionRequest,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        bench = await _action_bench(runtime, bench_id, capability="serial")
        serial_request = SerialReadRequest(
            timeout_seconds=body.timeout_seconds,
            until_pattern=body.until_pattern,
            max_lines=body.max_lines,
        )
        command, operation = await runtime.commands.create(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            command_type=RemoteCommandType.READ_SERIAL,
            payload={
                "owner": body.owner,
                "request": serial_request.model_dump(mode="json"),
            },
            expires_at=(
                datetime.now(UTC)
                + timedelta(seconds=max(60.0, serial_request.timeout_seconds + 30.0))
            ),
            idempotency_key=idempotency_key or f"bench-read-serial:{uuid4()}",
            operation_type=RemoteCommandType.READ_SERIAL.value,
        )
        return _remote_operation_payload(command, operation)

    @app.post("/api/v1/benches/{bench_id:path}/actions/flash", status_code=202)
    async def flash_bench(
        bench_id: str,
        firmware: Annotated[UploadFile, File()],
        owner: Annotated[str, Form(min_length=1, max_length=200)],
        _token: Annotated[ApiToken | None, Depends(firmware_write)],
        version: Annotated[str | None, Form(min_length=1, max_length=200)] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        bench = await _action_bench(runtime, bench_id, capability="firmware")
        reservation = await _action_reservation(runtime, bench.id, owner)
        command_key = idempotency_key or f"bench-flash:{uuid4()}"
        existing = await runtime.command_repository.get_by_idempotency_key(
            bench.agent_id,
            command_key,
        )
        if existing is not None:
            operation = await runtime.command_repository.get_operation_for_command(existing.id)
            return _remote_operation_payload(existing, operation)

        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await firmware.read(1024 * 1024):
                yield chunk

        staging_owner_id = uuid4()
        artifact = await runtime.platform_artifacts.upload(
            chunks(),
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=staging_owner_id,
            name=firmware.filename or "firmware.bin",
            artifact_type="firmware",
            content_type=firmware.content_type,
            metadata={"bench_id": bench.id, "owner": owner},
            idempotency_key=f"{command_key}:firmware",
        )
        descriptor = await runtime.workflow_artifacts.issue_download(
            agent_id=bench.agent_id,
            input_name="firmware",
            artifact_id=artifact.id,
            target_path=f"artifacts/{artifact.id}",
            idempotency_key=f"{command_key}:transfer",
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
        )
        response = _remote_operation_payload(command, operation)
        response["input_artifact"] = artifact.model_dump(mode="json")
        return response

    @app.get("/api/v1/benches/{bench_id:path}")
    async def get_bench(
        bench_id: str,
        _token: Annotated[ApiToken | None, Depends(benches_read)],
    ) -> object:
        bench = await runtime.inventory_repository.get(bench_id)
        if bench is None:
            raise BenchNotFoundError("Bench does not exist.", bench_id=bench_id)
        return bench.model_dump(mode="json")

    @app.post("/api/v1/reservations", status_code=status.HTTP_201_CREATED)
    async def create_reservation(
        body: ReservationCreateRequest,
        _token: Annotated[ApiToken | None, Depends(reservations_write)],
    ) -> dict[str, object]:
        bench = await runtime.inventory_repository.get(body.bench_id)
        if bench is None:
            raise BenchNotFoundError("Bench does not exist.", bench_id=body.bench_id)
        record = await runtime.reservations.grant(
            agent_id=bench.agent_id,
            bench_id=bench.id,
            owner=body.owner,
            idempotency_key=body.idempotency_key,
            reservation_duration_seconds=body.reservation_duration_seconds,
            lease_ttl_seconds=body.lease_ttl_seconds,
            metadata=body.metadata,
        )
        _require_active_reservation(record)
        return _coordinated_reservation_payload(record)

    @app.get("/api/v1/reservations")
    async def list_reservations(
        _token: Annotated[ApiToken | None, Depends(reservations_write)],
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
            agent_id=agent_id,
            states=lease_state,
            limit=limit,
        )
        if bench_id is not None:
            records = [record for record in records if record.reservation.bench_id == bench_id]
        if owner is not None:
            records = [record for record in records if record.reservation.owner == owner]
        return {"items": [_coordinated_reservation_payload(record) for record in records]}

    @app.get("/api/v1/reservations/{reservation_id}")
    async def get_reservation(
        reservation_id: UUID,
        _token: Annotated[ApiToken | None, Depends(reservations_write)],
    ) -> dict[str, object]:
        return _coordinated_reservation_payload(await runtime.reservations.get(reservation_id))

    @app.post("/api/v1/reservations/{reservation_id}/renew")
    async def renew_reservation(
        reservation_id: UUID,
        body: ReservationRenewRequest,
        _token: Annotated[ApiToken | None, Depends(reservations_write)],
    ) -> dict[str, object]:
        record = await runtime.reservations.renew(
            reservation_id,
            owner=body.owner,
            expected_lease_version=body.expected_lease_version,
            idempotency_key=body.idempotency_key,
            lease_ttl_seconds=body.lease_ttl_seconds,
        )
        _require_active_reservation(record)
        return _coordinated_reservation_payload(record)

    @app.post("/api/v1/reservations/{reservation_id}/release")
    async def release_reservation(
        reservation_id: UUID,
        body: ReservationReleaseRequest,
        _token: Annotated[ApiToken | None, Depends(reservations_write)],
    ) -> dict[str, object]:
        record = await runtime.reservations.release(
            reservation_id,
            owner=body.owner,
            expected_lease_version=body.expected_lease_version,
            idempotency_key=body.idempotency_key,
        )
        return _coordinated_reservation_payload(record)

    @app.post("/api/v1/workflows", status_code=status.HTTP_201_CREATED)
    async def register_workflow(
        definition: WorkflowDefinition,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
    ) -> object:
        stored = await runtime.workflow_repository.save_definition(definition)
        return stored.model_dump(mode="json")

    @app.get("/api/v1/workflows")
    async def list_workflows(
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
    ) -> dict[str, object]:
        definitions = await runtime.workflow_repository.list_definitions()
        return {"items": [definition.model_dump(mode="json") for definition in definitions]}

    @app.get("/api/v1/workflows/{workflow_name}")
    async def get_workflow(
        workflow_name: str,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> object:
        definition = await runtime.workflow_repository.get_definition(workflow_name, version)
        if definition is None:
            raise WorkflowNotFoundError(
                "Workflow does not exist.",
                workflow_name=workflow_name,
                workflow_version=version,
            )
        return definition.model_dump(mode="json")

    @app.post("/api/v1/workflows/{workflow_name}/runs", status_code=202)
    async def run_workflow(
        workflow_name: str,
        body: WorkflowRunRequest,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
    ) -> dict[str, object]:
        definition = await runtime.workflow_repository.get_definition(
            workflow_name,
            body.version,
        )
        if definition is None:
            raise WorkflowNotFoundError(
                "Workflow does not exist.",
                workflow_name=workflow_name,
                workflow_version=body.version,
            )
        dispatch = await runtime.workflows.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner=body.owner,
                idempotency_key=body.idempotency_key,
                inputs=body.inputs,
                bench_id=body.bench_id,
                kind=body.kind,
                location=body.location,
                bench_labels=body.bench_labels,
                agent_labels=body.agent_labels,
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
        _token: Annotated[ApiToken | None, Depends(operations_read)],
    ) -> dict[str, object]:
        operation = await _distributed_operation(runtime, operation_id)
        return _distributed_workflow_payload(operation)

    @app.post("/api/v1/workflow-runs/{operation_id:uuid}/cancel", status_code=202)
    async def cancel_distributed_workflow_run(
        operation_id: UUID,
        body: OperationCancelRequest,
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
    ) -> dict[str, object]:
        return await _cancel_distributed_operation(
            runtime,
            operation_id,
            owner=body.owner,
            reason=body.reason,
        )

    @app.get("/api/v1/workflow-runs/{operation_id:uuid}/results")
    async def distributed_workflow_results(
        operation_id: UUID,
        _token: Annotated[ApiToken | None, Depends(operations_read)],
    ) -> dict[str, object]:
        operation = await _distributed_operation(runtime, operation_id)
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
        _token: Annotated[ApiToken | None, Depends(operations_read)],
    ) -> Response:
        operation = await _distributed_operation(runtime, operation_id)
        workflow, steps = _distributed_workflow_result(operation)
        suite_name = str(workflow.get("workflow_name") or operation.operation_type)
        return Response(
            content=render_junit_xml(suite_name, build_test_results(steps)),
            media_type="application/xml",
        )

    @app.post("/api/v1/ci/sessions", status_code=status.HTTP_201_CREATED)
    async def create_ci_session(
        body: CiSessionCreateRequest,
        token: Annotated[ApiToken | None, Depends(ci_sessions)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=500),
        ] = None,
    ) -> dict[str, object]:
        requested_by = body.requested_by or (token.owner if token is not None else None)
        if requested_by is None:
            requested_by = body.actor or f"ci:{body.provider.value}:{body.external_run_id}"
        session = await runtime.ci.create(
            DistributedCiCreateRequest(
                provider=body.provider,
                external_run_id=body.external_run_id,
                requested_by=requested_by,
                bench_request=body.bench_request,
                repository=body.repository,
                ref=body.ref,
                commit_sha=body.commit_sha,
                actor=body.actor,
                idempotency_key=idempotency_key or body.idempotency_key,
                timeout_seconds=body.timeout_seconds,
            )
        )
        return await _ci_session_payload(runtime, session)

    @app.get("/api/v1/ci/sessions")
    async def list_ci_sessions(
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
        session_status: Annotated[CiSessionStatus | None, Query(alias="status")] = None,
        provider: CiProvider | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        sessions = await runtime.ci.list(
            status=session_status,
            provider=provider,
            limit=limit,
        )
        return {"items": [await _ci_session_payload(runtime, session) for session in sessions]}

    @app.get("/api/v1/ci/sessions/{session_id:uuid}")
    async def get_ci_session(
        session_id: UUID,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
        return await _ci_session_payload(runtime, await runtime.ci.get(session_id), details=True)

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/run", status_code=202)
    async def run_ci_session(
        session_id: UUID,
        body: CiSessionRunRequest,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
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
        )
        return await _ci_session_payload(runtime, session, details=True)

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/heartbeat")
    async def heartbeat_ci_session(
        session_id: UUID,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
        return await _ci_session_payload(runtime, await runtime.ci.heartbeat(session_id))

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/cancel")
    async def cancel_ci_session(
        session_id: UUID,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
        return await _ci_session_payload(
            runtime,
            await runtime.ci.cancel(session_id),
            details=True,
        )

    @app.post("/api/v1/ci/sessions/{session_id:uuid}/finalize")
    async def finalize_ci_session(
        session_id: UUID,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
        return await _ci_session_payload(
            runtime,
            await runtime.ci.cleanup(session_id),
            details=True,
        )

    @app.get("/api/v1/ci/sessions/{session_id:uuid}/artifacts")
    async def list_ci_session_artifacts(
        session_id: UUID,
        _token: Annotated[ApiToken | None, Depends(ci_sessions)],
    ) -> dict[str, object]:
        await runtime.ci.get(session_id, synchronize=False)
        platform_items = await runtime.platform_artifacts.list_for_owner(
            ArtifactOwnerType.CI_SESSION,
            session_id,
        )
        binding = await runtime.ci_repository.get_distributed_workflow(session_id)
        remote_items = (
            await runtime.remote_artifacts.list(command_id=binding.remote_command_id)
            if binding is not None
            else []
        )
        items = [item.model_dump(mode="json") for item in platform_items]
        items.extend(item.model_dump(mode="json") for item in remote_items)
        items.sort(key=lambda item: str(item.get("created_at", "")))
        return {"items": items}

    @app.get("/api/v1/operations")
    async def list_operations(
        _token: Annotated[ApiToken | None, Depends(operations_read)],
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        operation_status: Annotated[
            DistributedOperationStatus | None, Query(alias="status")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> dict[str, object]:
        operations = await runtime.operation_records.list(
            agent_id=agent_id,
            bench_id=bench_id,
            status=operation_status,
            limit=limit,
        )
        return {"items": [item.model_dump(mode="json") for item in operations]}

    @app.get("/api/v1/operations/{operation_id}")
    async def get_operation(
        operation_id: UUID,
        _token: Annotated[ApiToken | None, Depends(operations_read)],
    ) -> object:
        operation = await runtime.operation_records.get(operation_id)
        if operation is None:
            raise RemoteCommandNotFoundError("Distributed operation does not exist.")
        return operation.model_dump(mode="json")

    @app.get("/api/v1/operations/{operation_id}/artifacts/serial")
    async def get_operation_serial_output(
        operation_id: UUID,
        _token: Annotated[ApiToken | None, Depends(operations_read)],
    ) -> dict[str, str]:
        operation = await _distributed_operation(runtime, operation_id)
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
        _token: Annotated[ApiToken | None, Depends(workflows_run)],
    ) -> dict[str, object]:
        return await _cancel_distributed_operation(
            runtime,
            operation_id,
            owner=body.owner,
            reason=body.reason,
        )

    @app.post("/api/v1/operations/{operation_id}/reconcile", status_code=202)
    async def reconcile_operation(
        operation_id: UUID,
        _token: Annotated[ApiToken | None, Depends(agents_admin)],
    ) -> dict[str, str]:
        operation = await runtime.operation_records.get(operation_id)
        if operation is None:
            raise RemoteCommandNotFoundError("Distributed operation does not exist.")
        if not await runtime.hub.is_connected(operation.agent_id):
            raise AgentOfflineError("Agent is not connected.", agent_id=str(operation.agent_id))
        return {"request_id": str(await runtime.request_reconciliation(operation.agent_id))}

    @app.post("/api/v1/artifacts", status_code=status.HTTP_201_CREATED)
    async def upload_artifact(
        _token: Annotated[ApiToken | None, Depends(artifacts_write)],
        file: Annotated[UploadFile, File()],
        owner_type: Annotated[ArtifactOwnerType, Form()],
        owner_id: Annotated[UUID, Form()],
        artifact_type: Annotated[str, Form(min_length=1, max_length=200)],
        expected_sha256: Annotated[str | None, Form()] = None,
        idempotency_key: Annotated[str | None, Form(max_length=500)] = None,
    ) -> object:
        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await file.read(1024 * 1024):
                yield chunk

        record = await runtime.platform_artifacts.upload(
            chunks(),
            owner_type=owner_type,
            owner_id=owner_id,
            name=file.filename or "artifact.bin",
            artifact_type=artifact_type,
            content_type=file.content_type,
            expected_sha256=expected_sha256,
            idempotency_key=idempotency_key,
        )
        await runtime.artifact_store.stage_verified_file(
            record.id,
            record.sha256,
            record.size_bytes,
            await runtime.platform_artifacts.content_path(record.id),
            maximum_size_bytes=runtime.config.artifacts.max_upload_size_mb * 1024 * 1024,
        )
        return record.model_dump(mode="json")

    @app.get("/api/v1/artifacts")
    async def list_artifacts(
        _token: Annotated[ApiToken | None, Depends(artifacts_read)],
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
        platform_items = (
            await runtime.platform_artifacts.list_for_owner(owner_type, owner_id)
            if owner_type is not None and owner_id is not None
            else []
        )
        remote_items = await runtime.remote_artifacts.list(
            agent_id=agent_id,
            command_id=command_id,
            limit=limit,
        )
        items = [item.model_dump(mode="json") for item in platform_items]
        items.extend(item.model_dump(mode="json") for item in remote_items)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {"items": items[:limit]}

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: UUID,
        _token: Annotated[ApiToken | None, Depends(artifacts_read)],
        download: bool = False,
    ) -> object:
        try:
            record = await runtime.platform_artifacts.get(artifact_id)
        except ArtifactNotFoundError:
            remote = await runtime.remote_artifacts.get(artifact_id)
            if remote is None:
                raise ArtifactNotFoundError(
                    f"Artifact {artifact_id} does not exist.",
                    artifact_id=str(artifact_id),
                ) from None
            if download:
                return FileResponse(
                    runtime.artifact_store.open_verified(remote.id, remote.sha256),
                    media_type=remote.content_type,
                    filename=remote.name,
                )
            return remote.model_dump(mode="json")
        if download:
            return FileResponse(
                await runtime.platform_artifacts.content_path(artifact_id),
                media_type=record.content_type,
                filename=record.name,
            )
        return record.model_dump(mode="json")

    @app.get("/api/v1/artifacts/{artifact_id}/content")
    async def download_artifact_content(
        artifact_id: UUID,
        _token: Annotated[ApiToken | None, Depends(artifacts_read)],
    ) -> FileResponse:
        try:
            record = await runtime.platform_artifacts.get(artifact_id)
        except ArtifactNotFoundError:
            remote = await runtime.remote_artifacts.get(artifact_id)
            if remote is None:
                raise ArtifactNotFoundError(
                    f"Artifact {artifact_id} does not exist.",
                    artifact_id=str(artifact_id),
                ) from None
            return FileResponse(
                runtime.artifact_store.open_verified(remote.id, remote.sha256),
                media_type=remote.content_type,
                filename=remote.name,
            )
        return FileResponse(
            await runtime.platform_artifacts.content_path(artifact_id),
            media_type=record.content_type,
            filename=record.name,
        )

    @app.post("/api/v1/artifacts/{artifact_id}/transfers", status_code=201)
    async def issue_artifact_download(
        artifact_id: UUID,
        body: ArtifactTransferRequest,
        _token: Annotated[ApiToken | None, Depends(artifacts_write)],
    ) -> dict[str, object]:
        record = await runtime.platform_artifacts.get(artifact_id)
        issued = await runtime.artifacts.issue_download(
            agent_id=body.agent_id,
            artifact_id=record.id,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )
        return {
            "transfer": issued.transfer.model_dump(mode="json", exclude={"token_hash"}),
            "token": issued.plaintext_token.get_secret_value(),
            "download_url": (
                f"{runtime.config.control_plane.public_url}/api/v1/"
                f"artifact-transfers/{issued.transfer.id}/content"
            ),
        }

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
    ) -> FileResponse:
        token = _bearer_value(request.headers.get("Authorization"))
        path = await runtime.artifacts.download_path(transfer_id, token)
        return FileResponse(path, filename=str(transfer_id))

    return app


async def _action_bench(
    runtime: ControlPlaneRuntime,
    bench_id: str,
    *,
    capability: str,
) -> GlobalBenchRecord:
    bench = await runtime.inventory_repository.get(bench_id)
    if bench is None:
        raise BenchNotFoundError("Bench does not exist.", bench_id=bench_id)
    if capability not in bench.capabilities:
        raise CapabilityNotSupportedError(
            "Bench does not support the requested action.",
            bench_id=bench.id,
            required_capability=capability,
        )
    return bench


async def _action_reservation(
    runtime: ControlPlaneRuntime,
    bench_id: str,
    owner: str,
) -> CoordinatedReservationLease:
    reservation = await runtime.reservation_repository.get_current_for_bench(bench_id)
    if reservation is None:
        raise ReservationNotActiveError(
            "Mutating remote bench actions require an active reservation.",
            bench_id=bench_id,
        )
    if reservation.reservation.owner != owner:
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
) -> DistributedOperation:
    operation = await runtime.operation_records.get(operation_id)
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
    if operation.status in {
        DistributedOperationStatus.RUNNING,
        DistributedOperationStatus.UNKNOWN,
        DistributedOperationStatus.RECONCILING,
    }:
        return "running"
    return operation.status.value.casefold()


def _distributed_workflow_payload(operation: DistributedOperation) -> dict[str, object]:
    raw_workflow = (operation.result or {}).get("workflow_run")
    workflow = dict(raw_workflow) if isinstance(raw_workflow, dict) else {}
    local_workflow_run_id = workflow.get("id")
    workflow.update(
        {
            "id": str(operation.id),
            "local_workflow_run_id": local_workflow_run_id,
            "bench_id": operation.bench_id,
            "status": _distributed_workflow_status(operation),
            "progress": operation.progress,
            "message": operation.message,
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
    owner: str,
    reason: str | None,
) -> dict[str, object]:
    operation = await _distributed_operation(runtime, operation_id)
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
    command = await runtime.command_repository.get_command(operation.remote_command_id)
    if command is None:
        raise RemoteCommandNotFoundError("Remote command does not exist.")
    expected_owner = command.payload.get("owner")
    if not isinstance(expected_owner, str) and operation.reservation_id is not None:
        reservation = await runtime.reservations.get(operation.reservation_id)
        expected_owner = reservation.reservation.owner
    if isinstance(expected_owner, str) and expected_owner != owner:
        raise ReservationOwnerMismatchError(
            "Operation owner does not match the cancellation request.",
            operation_id=str(operation_id),
        )
    await runtime.commands.request_cancel(command.id, reason=reason)
    current = await _distributed_operation(runtime, operation_id)
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
) -> dict[str, object]:
    payload = await runtime.ci.details(session.id) if details else session.model_dump(mode="json")
    payload["cleanup_timeout_seconds"] = runtime.config.artifacts.finalization_timeout_seconds
    binding = await runtime.ci_repository.get_distributed_workflow(session.id)
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


def _require_scopes(
    runtime: ControlPlaneRuntime,
    *required: ApiTokenScope,
    allow_bootstrap: bool = False,
) -> Callable[..., object]:
    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)] = None,
    ) -> ApiToken | None:
        tokens = await runtime.token_service.list()
        if credentials is None and not tokens and allow_bootstrap:
            return None
        if credentials is None:
            raise AuthenticationRequiredError("An Authorization bearer token is required.")
        return await runtime.token_service.authenticate(credentials.credentials, required)

    return dependency


def _public_api_token(token: ApiToken) -> dict[str, object]:
    payload = token.model_dump(mode="json")
    payload.pop("token_hash", None)
    return payload


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


def _require_active_reservation(value: CoordinatedReservationLease) -> None:
    if value.state is not ReservationLeaseState.ACTIVE:
        raise ReservationLeaseInvalidError(
            "The Agent did not confirm the reservation lease.",
            reservation_id=str(value.reservation.id),
            lease_version=value.lease.lease_version,
            lease_state=value.state.value,
        )


_ERROR_STATUS = {
    "AUTHENTICATION_REQUIRED": 401,
    "INVALID_API_TOKEN": 401,
    "PERMISSION_DENIED": 403,
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
    "RESERVATION_OWNER_MISMATCH": 403,
    "RESERVATION_MAX_DURATION_EXCEEDED": 400,
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
