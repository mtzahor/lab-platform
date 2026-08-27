from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from lab_platform.agent_protocol import MessageType
from lab_platform.agent_protocol.commands import (
    ArtifactUploadRequestPayload,
    DrainAgentPayload,
    InventoryRefreshRequestPayload,
    ReconciliationRequestPayload,
)
from lab_platform.control_plane.artifact_access import ProtectedArtifactService
from lab_platform.control_plane.compatibility import (
    AgentUpgradeAssessment,
    AgentUpgradeStatus,
    VersionCompatibilityPolicy,
)
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.control_plane.gateway import (
    WS_AUTHENTICATION_FAILED,
    AgentConnectionHub,
    AgentGateway,
    AgentMessageRouter,
)
from lab_platform.control_plane.observability import OperationalMetrics
from lab_platform.control_plane.oidc_provider import HttpOidcProvider
from lab_platform.control_plane.operational_access import OperationalAccessService
from lab_platform.control_plane.operational_api import reconcile_operational_alerts
from lab_platform.control_plane.operational_events import (
    RetentionOperationalEventSink,
    StructuredOperationalEventSink,
)
from lab_platform.control_plane.operational_state import EventBackedOperationalState
from lab_platform.control_plane.reservation_queue import CentralReservationQueueService
from lab_platform.control_plane.reservations import (
    CoordinatedReconciliationHandler,
    HubReservationLeaseSynchronizer,
)
from lab_platform.control_plane.workflows import ControlPlaneWorkflowArtifactPort
from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    IssuedArtifactTransfer,
)
from lab_platform.control_plane_core.commands import RemoteCommandService
from lab_platform.control_plane_core.distributed_ci import DistributedCiSessionService
from lab_platform.control_plane_core.drain import AgentDrainService, DrainResult
from lab_platform.control_plane_core.enrollment import (
    AgentEnrollmentService,
    IssuedAgentCredential,
)
from lab_platform.control_plane_core.errors import AgentIncompatibleError
from lab_platform.control_plane_core.inventory import InventoryService
from lab_platform.control_plane_core.presence import AgentPresenceService
from lab_platform.control_plane_core.reconciliation import (
    ReconciliationResult,
    ReconciliationService,
)
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowCoordinator,
    DistributedWorkflowReservationLifecycle,
)
from lab_platform.core.artifact_storage import (
    ArtifactStorage,
    LocalArtifactStorage,
    S3CompatibleArtifactStorage,
)
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.auth import ApiTokenService
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    ConfigurationError,
    ResourceLimitExceededError,
)
from lab_platform.core.features import CommunityFeatureProvider, FeatureProvider
from lab_platform.core.identity import IdentityAuthenticationService
from lab_platform.core.identity_admin import IdentityAdministrationService
from lab_platform.core.oidc import OidcAuthenticationService, OidcProvider
from lab_platform.core.release import build_metadata
from lab_platform.core.retention import RetentionClass, RetentionPolicy, RetentionWorker
from lab_platform.models import (
    ActorContext,
    AgentRecord,
    AgentStatus,
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ArtifactTransferDirection,
    ArtifactTransferStatus,
    AuthenticationContext,
    AuthorisationResource,
    AuthorisationSnapshot,
    BenchVisibility,
    RemoteCommandStatus,
    ResourceType,
)
from lab_platform.persistence import (
    SQLiteApiTokenRepository,
    SQLiteCentralReservationLeaseRepository,
    SQLiteCiSessionRepository,
    SQLiteEventRepository,
    SQLiteGenericArtifactRepository,
    SQLiteQueueRepository,
    SQLiteTimedReservationRepository,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.agents import SQLiteAgentEnrollmentRepository
from lab_platform.persistence.database_management import (
    inspect_database_schema,
    require_current_schema,
)
from lab_platform.persistence.distributed import (
    SQLiteAgentTimelineRepository,
    SQLiteArtifactTransferRepository,
    SQLiteDistributedOperationRepository,
    SQLiteGlobalBenchRepository,
    SQLiteProtocolMessageJournalRepository,
    SQLiteRemoteArtifactRepository,
    SQLiteRemoteCommandRepository,
)
from lab_platform.persistence.distributed_adapters import (
    SQLiteAgentDrainRepository,
    SQLiteAgentPresenceAdapter,
    SQLiteArtifactTransferServiceRepository,
    SQLiteDistributedDirectory,
    SQLiteInventoryAdapter,
    SQLitePersistedReconciliationHandler,
    SQLiteProtocolJournalAdapter,
    SQLiteReconciliationBootRepository,
    SQLiteReconciliationInventoryAdapter,
    SQLiteReconciliationLeaseAdapter,
    SQLiteReconciliationReportStore,
    SQLiteRemoteCommandServiceRepository,
)
from lab_platform.persistence.identity import SQLiteIdentityRepository
from lab_platform.persistence.migrations import SCHEMA_VERSION
from lab_platform.persistence.postgresql import create_control_plane_database

_LOGGER = logging.getLogger("lab-platform.control-plane")

_IDENTITY_MAINTENANCE_INTERVAL = timedelta(hours=1)
_IDENTITY_MAINTENANCE_BATCH_SIZE = 1_000
_OPERATIONAL_ANALYTICS_INTERVAL = timedelta(minutes=1)


class ControlPlaneRuntime:
    """Composition root for the independent Phase 5 control-plane process."""

    def __init__(
        self,
        config: ControlPlaneConfig,
        *,
        oidc_provider: OidcProvider | None = None,
        feature_provider: FeatureProvider | None = None,
    ) -> None:
        self.config = config
        self.feature_provider = feature_provider or CommunityFeatureProvider()
        self.observability = OperationalMetrics()
        self.operational_events = StructuredOperationalEventSink()
        self._storage_unavailable_reported = False
        self.database = create_control_plane_database(config.database.url)
        self.audit_events = SQLiteEventRepository(self.database)
        self.operational_state = EventBackedOperationalState(self.audit_events)
        compatibility = config.compatibility.agents
        minimum_agent = compatibility.minimum_supported_version or (
            "0.8.0" if config.profile == "production" else "0.6.0-alpha"
        )
        self.compatibility_policy = VersionCompatibilityPolicy.from_strings(
            minimum_supported_agent=minimum_agent,
            minimum_recommended_agent=compatibility.minimum_recommended_version,
            target_agent=compatibility.target_version,
            maximum_supported_agent=compatibility.maximum_supported_version,
            release_channel=build_metadata().release_channel,
        )

        self.agent_repository = SQLiteAgentEnrollmentRepository(self.database)
        self.enrollment = AgentEnrollmentService(
            self.agent_repository,
            compatibility_validator=self.ensure_agent_compatible,
        )
        self.presence_repository = SQLiteAgentPresenceAdapter(self.database)
        self.inventory_repository = SQLiteInventoryAdapter(self.database)
        self.inventory = InventoryService(self.inventory_repository)
        self.presence = AgentPresenceService(
            self.presence_repository,
            heartbeat_timeout_seconds=config.agent_gateway.heartbeat_timeout_seconds,
            offline_timeout_seconds=config.agent_gateway.offline_timeout_seconds,
            offline_inventory=self.inventory,
        )

        self.hub = AgentConnectionHub(outgoing_queue_size=config.agent_gateway.outgoing_queue_size)
        self.command_repository = SQLiteRemoteCommandServiceRepository(self.database)
        self.command_records = SQLiteRemoteCommandRepository(self.database)
        self.operation_records = SQLiteDistributedOperationRepository(self.database)
        self.directory = SQLiteDistributedDirectory(
            self.presence_repository,
            self.inventory_repository,
        )
        self.lease_synchronizer = HubReservationLeaseSynchronizer(
            self.hub,
            confirmation_timeout_seconds=config.agent_gateway.handshake_timeout_seconds,
        )
        self.reservation_repository = SQLiteCentralReservationLeaseRepository(self.database)
        self.scheduled_reservation_repository = SQLiteTimedReservationRepository(self.database)
        self.reservations = CentralReservationLeaseService(
            self.reservation_repository,
            self.directory,
            self.lease_synchronizer,
            scheduled_repository=self.scheduled_reservation_repository,
            default_reservation_duration_seconds=min(
                3_600,
                (
                    config.resource_limits.maximum_reservation_duration_minutes * 60
                    if config.resource_limits.maximum_reservation_duration_minutes is not None
                    else 86_400
                ),
            ),
            maximum_reservation_duration_seconds=(
                config.resource_limits.maximum_reservation_duration_minutes * 60
                if config.resource_limits.maximum_reservation_duration_minutes is not None
                else 86_400
            ),
            maximum_clock_skew_seconds=(config.distributed.maximum_clock_skew_seconds),
            offline_reservation_grace_seconds=(
                config.distributed.offline_reservation_grace_seconds
            ),
        )
        self.reservation_queue = SQLiteQueueRepository(self.database)
        self.reservation_queue_service = CentralReservationQueueService(
            self.reservation_queue,
            self.reservations,
            self.inventory_repository,
            scheduled_protection_window_seconds=(
                config.distributed.scheduled_protection_window_seconds
            ),
        )

        maximum_artifact_size_mb = (
            config.resource_limits.maximum_artifact_size_mb or config.artifacts.max_upload_size_mb
        )
        self.maximum_artifact_size_bytes = maximum_artifact_size_mb * 1024 * 1024
        self.artifact_storage = _build_artifact_storage(config)
        self.artifact_transfer_repository = SQLiteArtifactTransferServiceRepository(self.database)
        self.artifact_store = FilesystemTransferStore(storage=self.artifact_storage)
        self.artifacts = DistributedArtifactService(
            self.artifact_transfer_repository,
            self.artifact_store,
            maximum_upload_size_bytes=self.maximum_artifact_size_bytes,
            token_ttl_seconds=config.artifacts.transfer_token_ttl_seconds,
        )
        self._artifact_upload_capabilities: dict[UUID, IssuedArtifactTransfer] = {}
        self._artifact_upload_requested_at: dict[UUID, datetime] = {}
        self._artifact_upload_lock = asyncio.Lock()
        self.remote_artifacts = SQLiteRemoteArtifactRepository(self.database)
        self.platform_artifact_repository = SQLiteGenericArtifactRepository(
            self.database,
            retention_storage=self.artifact_storage,
        )
        retention = config.retention.artifacts
        retention_policy = (
            RetentionPolicy(
                default_days=retention.default_days,
                failed_workflow_days=retention.failed_workflow_days,
                firmware_days=retention.firmware_days,
                class_days={
                    RetentionClass.SERIAL_LOG: retention.serial_log_days,
                    RetentionClass.JUNIT_REPORT: retention.junit_report_days,
                    RetentionClass.WORKFLOW_LOG: retention.workflow_log_days,
                    RetentionClass.DIAGNOSTIC_BUNDLE: retention.diagnostic_bundle_days,
                },
            )
            if retention.enabled
            else None
        )
        self.platform_artifacts = ArtifactService(
            self.platform_artifact_repository,
            maximum_size_bytes=self.maximum_artifact_size_bytes,
            events=self.audit_events,
            retention_policy=retention_policy,
            storage=self.artifact_storage,
        )
        self.artifact_retention = RetentionWorker(
            self.platform_artifact_repository,
            self.artifact_storage,
            events=RetentionOperationalEventSink(self.operational_events),
        )
        self.workflow_repository = SQLiteWorkflowRepository(
            self.database,
            initialize_schema=False,
        )
        self.workflow_artifacts = ControlPlaneWorkflowArtifactPort(
            self.platform_artifacts,
            self.artifacts,
            self.artifact_store,
            public_url=config.control_plane.public_url,
            maximum_size_bytes=self.maximum_artifact_size_bytes,
        )
        self.commands = RemoteCommandService(
            self.command_repository,
            self.directory,
            self.hub,
            queue_commands_for_offline_agents=(
                config.distributed.queue_commands_for_offline_agents
            ),
            reconciliation_timeout_seconds=(
                config.distributed.operation_reconciliation_timeout_seconds
            ),
            payload_hydrator=self.workflow_artifacts,
        )
        self.commands.set_workflow_definition_catalog(self.workflow_repository)
        self.workflows = DistributedWorkflowCoordinator(
            self.inventory,
            self.presence,
            self.reservations,
            self.workflow_artifacts,
            self.commands,
            definition_catalog=self.workflow_repository,
        )
        self.workflow_reservations = DistributedWorkflowReservationLifecycle(
            self.command_repository,
            self.reservations,
        )
        self.ci_repository = SQLiteCiSessionRepository(self.database)
        self.ci = DistributedCiSessionService(
            self.ci_repository,
            self.workflow_repository,
            self.workflows,
            self.command_repository,
            self.reservations,
            self.commands,
            self.remote_artifacts,
            self,
            artifact_finalization_timeout_seconds=(config.artifacts.finalization_timeout_seconds),
        )

        self.reconciliation_reports = SQLiteReconciliationReportStore(self.database)
        self.reconciliation_service = ReconciliationService(
            self.reconciliation_reports,
            SQLiteReconciliationBootRepository(self.database),
            self.command_repository,
            SQLiteReconciliationLeaseAdapter(self.database),
            SQLiteReconciliationInventoryAdapter(
                self.presence_repository,
                self.inventory_repository,
            ),
        )
        self.reconciliation = CoordinatedReconciliationHandler(
            SQLitePersistedReconciliationHandler(
                self.database,
                self.reconciliation_service,
            ),
            self.reservations,
            self.lease_synchronizer,
            on_reconciled=self._replay_after_reconciliation,
        )
        self.drain = AgentDrainService(SQLiteAgentDrainRepository(self.database))
        self.timeline = SQLiteAgentTimelineRepository(self.database)
        self.protocol_journal = SQLiteProtocolJournalAdapter(self.database)
        self.router = AgentMessageRouter(
            presence=self.presence,
            inventory=self.inventory,
            commands=self.commands,
            artifacts=self.artifacts,
            artifact_uploads=self,
            lease_receipts=self.lease_synchronizer,
            reservation_disconnects=self.reservations,
            reconciliation=self.reconciliation,
            timeline=self.timeline,
        )
        self.gateway = AgentGateway(
            enrollment=self.enrollment,
            presence=self.presence,
            hub=self.hub,
            router=self.router,
            gateway_settings=config.agent_gateway,
            distributed_settings=config.distributed,
            protocol_journal=self.protocol_journal,
            timeline=self.timeline,
            compatibility_validator=self.ensure_agent_compatible,
        )

        self.benches = SQLiteGlobalBenchRepository(self.database)
        self.artifact_transfers = SQLiteArtifactTransferRepository(self.database)
        self.raw_protocol_messages = SQLiteProtocolMessageJournalRepository(self.database)
        self.api_tokens = SQLiteApiTokenRepository(self.database)
        self.token_service = ApiTokenService(self.api_tokens, self.audit_events)
        self.identity_repository = SQLiteIdentityRepository(self.database)
        self.identity = IdentityAuthenticationService(
            self.identity_repository,
            minimum_password_length=config.identity.local_auth.minimum_password_length,
            access_token_minutes=config.identity.sessions.access_token_minutes,
            session_hours=config.identity.sessions.session_hours,
            maximum_session_days=config.identity.sessions.maximum_session_days,
            login_rate_limit_attempts=config.security.login_rate_limit.attempts,
            login_rate_limit_window_minutes=config.security.login_rate_limit.window_minutes,
            audit_enabled=config.audit.enabled,
        )
        if oidc_provider is None and config.identity.oidc.enabled:
            secret_name = config.identity.oidc.client_secret_env
            client_secret = os.environ.get(secret_name) if secret_name is not None else None
            if (
                config.identity.oidc.issuer_url is not None
                and config.identity.oidc.client_id is not None
                and client_secret
            ):
                oidc_provider = HttpOidcProvider(
                    issuer_url=config.identity.oidc.issuer_url,
                    client_id=config.identity.oidc.client_id,
                    client_secret=client_secret,
                )
        self.oidc = OidcAuthenticationService(
            self.identity_repository,
            self.identity,
            enabled=config.identity.oidc.enabled,
            issuer_url=config.identity.oidc.issuer_url,
            client_id=config.identity.oidc.client_id,
            scopes=config.identity.oidc.scopes,
            username_claim=config.identity.oidc.username_claim,
            provider=oidc_provider,
            transaction_ttl_seconds=config.identity.oidc.transaction_ttl_seconds,
            clock_skew_seconds=config.identity.oidc.clock_skew_seconds,
        )
        self.authorisation = AuthorisationService(
            self.identity_repository,
            audit_repository=self.identity_repository,
            policy_repository=self.identity_repository,
            default_bench_visibility=BenchVisibility(
                config.authorisation.default_bench_visibility.upper()
            ),
            audit_enabled=config.audit.enabled,
        )
        self.artifact_access = ProtectedArtifactService(
            self.platform_artifacts,
            self.remote_artifacts,
            self.operation_records,
            self.command_repository,
            self.workflow_repository,
            self.inventory_repository,
            self.presence,
            self.ci,
            self.ci_repository,
            self.authorisation,
            self.artifacts,
            self.artifact_store,
            maximum_transfer_size_bytes=self.maximum_artifact_size_bytes,
            hide_unauthorised_resources=(config.authorisation.hide_unauthorised_resources),
        )
        self.operational_access = OperationalAccessService(
            self.presence,
            self.inventory,
            self.workflow_repository,
            self.operation_records,
            self.ci,
            self.authorisation,
            hide_unauthorised_resources=(config.authorisation.hide_unauthorised_resources),
        )
        self.workflow_artifacts.set_protected_artifact_service(self.artifact_access)
        self.workflows.set_authorisation_service(self.authorisation)
        self.ci.set_authorisation_service(self.authorisation)
        self.ci.set_authorisation_snapshot_repository(self.identity_repository)
        self.commands.set_authorisation_service(self.authorisation)
        self.commands.set_authorisation_snapshot_repository(self.identity_repository)
        self.commands.set_ci_cancellation_binding_repository(self.ci_repository)
        self.reservations.set_authorisation_service(self.authorisation)
        self.drain.set_authorisation_service(self.authorisation)
        self.enrollment.set_authorisation_service(self.authorisation)
        self.identity_administration = IdentityAdministrationService(
            self.identity_repository,
            self.identity,
            self.authorisation,
            bench_directory=self.benches,
            workflow_catalog=self.workflow_repository,
            default_bench_visibility=BenchVisibility(
                config.authorisation.default_bench_visibility.upper()
            ),
            audit_enabled=config.audit.enabled,
        )
        self._monitor_task: asyncio.Task[None] | None = None
        self._next_identity_maintenance_at: datetime | None = None
        self._next_artifact_retention_at: datetime | None = None
        self._next_operational_analytics_at: datetime | None = None
        self._active_sse_streams = 0
        self._sse_stream_lock = asyncio.Lock()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def assess_agent_compatibility(
        self,
        agent_version: str,
        protocol_version: str,
    ) -> AgentUpgradeAssessment:
        return self.compatibility_policy.evaluate_agent(agent_version, protocol_version)

    def ensure_agent_compatible(self, agent_version: str, protocol_version: str) -> None:
        assessment = self.assess_agent_compatibility(agent_version, protocol_version)
        payload = {
            "agent_version": assessment.agent_version,
            "protocol_version": assessment.protocol_version,
            "upgrade_status": assessment.status.value,
            "minimum_supported_version": assessment.minimum_supported_version,
            "target_version": assessment.target_version,
            "release_channel": assessment.release_channel,
        }
        if assessment.status in {
            AgentUpgradeStatus.UPGRADE_AVAILABLE,
            AgentUpgradeStatus.UPGRADE_RECOMMENDED,
            AgentUpgradeStatus.UPGRADE_REQUIRED,
        }:
            self.operational_events.emit("AGENT_UPGRADE_AVAILABLE", payload)
        if assessment.work_allowed:
            return
        self.operational_events.emit(
            "VERSION_INCOMPATIBLE",
            {**payload, "reason": assessment.reason},
        )
        raise AgentIncompatibleError(
            assessment.reason,
            agent_version=assessment.agent_version,
            protocol_version=assessment.protocol_version,
            upgrade_status=assessment.status.value,
            minimum_supported_version=assessment.minimum_supported_version,
            target_version=assessment.target_version,
        )

    async def start(self) -> None:
        if self._started:
            return
        if self.config.development.auto_login_user is not None:
            _LOGGER.warning(
                "DEVELOPMENT AUTO-LOGIN IS ENABLED for user %s on loopback only; "
                "do not use this configuration in a shared or production deployment",
                self.config.development.auto_login_user,
            )
        try:
            # Production startup is deliberately non-migrating: an operator must run
            # ``lab-platform-control-plane db migrate`` before replacing the process.
            # The initialize call below can therefore only reopen an already-current
            # schema; it cannot bridge an old or empty production database.
            if self.config.profile == "production":
                require_current_schema(inspect_database_schema(self.config.database.url))
            self.database.initialize()
            if self.config.identity.enabled:
                await self.identity_repository.ensure_default_organisation(
                    slug=self.config.identity.default_organisation_slug,
                    name=self.config.identity.default_organisation_name,
                )
            if isinstance(self.artifact_storage, LocalArtifactStorage):
                self.config.artifacts.directory.mkdir(parents=True, exist_ok=True)
            await self.ci.recover_incomplete()
            now = datetime.now(UTC)
            self._next_identity_maintenance_at = now
            self._next_artifact_retention_at = now
            self._next_operational_analytics_at = now
            self._started = True
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="control-plane-monitor",
            )
        except Exception:
            if self._monitor_task is not None:
                self._monitor_task.cancel()
                await asyncio.gather(self._monitor_task, return_exceptions=True)
                self._monitor_task = None
            self._next_identity_maintenance_at = None
            self._next_artifact_retention_at = None
            self._next_operational_analytics_at = None
            self.database.close()
            self._started = False
            raise

    async def stop(self) -> None:
        # Readiness is withdrawn before workers and Agent transports are drained.
        self._started = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        await self.hub.close_all()
        self._next_identity_maintenance_at = None
        self._next_artifact_retention_at = None
        self._next_operational_analytics_at = None
        self.database.close()

    async def readiness(self) -> dict[str, object]:
        """Return dependency-aware readiness without exposing credentials or paths."""

        checks: dict[str, object] = {
            "runtime": {"ready": self._started},
        }
        ready = self._started
        try:
            with self.database.transaction() as connection:
                connection.execute("SELECT 1").fetchone()
                row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                schema_version = int(row[0]) if row is not None and row[0] is not None else 0
            schema_ready = schema_version == SCHEMA_VERSION
            checks["database"] = {
                "ready": schema_ready,
                "schema_version": schema_version,
                "target_schema_version": SCHEMA_VERSION,
            }
            ready = ready and schema_ready
        except Exception as exc:
            checks["database"] = {
                "ready": False,
                "error": type(exc).__name__,
            }
            ready = False
        try:
            # A missing reserved probe object is healthy; the metadata request itself
            # verifies that the configured backend can be reached.
            await self.artifact_storage.exists("health/readiness-probe")
            checks["artifact_storage"] = {"ready": True}
            self._storage_unavailable_reported = False
        except Exception as exc:
            checks["artifact_storage"] = {
                "ready": False,
                "error": type(exc).__name__,
            }
            ready = False
            if not self._storage_unavailable_reported:
                self.operational_events.emit(
                    "STORAGE_UNAVAILABLE",
                    {
                        "backend_type": self.config.artifacts.storage_backend,
                        "error_type": type(exc).__name__,
                    },
                )
                self._storage_unavailable_reported = True
        try:
            gateway_metrics = await self.hub.metrics()
            checks["agent_gateway"] = {
                "ready": True,
                "connections": gateway_metrics.get("connected_agents", 0),
            }
        except Exception as exc:
            checks["agent_gateway"] = {
                "ready": False,
                "error": type(exc).__name__,
            }
            ready = False
        return {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checks": checks,
        }

    def require_workflow_capacity(self) -> None:
        limit = self.config.resource_limits.maximum_concurrent_workflows
        if limit is None:
            return
        self._require_database_capacity(
            name="concurrent workflows",
            limit=limit,
            query=(
                "SELECT COUNT(*) FROM distributed_operations "
                "WHERE operation_type = 'RUN_WORKFLOW' AND status IN "
                "('CREATED', 'DISPATCHED', 'ACCEPTED', 'RUNNING', 'UNKNOWN', 'RECONCILING')"
            ),
        )

    def require_ci_capacity(self) -> None:
        limit = self.config.resource_limits.maximum_active_ci_sessions
        if limit is None:
            return
        self._require_database_capacity(
            name="active CI sessions",
            limit=limit,
            query=(
                "SELECT COUNT(*) FROM ci_sessions WHERE status IN "
                "('created', 'waiting_for_bench', 'reserved', 'running', "
                "'cancel_requested', 'cleanup_pending')"
            ),
        )

    def _require_database_capacity(self, *, name: str, limit: int, query: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(query).fetchone()
        active = int(row[0]) if row is not None else 0
        if active >= limit:
            raise ResourceLimitExceededError(
                f"The configured limit for {name} has been reached.",
                resource=name,
                limit=limit,
                active=active,
            )

    async def acquire_sse_stream(self) -> bool:
        limit = self.config.resource_limits.maximum_sse_streams
        async with self._sse_stream_lock:
            if limit is not None and self._active_sse_streams >= limit:
                return False
            self._active_sse_streams += 1
            return True

    async def release_sse_stream(self) -> None:
        async with self._sse_stream_lock:
            self._active_sse_streams = max(0, self._active_sse_streams - 1)

    async def refresh_inventory(
        self,
        agent_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> UUID:
        _, authentication_context = await self._require_agent_authorisation(
            agent_id,
            "agents:manage",
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            persist_snapshot=True,
        )
        request_id = uuid4()
        actor_context = _control_actor_context(authentication_context)
        payload = InventoryRefreshRequestPayload(
            request_id=request_id,
            actor_context=actor_context,
            authorisation_snapshot_id=(
                authentication_context.authorisation_snapshot_id
                if authentication_context is not None
                else None
            ),
        )
        # Persist the authorised intent before the in-memory socket enqueue.  A crash or
        # backpressure failure can therefore be distinguished from an action that was never
        # authorised, while the Agent protocol journal still records an eventual wire send.
        await self.record_timeline(
            agent_id,
            "INVENTORY_REFRESH_REQUESTED",
            "A fresh Agent inventory snapshot was requested.",
            correlation_id=request_id,
            metadata=_control_intent_metadata(authentication_context),
            deduplication_key=f"control-intent:inventory-refresh:{request_id}",
        )
        await self.hub.send(
            agent_id,
            MessageType.INVENTORY_REFRESH_REQUEST,
            payload,
            correlation_id=request_id,
        )
        return request_id

    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> object:
        async with self._artifact_upload_lock:
            artifact = await self.artifact_transfer_repository.get_remote_artifact(artifact_id)
            if artifact is None or artifact.agent_id != agent_id:
                raise ValueError("Remote artifact does not belong to the requesting Agent")
            if artifact.uploaded_at is not None:
                return artifact
            now = datetime.now(UTC)
            issued = self._artifact_upload_capabilities.get(artifact_id)
            last_requested_at = self._artifact_upload_requested_at.get(artifact_id)
            if issued is None or issued.transfer.expires_at <= now:
                # Plaintext transfer capabilities are intentionally memory-only.  After a
                # control-plane restart, supersede unrecoverable hashed capabilities before
                # issuing a fresh one. Within one process, retries reuse the same capability
                # and are briefly throttled so CI maintenance cannot flood the Agent queue.
                for existing in await self.artifact_transfers.list(
                    agent_id=agent_id,
                    limit=10_000,
                ):
                    if (
                        existing.artifact_id == artifact_id
                        and existing.direction is ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE
                        and existing.status
                        in {
                            ArtifactTransferStatus.PENDING,
                            ArtifactTransferStatus.IN_PROGRESS,
                            ArtifactTransferStatus.FAILED,
                        }
                    ):
                        await self.artifact_transfers.update(
                            existing.model_copy(
                                update={
                                    "status": ArtifactTransferStatus.EXPIRED,
                                    "error_code": "TRANSFER_SUPERSEDED",
                                }
                            ),
                            expected_status=existing.status,
                        )
                issued = await self.artifacts.issue_upload(artifact_id)
                self._artifact_upload_capabilities[artifact_id] = issued
                last_requested_at = None
            elif last_requested_at is not None and (now - last_requested_at).total_seconds() < 5:
                return issued
            transfer = issued.transfer
            base = self.config.control_plane.public_url.rstrip("/")
            await self.hub.send(
                agent_id,
                MessageType.ARTIFACT_UPLOAD_REQUEST,
                ArtifactUploadRequestPayload(
                    transfer_id=transfer.id,
                    artifact_id=artifact_id,
                    local_artifact_id=artifact.local_artifact_id,
                    upload_url=f"{base}/api/v1/artifact-transfers/{transfer.id}/content",
                    transfer_token=issued.plaintext_token,
                    expires_at=transfer.expires_at,
                    maximum_size_bytes=self.maximum_artifact_size_bytes,
                    expected_sha256=transfer.expected_sha256,
                ),
                correlation_id=artifact_id,
            )
            self._artifact_upload_requested_at[artifact_id] = now
            return issued

    async def request_reconciliation(
        self,
        agent_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> UUID:
        _, authentication_context = await self._require_agent_authorisation(
            agent_id,
            "agents:manage",
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            persist_snapshot=True,
        )
        connection = await self.presence.active_connection(agent_id)
        if connection is None:
            raise RuntimeError("Agent is not connected")
        request_id = uuid4()
        actor_context = _control_actor_context(authentication_context)
        payload = ReconciliationRequestPayload(
            request_id=request_id,
            expected_boot_id=connection.connection.boot_id,
            last_control_plane_sequence=connection.connection.last_sequence_number,
            actor_context=actor_context,
            authorisation_snapshot_id=(
                authentication_context.authorisation_snapshot_id
                if authentication_context is not None
                else None
            ),
        )
        await self.record_timeline(
            agent_id,
            "RECONCILIATION_REQUESTED",
            "A fresh Agent reconciliation report was requested.",
            correlation_id=request_id,
            metadata=_control_intent_metadata(
                authentication_context,
                expected_boot_id=str(connection.connection.boot_id),
                last_control_plane_sequence=connection.connection.last_sequence_number,
            ),
            deduplication_key=f"control-intent:reconciliation:{request_id}",
        )
        await self.hub.send(
            agent_id,
            MessageType.RECONCILIATION_REQUEST,
            payload,
            correlation_id=request_id,
        )
        return request_id

    async def _replay_after_reconciliation(
        self,
        result: ReconciliationResult,
    ) -> None:
        commands = await self.command_repository.list_commands(
            agent_id=result.agent_id,
            statuses={RemoteCommandStatus.UNKNOWN, RemoteCommandStatus.QUEUED},
            limit=10_000,
        )
        for command in commands:
            lease = None
            if command.reservation_id is not None:
                with suppress(Exception):
                    coordinated = await self.reservations.get(command.reservation_id)
                    if (
                        coordinated.state is ReservationLeaseState.ACTIVE
                        and coordinated.lease.lease_version == command.lease_version
                    ):
                        lease = coordinated.lease
                if lease is None:
                    continue
            try:
                await self.commands.dispatch(command.id, reservation_lease=lease)
            except Exception as exc:
                await self.record_timeline(
                    result.agent_id,
                    "REMOTE_COMMAND_REPLAY_DEFERRED",
                    "A reconciled command could not yet be replayed safely.",
                    severity=AgentTimelineSeverity.WARNING,
                    correlation_id=command.id,
                    metadata={"error_type": type(exc).__name__},
                )

    async def drain_agent(
        self,
        agent_id: UUID,
        *,
        cancel_queued_work: bool = False,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> DrainResult:
        _, authentication_context = await self._require_agent_authorisation(
            agent_id,
            "agents:drain",
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            persist_snapshot=True,
        )
        result = await self.drain.drain(
            agent_id,
            cancel_queued_work=cancel_queued_work,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        correlation_id = uuid4()
        actor_context = _control_actor_context(authentication_context)
        await self.record_timeline(
            agent_id,
            "AGENT_DRAIN_REQUESTED",
            "Agent entered drain mode.",
            correlation_id=correlation_id,
            metadata=_control_intent_metadata(
                authentication_context,
                cancelled_queued_work=result.cancelled_queued_work,
            ),
            deduplication_key=f"control-intent:drain:{correlation_id}",
        )
        if await self.hub.is_connected(agent_id):
            await self.hub.send(
                agent_id,
                MessageType.DRAIN_AGENT,
                DrainAgentPayload(
                    drain=True,
                    actor_context=actor_context,
                    authorisation_snapshot_id=(
                        authentication_context.authorisation_snapshot_id
                        if authentication_context is not None
                        else None
                    ),
                ),
                correlation_id=correlation_id,
            )
        return result

    async def undrain_agent(
        self,
        agent_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> AgentRecord:
        _, authentication_context = await self._require_agent_authorisation(
            agent_id,
            "agents:drain",
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            persist_snapshot=True,
        )
        agent = await self.drain.undrain(
            agent_id,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        correlation_id = uuid4()
        actor_context = _control_actor_context(authentication_context)
        await self.record_timeline(
            agent_id,
            "AGENT_UNDRAINED",
            "Agent left drain mode.",
            correlation_id=correlation_id,
            metadata=_control_intent_metadata(authentication_context),
            deduplication_key=f"control-intent:undrain:{correlation_id}",
        )
        await self.hub.send(
            agent_id,
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(
                drain=False,
                actor_context=actor_context,
                authorisation_snapshot_id=(
                    authentication_context.authorisation_snapshot_id
                    if authentication_context is not None
                    else None
                ),
            ),
            correlation_id=correlation_id,
        )
        return agent

    async def revoke_agent(
        self,
        agent_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> AgentRecord:
        trusted_agent, _ = await self._require_agent_authorisation(
            agent_id,
            "agents:manage",
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        agent = await self.enrollment.revoke_agent(
            agent_id,
            organisation_id=trusted_agent.organisation_id,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        await self.hub.close_agent(
            agent_id,
            code=WS_AUTHENTICATION_FAILED,
            reason="Agent credential revoked",
        )
        await self.record_timeline(
            agent_id,
            "AGENT_REVOKED",
            "Agent identity and credentials were revoked.",
            severity=AgentTimelineSeverity.WARNING,
        )
        return agent

    async def _require_agent_authorisation(
        self,
        agent_id: UUID,
        permission: str,
        *,
        authentication_context: AuthenticationContext | None,
        allow_legacy_authorisation: bool,
        allow_internal_authorisation: bool,
        persist_snapshot: bool = False,
    ) -> tuple[AgentRecord, AuthenticationContext | None]:
        if allow_legacy_authorisation and allow_internal_authorisation:
            raise ValueError(
                "Legacy and internal Agent authorisation escapes are mutually exclusive"
            )
        if authentication_context is not None and (
            allow_legacy_authorisation or allow_internal_authorisation
        ):
            raise ValueError("Agent authorisation escapes cannot carry an authenticated principal")
        agent = (
            await self.presence.get_agent(agent_id)
            if authentication_context is None
            else await self.presence.get_agent(
                agent_id,
                organisation_id=authentication_context.principal.organisation_id,
            )
        )
        if (
            authentication_context is not None
            and authentication_context.principal.organisation_id != agent.organisation_id
        ):
            raise ValueError("Agent organisation does not match the authenticated principal")
        if allow_legacy_authorisation or allow_internal_authorisation:
            return agent, None
        if authentication_context is None:
            raise AuthenticationRequiredError(
                "An authenticated principal is required to manage Agents."
            )
        principal = authentication_context.principal
        resource = AuthorisationResource(
            type=ResourceType.AGENT,
            id=str(agent.id),
            organisation_id=agent.organisation_id,
        )
        decision = await self.authorisation.evaluate(
            principal,
            permission,
            resource,
            credential_restrictions=authentication_context.permission_restrictions,
        )
        if not decision.allowed:
            await self.authorisation.require(
                principal,
                permission,
                resource,
                credential_restrictions=authentication_context.permission_restrictions,
            )
            raise AssertionError("AuthorisationService.require must reject a denied decision")
        if not persist_snapshot:
            return agent, authentication_context

        snapshot_id = authentication_context.authorisation_snapshot_id
        if snapshot_id is None:
            created_snapshot = await self.identity_repository.create_authorisation_snapshot(
                principal.organisation_id,
                AuthorisationSnapshot(
                    principal_id=principal.id,
                    permission=permission,
                    resource_type=ResourceType.AGENT,
                    resource_id=str(agent.id),
                    granted_by_assignments=sorted(decision.granting_assignment_ids, key=str),
                ),
            )
            authentication_context = authentication_context.model_copy(
                update={"authorisation_snapshot_id": created_snapshot.id}
            )
        else:
            existing_snapshot = await self.identity_repository.get_authorisation_snapshot(
                principal.organisation_id,
                snapshot_id,
            )
            if (
                existing_snapshot is None
                or existing_snapshot.principal_id != principal.id
                or existing_snapshot.permission != permission
                or existing_snapshot.resource_type is not ResourceType.AGENT
                or existing_snapshot.resource_id != str(agent.id)
            ):
                raise ValueError(
                    "Agent control authorisation snapshot evidence is unresolved or mismatched"
                )
        return agent, authentication_context

    async def rotate_agent_credential(
        self,
        agent_id: UUID,
        *,
        current_secret: str,
    ) -> IssuedAgentCredential:
        issued = await self.enrollment.rotate_credential(
            agent_id,
            current_secret=current_secret,
        )
        # Rotation invalidates the credential that authenticated the current
        # channel. Fence it immediately so every subsequent message arrives on a
        # connection authenticated with the replacement credential.
        await self.hub.close_agent(
            agent_id,
            code=WS_AUTHENTICATION_FAILED,
            reason="Agent credential rotated",
        )
        await self.record_timeline(
            agent_id,
            "AGENT_CREDENTIAL_ROTATED",
            "Agent credential rotated and the prior connection was fenced.",
            severity=AgentTimelineSeverity.WARNING,
            metadata={"credential_version": issued.credential_version},
        )
        return issued

    async def record_timeline(
        self,
        agent_id: UUID,
        event_type: str,
        message: str,
        *,
        severity: AgentTimelineSeverity = AgentTimelineSeverity.INFO,
        correlation_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
        deduplication_key: str | None = None,
    ) -> AgentTimelineRecord:
        return await self.timeline.append(
            AgentTimelineRecord(
                agent_id=agent_id,
                event_type=event_type,
                severity=severity,
                message=message,
                correlation_id=correlation_id,
                metadata=metadata or {},
                deduplication_key=deduplication_key,
            )
        )

    async def metrics(
        self,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> dict[str, int | float]:
        if allow_legacy_authorisation and allow_internal_authorisation:
            raise ValueError(
                "Legacy and internal metrics authorisation escapes are mutually exclusive"
            )
        if authentication_context is not None and (
            allow_legacy_authorisation or allow_internal_authorisation
        ):
            raise ValueError(
                "Metrics authorisation escapes cannot carry an authenticated principal"
            )
        if authentication_context is not None:
            principal = authentication_context.principal
            if organisation_id is not None and organisation_id != principal.organisation_id:
                raise ValueError("Metrics organisation does not match the authenticated principal")
            await self.authorisation.require(
                principal,
                "agents:read",
                AuthorisationResource(
                    type=ResourceType.ORGANISATION,
                    id=str(principal.organisation_id),
                    organisation_id=principal.organisation_id,
                ),
                credential_restrictions=authentication_context.permission_restrictions,
            )
            organisation_id = principal.organisation_id
        elif not (allow_legacy_authorisation or allow_internal_authorisation):
            raise AuthenticationRequiredError(
                "An authenticated principal is required to read control-plane metrics."
            )
        with self.database.transaction() as connection:

            def scalar(query: str, parameters: tuple[object, ...] = ()) -> int | float:
                value = connection.execute(query, parameters).fetchone()[0]
                if not isinstance(value, (int, float)):
                    raise RuntimeError("Metric query did not return a number")
                return value

            scope_values: tuple[object, ...] = (
                (str(organisation_id),) if organisation_id is not None else ()
            )
            scope_where = " WHERE organisation_id = ?" if organisation_id is not None else ""
            scope_and = " AND organisation_id = ?" if organisation_id is not None else ""
            metrics: dict[str, int | float] = {
                "database_healthy": 1,
                "database_schema_version": SCHEMA_VERSION,
                "agents_online": scalar(
                    "SELECT COUNT(*) FROM agents WHERE status = 'ONLINE'" + scope_and,
                    scope_values,
                ),
                "agents_offline": scalar(
                    "SELECT COUNT(*) FROM agents WHERE status = 'OFFLINE'" + scope_and,
                    scope_values,
                ),
                "agent_reconnects_total": scalar(
                    "SELECT MAX(0, COUNT(*) - COUNT(DISTINCT connection.agent_id)) "
                    "FROM agent_connections AS connection "
                    "JOIN agents AS agent ON agent.id = connection.agent_id"
                    + (" WHERE agent.organisation_id = ?" if organisation_id is not None else ""),
                    scope_values,
                ),
                "agent_heartbeat_lag_seconds": scalar(
                    "SELECT COALESCE(MAX((julianday('now') - julianday(last_heartbeat_at)) "
                    "* 86400.0), 0) FROM agent_connections AS connection "
                    "JOIN agents AS agent ON agent.id = connection.agent_id "
                    "WHERE connection.disconnected_at IS NULL"
                    + (" AND agent.organisation_id = ?" if organisation_id is not None else ""),
                    scope_values,
                ),
                "remote_commands_pending": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status IN "
                    "('CREATED', 'QUEUED', 'DISPATCHED', 'ACCEPTED', 'UNKNOWN')" + scope_and,
                    scope_values,
                ),
                "remote_commands_running": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status = 'RUNNING'" + scope_and,
                    scope_values,
                ),
                "remote_commands_failed": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status = 'FAILED'" + scope_and,
                    scope_values,
                ),
                "workflows_active": scalar(
                    "SELECT COUNT(*) FROM distributed_operations "
                    "WHERE operation_type = 'RUN_WORKFLOW' AND status IN "
                    "('CREATED', 'DISPATCHED', 'ACCEPTED', 'RUNNING', 'UNKNOWN', "
                    "'RECONCILING')" + scope_and,
                    scope_values,
                ),
                "workflows_failed": scalar(
                    "SELECT COUNT(*) FROM distributed_operations "
                    "WHERE operation_type = 'RUN_WORKFLOW' AND status = 'FAILED'" + scope_and,
                    scope_values,
                ),
                "reservations_active": scalar(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'active'" + scope_and,
                    scope_values,
                ),
                "reservation_queue_depth": scalar(
                    "SELECT COUNT(*) FROM reservation_queue WHERE status = 'waiting'" + scope_and,
                    scope_values,
                ),
                "artifact_usage_bytes": scalar(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts "
                    "WHERE retention_state = 'active'" + scope_and,
                    scope_values,
                ),
                "artifacts_active": scalar(
                    "SELECT COUNT(*) FROM artifacts WHERE retention_state = 'active'" + scope_and,
                    scope_values,
                ),
                "operation_reconciliations_total": scalar(
                    "SELECT COUNT(*) FROM reconciliation_report_claims AS claim "
                    "JOIN agents AS agent ON agent.id = claim.agent_id "
                    "WHERE claim.status = 'COMPLETE'"
                    + (" AND agent.organisation_id = ?" if organisation_id is not None else ""),
                    scope_values,
                ),
                "artifact_transfer_bytes": scalar(
                    "SELECT COALESCE(SUM(attempt.bytes_transferred), 0) "
                    "FROM artifact_transfer_attempts AS attempt "
                    "JOIN artifact_transfers AS transfer ON transfer.id = attempt.transfer_id "
                    "JOIN agents AS agent ON agent.id = transfer.agent_id"
                    + (" WHERE agent.organisation_id = ?" if organisation_id is not None else ""),
                    scope_values,
                ),
                "artifact_transfer_failures": scalar(
                    "SELECT COUNT(*) FROM artifact_transfers AS transfer "
                    "JOIN agents AS agent ON agent.id = transfer.agent_id "
                    "WHERE transfer.status = 'FAILED'"
                    + (" AND agent.organisation_id = ?" if organisation_id is not None else ""),
                    scope_values,
                ),
                "bench_inventory_total": scalar(
                    "SELECT COUNT(*) FROM global_benches" + scope_where,
                    scope_values,
                ),
                "bench_inventory_offline": scalar(
                    "SELECT COUNT(*) FROM global_benches WHERE status = 'OFFLINE'" + scope_and,
                    scope_values,
                ),
                "ci_sessions_waiting": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status IN "
                    "('created', 'waiting_for_bench')" + scope_and,
                    scope_values,
                ),
                "ci_sessions_running": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status IN "
                    "('reserved', 'running', 'cancel_requested')" + scope_and,
                    scope_values,
                ),
                "ci_sessions_cleanup_pending": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status = 'cleanup_pending'" + scope_and,
                    scope_values,
                ),
            }
            agent_ids = (
                None
                if organisation_id is None
                else {
                    UUID(str(row[0]))
                    for row in connection.execute(
                        "SELECT id FROM agents WHERE organisation_id = ?",
                        scope_values,
                    ).fetchall()
                }
            )
        hub_metrics = await self.hub.metrics(agent_ids=agent_ids)
        metrics.update({f"gateway_{key}": value for key, value in hub_metrics.items()})
        return metrics

    async def monitor_once(self) -> None:
        now = datetime.now(UTC)
        transitions = await self.presence.check_timeouts(
            observed_at=now,
            observed_monotonic=asyncio.get_running_loop().time(),
        )
        for transition in transitions:
            if transition.agent.disconnected_at is not None:
                await self.commands.mark_unknown(transition.agent.id, observed_at=now)
                await self.reservations.mark_agent_disconnected(
                    transition.agent.id,
                    disconnect_id=transition.connection.id,
                )
        await self.commands.expire_due()
        await self.reservations.expire_due()
        await self.reservations.process_due_scheduled()
        await self.reservation_queue_service.promote_waiting()
        await self.reconciliation_service.timeout_unreconciled_operations()
        await self.workflow_reservations.release_terminal()
        await self.ci.process_maintenance()
        for agent in await self.presence.list_agents(status=AgentStatus.DRAINING):
            with suppress(Exception):
                await self.drain.refresh(
                    agent.id,
                    allow_internal_authorisation=True,
                )
        await self._process_artifact_retention(now)
        await self._process_identity_maintenance(now)
        await self._process_operational_analytics(now)

    async def _process_operational_analytics(self, now: datetime) -> None:
        due_at = self._next_operational_analytics_at
        if due_at is None or now < due_at:
            return
        self._next_operational_analytics_at = now + _OPERATIONAL_ANALYTICS_INTERVAL
        await reconcile_operational_alerts(self, generated_at=now)

    async def _process_artifact_retention(self, now: datetime) -> None:
        retention = self.config.retention.artifacts
        due_at = self._next_artifact_retention_at
        if not retention.enabled or due_at is None or now < due_at:
            return
        # Move the deadline before I/O so a transient backend failure cannot turn
        # the one-second monitor into a destructive tight retry loop.
        self._next_artifact_retention_at = now + timedelta(
            seconds=retention.worker_interval_seconds
        )
        result = await self.artifact_retention.run_once(
            now=now,
            limit=retention.batch_size,
        )
        if result.tombstoned:
            _LOGGER.info(
                "Expired %d artifacts (%d bytes) under the configured retention policy",
                result.tombstoned,
                result.deleted_bytes,
            )
        if result.failures:
            self.observability.record_background_failure("artifact-retention")
            _LOGGER.warning(
                "Artifact retention completed with %d recoverable failures",
                len(result.failures),
            )

    async def _process_identity_maintenance(self, now: datetime) -> None:
        due_at = self._next_identity_maintenance_at
        if due_at is None or now < due_at:
            return
        # Advance before I/O so a transient database failure does not cause the
        # one-second monitor loop to hammer the maintenance path.
        self._next_identity_maintenance_at = now + _IDENTITY_MAINTENANCE_INTERVAL
        await self.identity_repository.prune_audit_events_for_retention(
            now - timedelta(days=self.config.audit.retention_days),
            batch_size=_IDENTITY_MAINTENANCE_BATCH_SIZE,
        )
        await self.identity_repository.prune_login_attempts_for_retention(
            now - timedelta(minutes=self.config.security.login_rate_limit.window_minutes),
            batch_size=_IDENTITY_MAINTENANCE_BATCH_SIZE,
        )

    async def _monitor_loop(self) -> None:
        interval = self.config.agent_gateway.monitor_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.observability.record_background_failure("control-plane-monitor")
                _LOGGER.exception("Control-plane monitor iteration failed")


def _control_actor_context(
    authentication_context: AuthenticationContext | None,
) -> ActorContext | None:
    if authentication_context is None:
        return None
    principal = authentication_context.principal
    return ActorContext(
        principal_id=principal.id,
        principal_type=principal.type,
        display_name=principal.display_name,
        organisation_id=principal.organisation_id,
        authorisation_snapshot_id=authentication_context.authorisation_snapshot_id,
    )


def _control_intent_metadata(
    authentication_context: AuthenticationContext | None,
    **metadata: object,
) -> dict[str, object]:
    actor_context = _control_actor_context(authentication_context)
    if actor_context is not None:
        metadata["actor_context"] = actor_context.model_dump(mode="json")
        metadata["authorisation_snapshot_id"] = (
            str(authentication_context.authorisation_snapshot_id)
            if authentication_context is not None
            and authentication_context.authorisation_snapshot_id is not None
            else None
        )
    return metadata


def _sqlite_path(url: str) -> Path:
    """Retained for callers that need to inspect an explicit SQLite URL path."""

    if not url.startswith("sqlite:///"):
        raise ConfigurationError(
            "SQLite path extraction requires a sqlite:/// database URL.",
            database_url_scheme=url.partition(":")[0],
        )
    value = url.removeprefix("sqlite:///")
    if not value:
        raise ConfigurationError("SQLite database URL must include a path.")
    return Path(value).expanduser()


def _build_artifact_storage(config: ControlPlaneConfig) -> ArtifactStorage:
    if config.artifacts.storage_backend == "local":
        return LocalArtifactStorage(config.artifacts.directory)
    s3 = config.artifacts.s3
    if s3.bucket is None:  # Defensive: configuration validation owns this invariant.
        raise ConfigurationError("S3 artifact storage requires a bucket name.")
    return S3CompatibleArtifactStorage(
        bucket=s3.bucket,
        prefix=s3.prefix,
        endpoint_url=s3.endpoint_url,
        region_name=s3.region_name,
    )
