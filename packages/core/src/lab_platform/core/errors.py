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


class AuthenticationRequiredError(PlatformError):
    code = "AUTHENTICATION_REQUIRED"


class AuthenticationFailedError(PlatformError):
    code = "AUTHENTICATION_FAILED"


class InvalidCredentialsError(PlatformError):
    code = "INVALID_CREDENTIALS"


class SessionExpiredError(PlatformError):
    code = "SESSION_EXPIRED"


class SessionRevokedError(PlatformError):
    code = "SESSION_REVOKED"


class TokenExpiredError(PlatformError):
    code = "TOKEN_EXPIRED"


class TokenRevokedError(PlatformError):
    code = "TOKEN_REVOKED"


class InvalidApiTokenError(PlatformError):
    code = "INVALID_API_TOKEN"


class PermissionDeniedError(PlatformError):
    code = "PERMISSION_DENIED"


class UserNotFoundError(PlatformError):
    code = "USER_NOT_FOUND"


class UserDisabledError(PlatformError):
    code = "USER_DISABLED"


class UserLockedError(PlatformError):
    code = "USER_LOCKED"


class UsernameAlreadyExistsError(PlatformError):
    code = "USERNAME_ALREADY_EXISTS"


class ServiceAccountNotFoundError(PlatformError):
    code = "SERVICE_ACCOUNT_NOT_FOUND"


class ServiceAccountDisabledError(PlatformError):
    code = "SERVICE_ACCOUNT_DISABLED"


class TeamNotFoundError(PlatformError):
    code = "TEAM_NOT_FOUND"


class TeamAlreadyExistsError(PlatformError):
    code = "TEAM_ALREADY_EXISTS"


class TeamMembershipNotFoundError(PlatformError):
    code = "TEAM_MEMBERSHIP_NOT_FOUND"


class RoleAssignmentNotFoundError(PlatformError):
    code = "ROLE_ASSIGNMENT_NOT_FOUND"


class RoleAssignmentConflictError(PlatformError):
    code = "ROLE_ASSIGNMENT_CONFLICT"


class RoleNotAllowedError(PlatformError):
    code = "ROLE_NOT_ALLOWED"


class ResourceAccessDeniedError(PlatformError):
    code = "RESOURCE_ACCESS_DENIED"


class OrganisationNotFoundError(PlatformError):
    code = "ORGANISATION_NOT_FOUND"


class OrganisationSuspendedError(PlatformError):
    code = "ORGANISATION_SUSPENDED"


class CrossOrganisationAccessDeniedError(PlatformError):
    code = "CROSS_ORGANISATION_ACCESS_DENIED"


class OidcConfigurationInvalidError(PlatformError):
    code = "OIDC_CONFIGURATION_INVALID"


class OidcLoginFailedError(PlatformError):
    code = "OIDC_LOGIN_FAILED"


class OidcIdentityNotMappedError(PlatformError):
    code = "OIDC_IDENTITY_NOT_MAPPED"


class AuditEventNotFoundError(PlatformError):
    code = "AUDIT_EVENT_NOT_FOUND"


class LoginRateLimitExceededError(PlatformError):
    code = "LOGIN_RATE_LIMIT_EXCEEDED"


class ArtifactNotFoundError(PlatformError):
    code = "ARTIFACT_NOT_FOUND"


class InvalidArtifactError(PlatformError):
    code = "INVALID_ARTIFACT"


class ArtifactTooLargeError(PlatformError):
    code = "ARTIFACT_TOO_LARGE"


class RequestBodyTooLargeError(PlatformError):
    code = "REQUEST_BODY_TOO_LARGE"


class ArtifactChecksumMismatchError(PlatformError):
    code = "ARTIFACT_CHECKSUM_MISMATCH"


class CiSessionNotFoundError(PlatformError):
    code = "CI_SESSION_NOT_FOUND"


class CiSessionConflictError(PlatformError):
    code = "CI_SESSION_CONFLICT"


class NoCompatibleBenchError(PlatformError):
    code = "NO_COMPATIBLE_BENCH"


class BenchWaitTimeoutError(PlatformError):
    code = "BENCH_WAIT_TIMEOUT"


class CiCleanupError(PlatformError):
    code = "CI_CLEANUP_FAILED"
