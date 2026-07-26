import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from lab_platform.agent.api.artifacts import create_artifact_router
from lab_platform.agent.api.auth import require_legacy_scopes
from lab_platform.agent.api.ci import create_ci_router
from lab_platform.agent.api.errors import (
    http_error_handler,
    internal_error_handler,
    platform_error_handler,
    validation_error_handler,
)
from lab_platform.agent.api.phase3 import create_phase3_router
from lab_platform.agent.api.results import create_results_router
from lab_platform.agent.api.tokens import create_token_router
from lab_platform.agent.runtime import LabAgent
from lab_platform.core import (
    VERSION,
    ArtifactTooLargeError,
    BenchNotReservedError,
    FirmwareFileTooLargeError,
    InvalidFirmwareFileError,
    PermissionDeniedError,
    PlatformError,
    RequestBodyTooLargeError,
)
from lab_platform.core.bench_catalog import BenchRecord
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    OperationStatus,
    OperationType,
    SerialReadRequest,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.exceptions import HTTPException
from starlette.types import Message


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OwnerRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=200)


class OperationAccepted(ApiModel):
    operation_id: UUID
    status: OperationStatus


class ApiError(ApiModel):
    code: str
    message: str
    details: dict[str, object]
    request_id: str


class ErrorEnvelope(ApiModel):
    error: ApiError


class SerialReadApiRequest(OwnerRequest):
    timeout_seconds: float = Field(default=10, gt=0, le=3600)
    until_pattern: str | None = None
    max_lines: int | None = Field(default=500, ge=1, le=100_000)
    include_timestamps: bool = True


LabelFilter = Annotated[str, StringConstraints(pattern=r"^[^:=\s]+[:=].+$")]


