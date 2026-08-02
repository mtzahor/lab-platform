from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from lab_platform.agent_protocol import MessageType
from lab_platform.agent_protocol.commands import (
    ArtifactUploadRequestPayload,
    DrainAgentPayload,
    InventoryRefreshRequestPayload,
    ReconciliationRequestPayload,
)
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.control_plane.gateway import (
    WS_AUTHENTICATION_FAILED,
    AgentConnectionHub,
    AgentGateway,
    AgentMessageRouter,
)
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
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.auth import ApiTokenService
from lab_platform.core.errors import ConfigurationError
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ArtifactTransferDirection,
    ArtifactTransferStatus,
    RemoteCommandStatus,
)
from lab_platform.persistence import (
    SQLiteApiTokenRepository,
    SQLiteCentralReservationLeaseRepository,
    SQLiteCiSessionRepository,
    SQLiteEventRepository,
    SQLiteGenericArtifactRepository,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.agents import SQLiteAgentEnrollmentRepository
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
from lab_platform.persistence.postgresql import create_control_plane_database


class ControlPlaneRuntime:
    """Composition root for the independent Phase 5 control-plane process."""

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self.database = create_control_plane_database(config.database.url)

        self.agent_repository = SQLiteAgentEnrollmentRepository(self.database)
        self.enrollment = AgentEnrollmentService(self.agent_repository)
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
        self.reservations = CentralReservationLeaseService(
            self.reservation_repository,
            self.directory,
            self.lease_synchronizer,
            maximum_clock_skew_seconds=(config.distributed.maximum_clock_skew_seconds),
            offline_reservation_grace_seconds=(
                config.distributed.offline_reservation_grace_seconds
            ),
        )

        self.artifact_transfer_repository = SQLiteArtifactTransferServiceRepository(self.database)
        self.artifact_store = FilesystemTransferStore(config.artifacts.directory)
        self.artifacts = DistributedArtifactService(
            self.artifact_transfer_repository,
            self.artifact_store,
            maximum_upload_size_bytes=config.artifacts.max_upload_size_mb * 1024 * 1024,
            token_ttl_seconds=config.artifacts.transfer_token_ttl_seconds,
        )
        self._artifact_upload_capabilities: dict[UUID, IssuedArtifactTransfer] = {}
        self._artifact_upload_requested_at: dict[UUID, datetime] = {}
        self._artifact_upload_lock = asyncio.Lock()
        self.remote_artifacts = SQLiteRemoteArtifactRepository(self.database)
        self.platform_artifact_repository = SQLiteGenericArtifactRepository(self.database)
        self.platform_artifacts = ArtifactService(
            self.platform_artifact_repository,
            config.artifacts.directory,
            maximum_size_bytes=config.artifacts.max_upload_size_mb * 1024 * 1024,
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
            maximum_size_bytes=config.artifacts.max_upload_size_mb * 1024 * 1024,
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
        self.workflows = DistributedWorkflowCoordinator(
            self.inventory,
            self.presence,
            self.reservations,
            self.workflow_artifacts,
            self.commands,
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
        )

        self.benches = SQLiteGlobalBenchRepository(self.database)
        self.artifact_transfers = SQLiteArtifactTransferRepository(self.database)
        self.raw_protocol_messages = SQLiteProtocolMessageJournalRepository(self.database)
        self.api_tokens = SQLiteApiTokenRepository(self.database)
        self.audit_events = SQLiteEventRepository(self.database)
        self.token_service = ApiTokenService(self.api_tokens, self.audit_events)
        self._monitor_task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        try:
            self.database.initialize()
            self.config.artifacts.directory.mkdir(parents=True, exist_ok=True)
            await self.ci.recover_incomplete()
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="control-plane-monitor",
            )
            self._started = True
        except Exception:
            if self._monitor_task is not None:
                self._monitor_task.cancel()
                await asyncio.gather(self._monitor_task, return_exceptions=True)
                self._monitor_task = None
            self.database.close()
            self._started = False
            raise

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        self.database.close()
        self._started = False

    async def refresh_inventory(self, agent_id: UUID) -> UUID:
        await self.presence.get_agent(agent_id)
        request_id = uuid4()
        await self.hub.send(
            agent_id,
            MessageType.INVENTORY_REFRESH_REQUEST,
            InventoryRefreshRequestPayload(request_id=request_id),
            correlation_id=request_id,
        )
        await self.record_timeline(
            agent_id,
            "INVENTORY_REFRESH_REQUESTED",
            "A fresh Agent inventory snapshot was requested.",
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
                    maximum_size_bytes=(self.config.artifacts.max_upload_size_mb * 1024 * 1024),
                    expected_sha256=transfer.expected_sha256,
                ),
                correlation_id=artifact_id,
            )
            self._artifact_upload_requested_at[artifact_id] = now
            return issued

    async def request_reconciliation(self, agent_id: UUID) -> UUID:
        connection = await self.presence.active_connection(agent_id)
        if connection is None:
            await self.presence.get_agent(agent_id)
            raise RuntimeError("Agent is not connected")
        request_id = uuid4()
        await self.hub.send(
            agent_id,
            MessageType.RECONCILIATION_REQUEST,
            ReconciliationRequestPayload(
                request_id=request_id,
                expected_boot_id=connection.connection.boot_id,
                last_control_plane_sequence=connection.connection.last_sequence_number,
            ),
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
    ) -> DrainResult:
        result = await self.drain.drain(
            agent_id,
            cancel_queued_work=cancel_queued_work,
        )
        if await self.hub.is_connected(agent_id):
            await self.hub.send(
                agent_id,
                MessageType.DRAIN_AGENT,
                DrainAgentPayload(drain=True),
                correlation_id=uuid4(),
            )
        await self.record_timeline(
            agent_id,
            "AGENT_DRAIN_REQUESTED",
            "Agent entered drain mode.",
            metadata={"cancelled_queued_work": result.cancelled_queued_work},
        )
        return result

    async def undrain_agent(self, agent_id: UUID) -> AgentRecord:
        agent = await self.drain.undrain(agent_id)
        await self.hub.send(
            agent_id,
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(drain=False),
            correlation_id=uuid4(),
        )
        await self.record_timeline(
            agent_id,
            "AGENT_UNDRAINED",
            "Agent left drain mode.",
        )
        return agent

    async def revoke_agent(self, agent_id: UUID) -> AgentRecord:
        agent = await self.enrollment.revoke_agent(agent_id)
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
    ) -> AgentTimelineRecord:
        return await self.timeline.append(
            AgentTimelineRecord(
                agent_id=agent_id,
                event_type=event_type,
                severity=severity,
                message=message,
                correlation_id=correlation_id,
                metadata=metadata or {},
            )
        )

    async def metrics(self) -> dict[str, int | float]:
        with self.database.transaction() as connection:

            def scalar(query: str) -> int | float:
                value = connection.execute(query).fetchone()[0]
                if not isinstance(value, (int, float)):
                    raise RuntimeError("Metric query did not return a number")
                return value

            metrics: dict[str, int | float] = {
                "agents_online": scalar("SELECT COUNT(*) FROM agents WHERE status = 'ONLINE'"),
                "agents_offline": scalar("SELECT COUNT(*) FROM agents WHERE status = 'OFFLINE'"),
                "agent_reconnects_total": scalar(
                    "SELECT MAX(0, COUNT(*) - COUNT(DISTINCT agent_id)) FROM agent_connections"
                ),
                "agent_heartbeat_lag_seconds": scalar(
                    "SELECT COALESCE(MAX((julianday('now') - julianday(last_heartbeat_at)) "
                    "* 86400.0), 0) FROM agent_connections WHERE disconnected_at IS NULL"
                ),
                "remote_commands_pending": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status IN "
                    "('CREATED', 'QUEUED', 'DISPATCHED', 'ACCEPTED', 'UNKNOWN')"
                ),
                "remote_commands_running": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status = 'RUNNING'"
                ),
                "remote_commands_failed": scalar(
                    "SELECT COUNT(*) FROM remote_commands WHERE status = 'FAILED'"
                ),
                "operation_reconciliations_total": scalar(
                    "SELECT COUNT(*) FROM reconciliation_report_claims WHERE status = 'COMPLETE'"
                ),
                "artifact_transfer_bytes": scalar(
                    "SELECT COALESCE(SUM(bytes_transferred), 0) FROM artifact_transfer_attempts"
                ),
                "artifact_transfer_failures": scalar(
                    "SELECT COUNT(*) FROM artifact_transfers WHERE status = 'FAILED'"
                ),
                "bench_inventory_total": scalar("SELECT COUNT(*) FROM global_benches"),
                "bench_inventory_offline": scalar(
                    "SELECT COUNT(*) FROM global_benches WHERE status = 'OFFLINE'"
                ),
                "ci_sessions_waiting": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status IN "
                    "('created', 'waiting_for_bench')"
                ),
                "ci_sessions_running": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status IN "
                    "('reserved', 'running', 'cancel_requested')"
                ),
                "ci_sessions_cleanup_pending": scalar(
                    "SELECT COUNT(*) FROM ci_sessions WHERE status = 'cleanup_pending'"
                ),
            }
        hub_metrics = await self.hub.metrics()
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
        await self.reconciliation_service.timeout_unreconciled_operations()
        await self.workflow_reservations.release_terminal()
        await self.ci.process_maintenance()
        for agent in await self.presence.list_agents(status=AgentStatus.DRAINING):
            with suppress(Exception):
                await self.drain.refresh(agent.id)

    async def _monitor_loop(self) -> None:
        interval = self.config.agent_gateway.monitor_interval_seconds
        while True:
            await asyncio.sleep(interval)
            with suppress(Exception):
                await self.monitor_once()


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
