from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from lab_platform.core import PlatformError
from starlette.exceptions import HTTPException

ERROR_STATUS = {
    "BENCH_NOT_FOUND": 404,
    "BENCH_OFFLINE": 409,
    "BENCH_ALREADY_RESERVED": 409,
    "BENCH_NOT_RESERVED": 404,
    "RESERVATION_OWNER_MISMATCH": 403,
    "CAPABILITY_NOT_SUPPORTED": 409,
    "BENCH_OPERATION_IN_PROGRESS": 409,
    "OPERATION_NOT_FOUND": 404,
    "OPERATION_NOT_CANCELLABLE": 409,
    "FIRMWARE_FILE_TOO_LARGE": 413,
    "INVALID_FIRMWARE_FILE": 400,
    "BACKEND_FAILURE": 503,
    "BACKEND_TIMEOUT": 503,
    "SIMULATION_FAILURE": 503,
    "CONFIGURATION_ERROR": 500,
    "INTERNAL_ERROR": 500,
}


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", uuid4()))


async def platform_error_handler(request: Request, exc: PlatformError) -> JSONResponse:
    return error_response(
        request,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        status_code=ERROR_STATUS.get(exc.code, 500),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details: dict[str, object] = {"errors": [_serializable_error(error) for error in exc.errors()]}
    return error_response(
        request,
        code="VALIDATION_ERROR",
        message="The request did not pass validation.",
        details=details,
        status_code=422,
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        request,
        code="INTERNAL_ERROR",
        message="An unexpected internal error occurred.",
        details={},
        status_code=500,
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
    return error_response(
        request,
        code=code,
        message=str(exc.detail),
        details={},
        status_code=exc.status_code,
    )


def error_response(
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
                "request_id": request_id(request),
            }
        },
    )


def _serializable_error(error: dict[str, object]) -> dict[str, object]:
    location = error.get("loc", ())
    return {
        "type": str(error.get("type", "validation_error")),
        "loc": list(location) if isinstance(location, (list, tuple)) else [str(location)],
        "msg": str(error.get("msg", "Invalid value")),
    }
