from __future__ import annotations

from typing import Any


class PlatformError(RuntimeError):
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigurationError(PlatformError):
    code = "CONFIGURATION_ERROR"


class BackendNotFoundError(PlatformError):
    code = "BACKEND_NOT_FOUND"


class BackendUnavailableError(PlatformError):
    code = "BACKEND_UNAVAILABLE"


class BenchNotFoundError(PlatformError):
    code = "BENCH_NOT_FOUND"


class BenchOfflineError(PlatformError):
    code = "BENCH_OFFLINE"


class BenchAlreadyReservedError(PlatformError):
    code = "BENCH_ALREADY_RESERVED"


class BenchNotReservedError(PlatformError):
    code = "BENCH_NOT_RESERVED"


class ReservationOwnerMismatchError(PlatformError):
    code = "RESERVATION_OWNER_MISMATCH"


class ReservationNotFoundError(PlatformError):
    code = "RESERVATION_NOT_FOUND"


class ReservationTimeConflictError(PlatformError):
    code = "RESERVATION_TIME_CONFLICT"


class ReservationMaxDurationExceededError(PlatformError):
    code = "RESERVATION_MAX_DURATION_EXCEEDED"


class ReservationNotActiveError(PlatformError):
    code = "RESERVATION_NOT_ACTIVE"


class ReservationAlreadyExpiredError(PlatformError):
    code = "RESERVATION_ALREADY_EXPIRED"


class ReservationExtensionConflictError(PlatformError):
    code = "RESERVATION_EXTENSION_CONFLICT"


class QueueDisabledError(PlatformError):
    code = "QUEUE_DISABLED"


class QueueEntryNotFoundError(PlatformError):
    code = "QUEUE_ENTRY_NOT_FOUND"


class QueueOwnerMismatchError(PlatformError):
    code = "QUEUE_OWNER_MISMATCH"


class StaleOperationLockError(PlatformError):
    code = "STALE_OPERATION_LOCK"


class SchedulerFailureError(PlatformError):
    code = "SCHEDULER_FAILURE"


class RecoveryFailureError(PlatformError):
    code = "RECOVERY_FAILURE"


class CapabilityNotSupportedError(PlatformError):
    code = "CAPABILITY_NOT_SUPPORTED"


class OperationNotFoundError(PlatformError):
    code = "OPERATION_NOT_FOUND"


class OperationNotCancellableError(PlatformError):
    code = "OPERATION_NOT_CANCELLABLE"


class BenchOperationInProgressError(PlatformError):
    code = "BENCH_OPERATION_IN_PROGRESS"


class FirmwareFileTooLargeError(PlatformError):
    code = "FIRMWARE_FILE_TOO_LARGE"


class InvalidFirmwareFileError(PlatformError):
    code = "INVALID_FIRMWARE_FILE"


class BackendFailureError(PlatformError):
    code = "BACKEND_FAILURE"


class BackendTimeoutError(PlatformError):
    code = "BACKEND_TIMEOUT"


class SimulationFailureError(PlatformError):
    code = "SIMULATION_FAILURE"


class OperationArtifactNotFoundError(PlatformError):
    code = "OPERATION_ARTIFACT_NOT_FOUND"
