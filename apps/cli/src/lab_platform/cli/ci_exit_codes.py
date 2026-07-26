from __future__ import annotations

from enum import IntEnum


class CiExitCode(IntEnum):
    SUCCESS = 0
    WORKFLOW_FAILED = 10
    HARDWARE_TEST_FAILED = 11
    NO_COMPATIBLE_BENCH = 12
    BENCH_WAIT_TIMEOUT = 13
    AUTHENTICATION_FAILED = 14
    ARTIFACT_UPLOAD_FAILED = 15
    WORKFLOW_CANCELLED = 16
    SESSION_TIMED_OUT = 17
    CLEANUP_FAILED = 18
    BACKEND_UNAVAILABLE = 19
    CLIENT_OR_PROTOCOL_ERROR = 20


_ERROR_CODES: dict[str, CiExitCode] = {
    "WORKFLOW_FAILED": CiExitCode.WORKFLOW_FAILED,
    "WORKFLOW_STEP_FAILED": CiExitCode.WORKFLOW_FAILED,
    "WORKFLOW_ASSERTION_FAILED": CiExitCode.HARDWARE_TEST_FAILED,
    "HARDWARE_TEST_FAILED": CiExitCode.HARDWARE_TEST_FAILED,
    "NO_COMPATIBLE_BENCH": CiExitCode.NO_COMPATIBLE_BENCH,
    "BENCH_WAIT_TIMEOUT": CiExitCode.BENCH_WAIT_TIMEOUT,
    "AUTHENTICATION_FAILED": CiExitCode.AUTHENTICATION_FAILED,
    "INVALID_API_TOKEN": CiExitCode.AUTHENTICATION_FAILED,
    "TOKEN_EXPIRED": CiExitCode.AUTHENTICATION_FAILED,
    "TOKEN_REVOKED": CiExitCode.AUTHENTICATION_FAILED,
    "INSUFFICIENT_SCOPE": CiExitCode.AUTHENTICATION_FAILED,
    "ARTIFACT_UPLOAD_FAILED": CiExitCode.ARTIFACT_UPLOAD_FAILED,
    "ARTIFACT_CHECKSUM_MISMATCH": CiExitCode.ARTIFACT_UPLOAD_FAILED,
    "WORKFLOW_CANCELLED": CiExitCode.WORKFLOW_CANCELLED,
    "CI_SESSION_CANCELLED": CiExitCode.WORKFLOW_CANCELLED,
    "CI_SESSION_TIMED_OUT": CiExitCode.SESSION_TIMED_OUT,
    "HEARTBEAT_TIMED_OUT": CiExitCode.SESSION_TIMED_OUT,
    "CI_CLEANUP_FAILED": CiExitCode.CLEANUP_FAILED,
    "BACKEND_UNAVAILABLE": CiExitCode.BACKEND_UNAVAILABLE,
}


def exit_code_for_error(
    error_code: str | None,
    *,
    http_status: int | None = None,
) -> CiExitCode:
    """Map stable API errors to the public CI process exit-code contract."""

    normalized = (error_code or "").strip().upper()
    if normalized in _ERROR_CODES:
        return _ERROR_CODES[normalized]
    if normalized.startswith("ARTIFACT_"):
        return CiExitCode.ARTIFACT_UPLOAD_FAILED
    if normalized.startswith(("BACKEND_", "DEVICE_", "SERIAL_", "ESPTOOL_")):
        return CiExitCode.BACKEND_UNAVAILABLE
    if http_status in {401, 403}:
        return CiExitCode.AUTHENTICATION_FAILED
    if http_status in {502, 503, 504}:
        return CiExitCode.BACKEND_UNAVAILABLE
    return CiExitCode.CLIENT_OR_PROTOCOL_ERROR


def exit_code_for_status(
    status: str,
    *,
    cleanup_succeeded: bool = True,
    error_code: str | None = None,
) -> CiExitCode:
    """Resolve a terminal session status while giving cleanup failure precedence."""

    if not cleanup_succeeded:
        return CiExitCode.CLEANUP_FAILED
    normalized = status.strip().casefold()
    if normalized in {"succeeded", "success", "passed", "completed"}:
        return CiExitCode.SUCCESS
    if normalized in {"cancel_requested", "cancelled", "canceled"}:
        return CiExitCode.WORKFLOW_CANCELLED
    if normalized in {"timed_out", "timeout", "abandoned"}:
        return CiExitCode.SESSION_TIMED_OUT
    if error_code is not None:
        return exit_code_for_error(error_code)
    return CiExitCode.WORKFLOW_FAILED
