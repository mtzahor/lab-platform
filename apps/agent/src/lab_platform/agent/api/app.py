from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from lab_platform.agent.api.errors import (
    http_error_handler,
    internal_error_handler,
    platform_error_handler,
    validation_error_handler,
)
from lab_platform.agent.runtime import LabAgent
from lab_platform.core import (
    BenchNotReservedError,
    FirmwareFileTooLargeError,
    InvalidFirmwareFileError,
    PlatformError,
)
from lab_platform.models import FirmwareInput, OperationStatus, OperationType
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OwnerRequest(ApiModel):
    owner: str = Field(min_length=1, max_length=200)


class OperationAccepted(ApiModel):
    operation_id: UUID
    status: OperationStatus


def create_app(agent: LabAgent) -> FastAPI:
    app = FastAPI(
        title="Lab Platform Agent API",
        version="0.2.0-alpha",
        description="Versioned local API for reserving and controlling lab benches.",
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
        try:
            response = await call_next(request)
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

    @router.get("/health")
    async def health() -> dict[str, object]:
        return agent.health_payload()

    @router.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "0.2.0-alpha"}

    @router.get("/benches")
    async def list_benches(
        status: str | None = None,
        capability: str | None = None,
        reserved: bool | None = None,
    ) -> dict[str, object]:
        benches = await agent.bench_service.list_benches()
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
        return {"items": [bench.model_dump(mode="json") for bench in benches]}

    @router.get("/benches/{bench_id}")
    async def get_bench(bench_id: str) -> object:
        return (await agent.bench_service.get_bench(bench_id)).model_dump(mode="json")

    @router.post("/benches/{bench_id}/reservation", status_code=201)
    async def reserve(bench_id: str, request: OwnerRequest) -> object:
        reservation = await agent.reservation_service.reserve(bench_id, request.owner)
        return reservation.model_dump(mode="json", exclude_none=True)

    @router.get("/benches/{bench_id}/reservation")
    async def get_reservation(bench_id: str) -> object:
        reservation = await agent.reservation_service.get_reservation(bench_id)
        if reservation is None:
            raise BenchNotReservedError(
                f"Bench {bench_id} has no active reservation.", bench_id=bench_id
            )
        return reservation.model_dump(mode="json", exclude_none=True)

    @router.delete("/benches/{bench_id}/reservation", status_code=204)
    async def release(bench_id: str, request: OwnerRequest) -> Response:
        await agent.reservation_service.release(bench_id, request.owner)
        return Response(status_code=204)

    async def submit_power(
        bench_id: str, request: OwnerRequest, operation_type: OperationType
    ) -> OperationAccepted:
        actions = {
            OperationType.POWER_ON: agent.bench_service.power_on,
            OperationType.POWER_OFF: agent.bench_service.power_off,
            OperationType.POWER_CYCLE: agent.bench_service.power_cycle,
        }
        operation = await actions[operation_type](bench_id, request.owner)
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.post(
        "/benches/{bench_id}/actions/power-on",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_on(bench_id: str, request: OwnerRequest) -> OperationAccepted:
        return await submit_power(bench_id, request, OperationType.POWER_ON)

    @router.post(
        "/benches/{bench_id}/actions/power-off",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_off(bench_id: str, request: OwnerRequest) -> OperationAccepted:
        return await submit_power(bench_id, request, OperationType.POWER_OFF)

    @router.post(
        "/benches/{bench_id}/actions/power-cycle",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def power_cycle(bench_id: str, request: OwnerRequest) -> OperationAccepted:
        return await submit_power(bench_id, request, OperationType.POWER_CYCLE)

    @router.post(
        "/benches/{bench_id}/actions/flash",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def flash(
        bench_id: str,
        owner: Annotated[str, Form(min_length=1, max_length=200)],
        firmware: Annotated[UploadFile, File()],
        version: Annotated[str | None, Form()] = None,
    ) -> OperationAccepted:
        firmware_input = await _store_firmware(agent, firmware, version)
        operation = await agent.bench_service.flash_firmware(bench_id, owner, firmware_input)
        return OperationAccepted(operation_id=operation.id, status=operation.status)

    @router.get("/operations/{operation_id}")
    async def get_operation(operation_id: UUID) -> object:
        operation = await agent.operation_service.get_operation(operation_id)
        return operation.model_dump(mode="json")

    @router.get("/operations")
    async def list_operations(
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
        return {"items": [operation.model_dump(mode="json") for operation in operations]}

    @router.post("/operations/{operation_id}/cancel")
    async def cancel_operation(operation_id: UUID, request: OwnerRequest) -> object:
        operation = await agent.operation_service.cancel_operation(operation_id, request.owner)
        return operation.model_dump(mode="json")

    @router.get("/events")
    async def list_events(
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
        return {"items": [event.model_dump(mode="json") for event in events]}

    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def legacy_health() -> dict[str, object]:
        return agent.health_payload()

    @app.get("/version", include_in_schema=False)
    async def legacy_version() -> dict[str, str]:
        return {"version": "0.2.0-alpha"}

    @app.get("/plugins", include_in_schema=False)
    async def legacy_plugins() -> list[object]:
        return [plugin.model_dump(mode="json") for plugin in agent.plugins()]

    @app.get("/benches", include_in_schema=False)
    async def legacy_benches() -> list[object]:
        return [bench.model_dump(mode="json") for bench in agent.benches()]

    return app


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
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
