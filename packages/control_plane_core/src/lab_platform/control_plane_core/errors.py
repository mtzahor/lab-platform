from lab_platform.core.errors import PlatformError


class AgentNotFoundError(PlatformError):
    code = "AGENT_NOT_FOUND"


class AgentRevokedError(PlatformError):
    code = "AGENT_REVOKED"


class AgentIncompatibleError(PlatformError):
    code = "AGENT_INCOMPATIBLE"


class AgentOfflineError(PlatformError):
    code = "AGENT_OFFLINE"


class AgentDegradedError(PlatformError):
    code = "AGENT_DEGRADED"


class AgentDrainingError(PlatformError):
    code = "AGENT_DRAINING"


class AgentEnrollmentTokenInvalidError(PlatformError):
    code = "AGENT_ENROLLMENT_TOKEN_INVALID"


class AgentEnrollmentTokenExpiredError(PlatformError):
    code = "AGENT_ENROLLMENT_TOKEN_EXPIRED"


class AgentEnrollmentTokenUsedError(PlatformError):
    code = "AGENT_ENROLLMENT_TOKEN_USED"


class AgentAuthenticationFailedError(PlatformError):
    code = "AGENT_AUTHENTICATION_FAILED"


class RemoteCommandNotFoundError(PlatformError):
    code = "REMOTE_COMMAND_NOT_FOUND"


class RemoteCommandExpiredError(PlatformError):
    code = "REMOTE_COMMAND_EXPIRED"


class RemoteCommandRejectedError(PlatformError):
    code = "REMOTE_COMMAND_REJECTED"


class RemoteCommandDuplicateError(PlatformError):
    code = "REMOTE_COMMAND_DUPLICATE"


class RemoteCommandDeliveryFailedError(PlatformError):
    code = "REMOTE_COMMAND_DELIVERY_FAILED"


class RemoteOperationStateUnknownError(PlatformError):
    code = "REMOTE_OPERATION_STATE_UNKNOWN"


class RemoteOperationReconciliationTimeoutError(PlatformError):
    code = "REMOTE_OPERATION_RECONCILIATION_TIMEOUT"


class AgentRestartedDuringOperationError(PlatformError):
    code = "AGENT_RESTARTED_DURING_OPERATION"


class ReservationLeaseInvalidError(PlatformError):
    code = "RESERVATION_LEASE_INVALID"


class ReservationLeaseExpiredError(PlatformError):
    code = "RESERVATION_LEASE_EXPIRED"


class ReservationLeaseVersionMismatchError(PlatformError):
    code = "RESERVATION_LEASE_VERSION_MISMATCH"


class ArtifactTransferFailedError(PlatformError):
    code = "ARTIFACT_TRANSFER_FAILED"


class ArtifactTransferTokenExpiredError(PlatformError):
    code = "ARTIFACT_TRANSFER_TOKEN_EXPIRED"


class InventorySyncFailedError(PlatformError):
    code = "INVENTORY_SYNC_FAILED"


class BenchAgentMismatchError(PlatformError):
    code = "BENCH_AGENT_MISMATCH"


class GlobalBenchIdConflictError(PlatformError):
    code = "GLOBAL_BENCH_ID_CONFLICT"