def create_app(agent: LabAgent) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await agent.start_background_workers()
        try:
            yield
        finally:
            await agent.stop_background_workers()

    app = FastAPI(
        title="Lab Platform Agent API",
        version=VERSION,
        description="Versioned local API for reserving and controlling lab benches.",
        lifespan=lifespan,
        responses={422: {"model": ErrorEnvelope, "description": "Request validation failed"}},
    )
    logger = logging.getLogger(agent.config.agent.name)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        identifier = request.headers.get("X-Request-ID", str(uuid4()))
        request.state.request_id = identifier
        started = time.perf_counter()
        status_code = 500
        request_limit_bytes = (
            getattr(agent.config.agent, "max_request_body_size_mb", 1) * 1024 * 1024
        )
        request_too_large = False
        request_limit_error: type[PlatformError] = RequestBodyTooLargeError
        request_limit_message = "Request body exceeds the configured size limit."
        try:
            artifact_upload = request.url.path == "/api/v1/artifacts"
            firmware_upload = request.url.path.startswith("/api/v1/benches/") and (
                request.url.path.endswith("/actions/flash")
            )
            if request.method == "POST" and (artifact_upload or firmware_upload):
                maximum_content_mb = (
                    agent.config.artifacts.max_upload_size_mb
                    if artifact_upload
                    else agent.config.artifacts.max_firmware_size_mb
                )
                request_limit_bytes = maximum_content_mb * 1024 * 1024 + 1024 * 1024
                if artifact_upload:
                    request_limit_error = ArtifactTooLargeError
                    request_limit_message = "Artifact request exceeds the configured upload limit."
                else:
                    request_limit_error = FirmwareFileTooLargeError
                    request_limit_message = "Firmware request exceeds the configured upload limit."
            content_length = request.headers.get("Content-Length")
            if (
                content_length is not None
                and content_length.isdecimal()
                and int(content_length) > request_limit_bytes
            ):
                error_response = await platform_error_handler(
                    request,
                    request_limit_error(
                        request_limit_message,
                        maximum_request_bytes=request_limit_bytes,
                    ),
                )
                status_code = error_response.status_code
                error_response.headers["X-Request-ID"] = identifier
                return error_response
            original_receive = request.receive
            received_bytes = 0

            async def receive_with_limit() -> Message:
                nonlocal received_bytes, request_too_large
                message = await original_receive()
                if message["type"] == "http.request":
                    received_bytes += len(message.get("body", b""))
                    if received_bytes > request_limit_bytes:
                        request_too_large = True
                        raise request_limit_error(
                            request_limit_message,
                            maximum_request_bytes=request_limit_bytes,
                        )
                return message

            request._receive = receive_with_limit
            response = await call_next(request)
            if request_too_large:
                response = await platform_error_handler(
                    request,
                    request_limit_error(
                        request_limit_message,
                        maximum_request_bytes=request_limit_bytes,
                    ),
                )
            status_code = response.status_code
            response.headers["X-Request-ID"] = identifier
            return response
        finally:
            logger.info(
                "HTTP request",
                extra={
                    "request_id": identifier,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )

    app.add_exception_handler(PlatformError, platform_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, internal_error_handler)

    router = APIRouter(prefix="/api/v1")
    authorize_benches = require_legacy_scopes(agent, ApiTokenScope.BENCHES_READ)
    authorize_reservations = require_legacy_scopes(
        agent,
        ApiTokenScope.RESERVATIONS_WRITE,
    )
    authorize_workflows = require_legacy_scopes(agent, ApiTokenScope.WORKFLOWS_RUN)
    authorize_operations = require_legacy_scopes(agent, ApiTokenScope.OPERATIONS_READ)

    @router.get("/health")
    async def health() -> dict[str, object]:
        return agent.health_payload()

    @router.get("/version")
    async def version() -> dict[str, str]:
        return {"version": VERSION}

    @router.get("/benches")
    async def list_benches(
        _token: Annotated[ApiToken | None, Depends(authorize_benches)],
        status: str | None = None,
        capability: str | None = None,
        reserved: bool | None = None,
        online: bool | None = None,
        available: bool | None = None,
        label: Annotated[list[LabelFilter] | None, Query()] = None,
    ) -> dict[str, object]:
        await agent.catalog.refresh()
        by_id = {bench.id: bench for bench in await agent.bench_service.list_benches()}
        for record in agent.catalog.list():
            current = by_id.get(record.id)
            if current is None:
                by_id[record.id] = _catalog_snapshot(record)
            elif not record.online:
                by_id[record.id] = current.model_copy(
                    update={"status": BenchStatus.OFFLINE, "online": False, "powered": None}
                )
        benches = sorted(by_id.values(), key=lambda bench: bench.id)
        if status is not None:
            benches = [bench for bench in benches if bench.status.value == status.lower()]
        if capability is not None:
            benches = [
                bench
                for bench in benches
                if capability.lower() in {item.lower() for item in bench.capabilities}
            ]
        if reserved is not None:
            benches = [bench for bench in benches if (bench.reserved_by is not None) is reserved]
        if online is not None:
            benches = [bench for bench in benches if bench.online is online]
        if available is not None:
            benches = [
                bench
                for bench in benches
                if (bench.online and bench.reserved_by is None) is available
            ]
        labels = _parse_labels(label or [])
        if labels:
            benches = [
                bench
                for bench in benches
                if all(
                    agent.catalog.get(bench.id).labels.get(key) == value
                    for key, value in labels.items()
                )
            ]
        return {"items": [_bench_payload(agent, bench) for bench in benches]}

    @router.get("/benches/{bench_id}")
    async def get_bench(
        bench_id: str,
        _token: Annotated[ApiToken | None, Depends(authorize_benches)],
    ) -> object:
        await agent.catalog.refresh()
        catalog_record = agent.catalog.get(bench_id)
        if not catalog_record.online:
            return _bench_payload(agent, _catalog_snapshot(catalog_record))
        return _bench_payload(agent, await agent.bench_service.get_bench(bench_id))

    @router.post("/benches/{bench_id}/reservation", status_code=201)
    async def reserve(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        reservation = await agent.reservation_service.reserve(bench_id, owner)
        return reservation.model_dump(mode="json", exclude_none=True)

    @router.get("/benches/{bench_id}/reservation")
    async def get_reservation(
        bench_id: str,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        reservation = await agent.reservation_service.get_reservation(bench_id)
        if reservation is None:
            raise BenchNotReservedError(
                f"Bench {bench_id} has no active reservation.", bench_id=bench_id
            )
        _require_owner(reservation.owner, token)
        return reservation.model_dump(mode="json", exclude_none=True)

    @router.delete("/benches/{bench_id}/reservation", status_code=204)
    async def release(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> Response:
        owner = _authorized_owner(request.owner, token)
        await agent.reservation_service.release(bench_id, owner)
        return Response(status_code=204)

    async def submit_power(
        bench_id: str, owner: str, operation_type: OperationType
    ) -> OperationAccepted:
        actions = {
            OperationType.POWER_ON: agent.bench_service.power_on,
            OperationType.POWER_OFF: agent.bench_service.power_off,
            OperationType.POWER_CYCLE: agent.bench_service.power_cycle,
        }
        operation = await actions[operation_type](bench_id, owner)
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.post(
        "/benches/{bench_id}/actions/power-on",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_on(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> OperationAccepted:
        return await submit_power(
            bench_id, _authorized_owner(request.owner, token), OperationType.POWER_ON
        )

    @router.post(
        "/benches/{bench_id}/actions/power-off",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_off(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> OperationAccepted:
        return await submit_power(
            bench_id, _authorized_owner(request.owner, token), OperationType.POWER_OFF
        )

    @router.post(
        "/benches/{bench_id}/actions/power-cycle",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_cycle(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> OperationAccepted:
        return await submit_power(
            bench_id, _authorized_owner(request.owner, token), OperationType.POWER_CYCLE
        )

    @router.post("/benches/{bench_id}/actions/probe")
    async def probe(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> object:
        health = await agent.bench_service.probe(
            bench_id,
            _authorized_owner(request.owner, token),
        )
        return health.model_dump(mode="json")

    @router.post(
        "/benches/{bench_id}/actions/reset",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def reset(
        bench_id: str,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> OperationAccepted:
        operation = await agent.bench_service.reset(
            bench_id,
            _authorized_owner(request.owner, token),
        )
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.post(
        "/benches/{bench_id}/actions/read-serial",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def read_serial(
        bench_id: str,
        request: SerialReadApiRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> OperationAccepted:
        operation = await agent.bench_service.read_serial(
            bench_id,
            _authorized_owner(request.owner, token),
            SerialReadRequest(
                timeout_seconds=request.timeout_seconds,
                until_pattern=request.until_pattern,
                max_lines=request.max_lines,
                include_timestamps=request.include_timestamps,
            ),
        )
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.post(
        "/benches/{bench_id}/actions/flash",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def flash(
        bench_id: str,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
        owner: Annotated[str, Form(min_length=1, max_length=200)],
        firmware: Annotated[UploadFile, File()],
        version: Annotated[str | None, Form()] = None,
    ) -> OperationAccepted:
        owner = _authorized_owner(owner, token)
        firmware_input = await _store_firmware(agent, firmware, version)
        operation = await agent.bench_service.flash_firmware(bench_id, owner, firmware_input)
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.get("/operations/{operation_id}")
    async def get_operation(
        operation_id: UUID,
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
    ) -> object:
        operation = await agent.operation_service.get_operation(operation_id)
        _require_owner(operation.requested_by, token)
        return operation.model_dump(mode="json")

    @router.get("/operations/{operation_id}/artifacts")
    async def list_operation_artifacts(
        operation_id: UUID,
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
    ) -> dict[str, object]:
        operation = await agent.operation_service.get_operation(operation_id)
        _require_owner(operation.requested_by, token)
        artifacts = await agent.operation_service.list_artifacts(operation_id)
        return {"items": [artifact.model_dump(mode="json") for artifact in artifacts]}

    @router.get("/operations/{operation_id}/artifacts/serial")
    async def read_serial_artifact(
        operation_id: UUID,
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
    ) -> dict[str, str]:
        operation = await agent.operation_service.get_operation(operation_id)
        _require_owner(operation.requested_by, token)
        return {"text": await agent.operation_service.read_serial_artifact(operation_id)}

    @router.get("/operations")
    async def list_operations(
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
        bench_id: str | None = None,
        status: OperationStatus | None = None,
        operation_type: Annotated[OperationType | None, Query(alias="type")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, object]:
        operations = await agent.operation_service.list_operations(
            bench_id=bench_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
        )
        if token is not None:
            operations = [
                operation for operation in operations if operation.requested_by == token.owner
            ]
        return {"items": [operation.model_dump(mode="json") for operation in operations]}

    @router.post("/operations/{operation_id}/cancel")
    async def cancel_operation(
        operation_id: UUID,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        operation = await agent.operation_service.cancel_operation(operation_id, owner)
        return operation.model_dump(mode="json")

    @router.get("/events")
    async def list_events(
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, object]:
        events = await agent.event_service.list_events(
            bench_id=bench_id,
            event_type=event_type,
            after=after,
            before=before,
            limit=limit,
        )
        if token is not None:
            events = [event for event in events if event.actor == token.owner]
        return {"items": [event.model_dump(mode="json") for event in events]}

    app.include_router(router)
    app.include_router(create_phase3_router(agent))
    app.include_router(create_token_router(agent))
    app.include_router(create_results_router(agent))
    app.include_router(create_ci_router(agent))
    app.include_router(create_artifact_router(agent))

    @app.get("/health", include_in_schema=False)
    async def legacy_health() -> dict[str, object]:
        return agent.health_payload()

    @app.get("/version", include_in_schema=False)
    async def legacy_version() -> dict[str, str]:
        return {"version": VERSION}

    @app.get("/plugins", include_in_schema=False)
    async def legacy_plugins(
        _token: Annotated[ApiToken | None, Depends(authorize_benches)],
    ) -> list[object]:
        return [plugin.model_dump(mode="json") for plugin in agent.plugins()]

    @app.get("/benches", include_in_schema=False)
    async def legacy_benches(
        _token: Annotated[ApiToken | None, Depends(authorize_benches)],
    ) -> list[object]:
        return [bench.model_dump(mode="json") for bench in agent.benches()]

    return app


def _authorized_owner(owner: str, token: ApiToken | None) -> str:
    _require_owner(owner, token)
    return token.owner if token is not None else owner


def _require_owner(owner: str, token: ApiToken | None) -> None:
    if token is not None and owner != token.owner:
        raise PermissionDeniedError("The resource belongs to another API token owner.")


def _bench_payload(agent: LabAgent, bench: BenchSnapshot) -> dict[str, object]:
    snapshot = bench.model_dump(mode="json")
    record = agent.catalog.get(str(snapshot["id"]))
    return {
        **snapshot,
        "backend_id": record.backend_id,
        "target_type": record.target_type,
        "health": record.health.value,
        "labels": record.labels,
        "last_seen_at": record.last_seen_at.isoformat() if record.last_seen_at else None,
    }


def _catalog_snapshot(record: BenchRecord) -> BenchSnapshot:
    return BenchSnapshot(
        id=record.id,
        name=record.name,
        status=BenchStatus.OFFLINE,
        online=False,
        powered=None,
        capabilities=sorted(record.capabilities),
    )


def _parse_labels(values: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition(":")
        if not separator:
            key, separator, item = value.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Invalid bench label filter: {value}")
        labels[key.strip()] = item
    return labels


async def _store_firmware(
    agent: LabAgent, upload: UploadFile, version: str | None
) -> FirmwareInput:
    filename = Path(upload.filename or "firmware.bin").name
    if not filename or filename in {".", ".."}:
        raise InvalidFirmwareFileError("The firmware filename is invalid.")
    max_bytes = agent.config.artifacts.max_firmware_size_mb * 1024 * 1024
    incoming = agent.artifacts_directory / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    temporary = incoming / str(uuid4())
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise FirmwareFileTooLargeError(
                        "Firmware exceeds the "
                        f"{agent.config.artifacts.max_firmware_size_mb} MB limit.",
                        max_size_bytes=max_bytes,
                    )
                digest.update(chunk)
                stream.write(chunk)
        if size == 0:
            raise InvalidFirmwareFileError("The firmware file is empty.")
        checksum = digest.hexdigest()
        directory = agent.artifacts_directory / checksum
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        if destination.exists():
            temporary.unlink()
        else:
            temporary.replace(destination)
        return FirmwareInput(
            filename=filename,
            local_path=destination,
            sha256=checksum,
            size_bytes=size,
            version=version,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
