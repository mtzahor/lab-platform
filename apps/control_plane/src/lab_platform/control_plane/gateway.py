from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    AgentHeartbeatEnvelope,
    AgentHelloEnvelope,
    AgentStatusEnvelope,
    ArtifactCreatedEnvelope,
    ArtifactCreatedPayload,
    ArtifactUploadRequestEnvelope,
    BenchAddedEnvelope,
    BenchHealthChangedEnvelope,
    BenchRemovedEnvelope,
    BenchSnapshotEnvelope,
    CommandAcceptedEnvelope,
    CommandAcceptedPayload,
    CommandCancelEnvelope,
    CommandRejectedEnvelope,
    CommandRejectedPayload,
    CommandRequestEnvelope,
    ConfigRefreshRequestEnvelope,
    DrainAgentEnvelope,
    EnvelopeBase,
    EventAckEnvelope,
    EventAckPayload,
    EventBatchEnvelope,
    IncomingSequenceTracker,
    InventoryRefreshRequestEnvelope,
    MessageType,
    OperationEventEnvelope,
    OperationEventPayload,
    ProtocolMessageInvalidError,
    ReconciliationReportEnvelope,
    ReconciliationRequestEnvelope,
    ReservationActivatedEnvelope,
    ReservationLeaseAppliedPayload,
    ReservationReleasedEnvelope,
    SequenceDisposition,
    SupportedEnvelope,
    WelcomeEnvelope,
    WelcomePayload,
    WorkflowProgressPayload,
    parse_agent_message,
)
from lab_platform.agent_protocol.commands import (
    ArtifactUploadRequestPayload,
    CommandCancelPayload,
    CommandRequestPayload,
    ConfigRefreshRequestPayload,
    DrainAgentPayload,
    InventoryRefreshRequestPayload,
    ReconciliationRequestPayload,
    ReservationActivatedPayload,
    ReservationReleasedPayload,
)
from lab_platform.control_plane.config import AgentGatewaySettings, DistributedSettings
from lab_platform.control_plane_core.artifacts import DistributedArtifactService
from lab_platform.control_plane_core.commands import (
    AgentCommandTransport,
    CommandDeliveryReceipt,
    RemoteCommandService,
)
from lab_platform.control_plane_core.enrollment import AgentEnrollmentService
from lab_platform.control_plane_core.errors import AgentAuthenticationFailedError
from lab_platform.control_plane_core.inventory import InventoryService
from lab_platform.control_plane_core.presence import AgentPresenceService
from lab_platform.core.errors import PlatformError
from lab_platform.models import (
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ProtocolMessageDirection,
    ProtocolMessageJournalRecord,
    ProtocolMessageOutcome,
    ReconciliationReport,
)
from starlette.websockets import WebSocketDisconnect, WebSocketState

WS_AUTHENTICATION_FAILED = 4401
WS_PROTOCOL_ERROR = 4400
WS_AGENT_SUPERSEDED = 4409
WS_MESSAGE_TOO_LARGE = 4403
WS_BACKPRESSURE = 4429
WS_INTERNAL_ERROR = 4500


class GatewayMessageTooLargeError(ValueError):
    """An Agent control message exceeded the configured transport bound."""


class ProtocolMessageJournal(Protocol):
    async def record_protocol_message(
        self,
        record: ProtocolMessageJournalRecord,
    ) -> bool: ...

    async def finalize_protocol_message(
        self,
        record: ProtocolMessageJournalRecord,
    ) -> None: ...


class AgentTimelineSink(Protocol):
    async def append(self, entry: AgentTimelineRecord) -> AgentTimelineRecord: ...


class ReconciliationReportHandler(Protocol):
    async def reconcile(
        self,
        report_id: UUID,
        report: ReconciliationReport,
    ) -> object: ...


class ArtifactUploadRequester(Protocol):
    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> object: ...


class ReservationLeaseReceiptHandler(Protocol):
    async def confirm(self, payload: ReservationLeaseAppliedPayload) -> bool: ...


class ReservationDisconnectHandler(Protocol):
    async def mark_agent_disconnected(
        self,
        agent_id: UUID,
        *,
        disconnect_id: UUID,
    ) -> object: ...


@dataclass(slots=True)
class _AgentSocketSession:
    agent_id: UUID
    connection_id: UUID
    boot_id: UUID
    websocket: WebSocket
    outgoing: asyncio.Queue[EnvelopeBase]
    next_outgoing_sequence: int = 1
    outgoing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def make_envelope(
        self,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None,
        now: datetime,
    ) -> EnvelopeBase:
        async with self.outgoing_lock:
            sequence_number = self.next_outgoing_sequence
            self.next_outgoing_sequence += 1
        common = {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": uuid4(),
            "message_type": message_type,
            "agent_id": self.agent_id,
            "sent_at": now,
            "correlation_id": correlation_id,
            "sequence_number": sequence_number,
            "payload": payload,
        }
        return _control_plane_envelope(message_type, common)


class AgentConnectionHub(AgentCommandTransport):
    """Fenced, bounded outgoing channels for authenticated Agent sockets."""

    def __init__(
        self,
        *,
        outgoing_queue_size: int = 256,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if outgoing_queue_size <= 0:
            raise ValueError("outgoing_queue_size must be positive")
        self._queue_size = outgoing_queue_size
        self._clock = clock
        self._sessions: dict[UUID, _AgentSocketSession] = {}
        self._lock = asyncio.Lock()

    async def attach(
        self,
        *,
        agent_id: UUID,
        connection_id: UUID,
        boot_id: UUID,
        websocket: WebSocket,
    ) -> _AgentSocketSession:
        session = _AgentSocketSession(
            agent_id=agent_id,
            connection_id=connection_id,
            boot_id=boot_id,
            websocket=websocket,
            outgoing=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            previous = self._sessions.get(agent_id)
            self._sessions[agent_id] = session
        if previous is not None and previous.connection_id != connection_id:
            await _close_socket(previous.websocket, WS_AGENT_SUPERSEDED, "Superseded connection")
        return session

    async def detach(self, agent_id: UUID, connection_id: UUID) -> bool:
        async with self._lock:
            current = self._sessions.get(agent_id)
            if current is None or current.connection_id != connection_id:
                return False
            self._sessions.pop(agent_id, None)
            return True

    async def is_connected(self, agent_id: UUID) -> bool:
        async with self._lock:
            session = self._sessions.get(agent_id)
            return (
                session is not None
                and session.websocket.application_state is WebSocketState.CONNECTED
            )

    async def close_agent(
        self,
        agent_id: UUID,
        *,
        code: int = WS_AGENT_SUPERSEDED,
        reason: str = "Connection closed by control plane",
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(agent_id)
        if session is None:
            return False
        await _close_socket(session.websocket, code, reason)
        return True

    async def send_command(
        self,
        agent_id: UUID,
        payload: CommandRequestPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        envelope = await self.send(
            agent_id,
            MessageType.COMMAND_REQUEST,
            payload,
            correlation_id=correlation_id,
        )
        session = await self.require_session(agent_id)
        return CommandDeliveryReceipt(
            connection_id=session.connection_id,
            sequence_number=envelope.sequence_number,
            dispatched_at=envelope.sent_at,
        )

    async def send_cancel(
        self,
        agent_id: UUID,
        payload: CommandCancelPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        envelope = await self.send(
            agent_id,
            MessageType.COMMAND_CANCEL,
            payload,
            correlation_id=correlation_id,
        )
        session = await self.require_session(agent_id)
        return CommandDeliveryReceipt(
            connection_id=session.connection_id,
            sequence_number=envelope.sequence_number,
            dispatched_at=envelope.sent_at,
        )

    async def send(
        self,
        agent_id: UUID,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None = None,
        expected_connection_id: UUID | None = None,
    ) -> EnvelopeBase:
        session = await self.require_session(agent_id)
        if expected_connection_id is not None and session.connection_id != expected_connection_id:
            raise RuntimeError("Agent WebSocket session was superseded")
        envelope = await session.make_envelope(
            message_type,
            payload,
            correlation_id=correlation_id,
            now=_utc(self._clock()),
        )
        try:
            session.outgoing.put_nowait(envelope)
        except asyncio.QueueFull as exc:
            await _close_socket(session.websocket, WS_BACKPRESSURE, "Outgoing queue full")
            raise RuntimeError("Agent outgoing queue is full") from exc
        return envelope

    async def require_session(self, agent_id: UUID) -> _AgentSocketSession:
        async with self._lock:
            session = self._sessions.get(agent_id)
        if session is None or session.websocket.application_state is not WebSocketState.CONNECTED:
            raise RuntimeError("Agent has no active WebSocket session")
        return session

    async def metrics(self, *, agent_ids: set[UUID] | None = None) -> dict[str, int]:
        async with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if agent_ids is None or session.agent_id in agent_ids
            ]
        return {
            "connected_agents": len(sessions),
            "queued_messages": sum(session.outgoing.qsize() for session in sessions),
        }


class AgentMessageRouter:
    def __init__(
        self,
        *,
        presence: AgentPresenceService,
        inventory: InventoryService,
        commands: RemoteCommandService,
        artifacts: DistributedArtifactService | None = None,
        artifact_uploads: ArtifactUploadRequester | None = None,
        lease_receipts: ReservationLeaseReceiptHandler | None = None,
        reservation_disconnects: ReservationDisconnectHandler | None = None,
        reconciliation: ReconciliationReportHandler | None = None,
    ) -> None:
        self._presence = presence
        self._inventory = inventory
        self._commands = commands
        self._artifacts = artifacts
        self._artifact_uploads = artifact_uploads
        self._lease_receipts = lease_receipts
        self._reservation_disconnects = reservation_disconnects
        self._reconciliation = reconciliation

    async def handle(
        self,
        envelope: SupportedEnvelope,
        *,
        connection_id: UUID,
        boot_id: UUID,
        observed_at: datetime,
        observed_monotonic: float,
    ) -> None:
        clock_offset: float | None = None
        if isinstance(envelope, AgentHeartbeatEnvelope):
            clock_offset = (observed_at - envelope.payload.timestamp).total_seconds()
        await self._presence.record_heartbeat(
            envelope.agent_id,
            connection_id=connection_id,
            boot_id=boot_id,
            sequence_number=envelope.sequence_number,
            observed_at=observed_at,
            observed_monotonic=observed_monotonic,
            observed_clock_offset_seconds=clock_offset,
        )
        agent = await self._presence.get_agent(envelope.agent_id)

        if isinstance(envelope, BenchSnapshotEnvelope):
            await self._inventory.reconcile_snapshot(
                agent,
                envelope.payload,
                observed_at=observed_at,
                expected_boot_id=boot_id,
            )
        elif isinstance(envelope, BenchAddedEnvelope):
            await self._inventory.apply_bench_added(
                agent,
                envelope.payload,
                observed_at=observed_at,
                expected_boot_id=boot_id,
            )
        elif isinstance(envelope, BenchRemovedEnvelope):
            await self._inventory.apply_bench_removed(
                agent,
                envelope.payload,
                observed_at=observed_at,
                expected_boot_id=boot_id,
            )
        elif isinstance(envelope, BenchHealthChangedEnvelope):
            await self._inventory.apply_bench_health_changed(
                agent,
                envelope.payload,
                observed_at=observed_at,
                expected_boot_id=boot_id,
            )
        elif isinstance(envelope, CommandAcceptedEnvelope):
            await self._commands.accepted(envelope.agent_id, envelope.payload)
        elif isinstance(envelope, CommandRejectedEnvelope):
            await self._commands.rejected(envelope.agent_id, envelope.payload)
        elif isinstance(envelope, OperationEventEnvelope):
            await self._commands.operation_event(
                envelope.agent_id,
                envelope.message_type,
                envelope.payload,
            )
        elif isinstance(envelope, AgentStatusEnvelope):
            if self._lease_receipts is not None and envelope.payload.lease_application is not None:
                await self._lease_receipts.confirm(envelope.payload.lease_application)
        elif isinstance(envelope, ArtifactCreatedEnvelope):
            await self._handle_artifact_created(
                envelope.agent_id,
                envelope.payload,
            )
        elif isinstance(envelope, ReconciliationReportEnvelope):
            if self._reconciliation is not None:
                await self._reconciliation.reconcile(
                    envelope.message_id,
                    envelope.payload.report,
                )
        elif isinstance(envelope, EventBatchEnvelope):
            await self._handle_event_batch(envelope)

    async def _handle_event_batch(self, envelope: EventBatchEnvelope) -> None:
        for event in envelope.payload.events:
            try:
                message_type = MessageType(event.event_type)
            except ValueError:
                continue
            if message_type is MessageType.COMMAND_ACCEPTED:
                payload = CommandAcceptedPayload.model_validate(event.payload)
                await self._commands.accepted(envelope.agent_id, payload)
            elif message_type is MessageType.COMMAND_REJECTED:
                rejected = CommandRejectedPayload.model_validate(event.payload)
                await self._commands.rejected(envelope.agent_id, rejected)
            elif message_type in {
                MessageType.OPERATION_STARTED,
                MessageType.OPERATION_PROGRESS,
                MessageType.OPERATION_SUCCEEDED,
                MessageType.OPERATION_FAILED,
                MessageType.OPERATION_CANCELLED,
            }:
                operation = OperationEventPayload.model_validate(event.payload)
                await self._commands.operation_event(
                    envelope.agent_id,
                    message_type,
                    operation,
                )
            elif message_type is MessageType.WORKFLOW_PROGRESS:
                progress = WorkflowProgressPayload.model_validate(event.payload)
                await self._commands.operation_event(
                    envelope.agent_id,
                    MessageType.OPERATION_PROGRESS,
                    OperationEventPayload(
                        command_id=progress.command_id,
                        local_operation_id=progress.local_workflow_run_id,
                        occurred_at=progress.occurred_at,
                        progress=progress.progress,
                        message=progress.message or progress.step_name,
                        result={
                            "workflow_step_index": progress.step_index,
                            "workflow_step_count": progress.step_count,
                            "workflow_step_name": progress.step_name,
                        },
                    ),
                )
            elif message_type is MessageType.ARTIFACT_CREATED:
                artifact = ArtifactCreatedPayload.model_validate(event.payload)
                await self._handle_artifact_created(envelope.agent_id, artifact)

    async def _handle_artifact_created(
        self,
        agent_id: UUID,
        payload: ArtifactCreatedPayload,
    ) -> None:
        if self._artifacts is None:
            return
        if payload.artifact.agent_id != agent_id:
            raise ProtocolMessageInvalidError(
                "Artifact metadata Agent identity does not match the authenticated stream.",
                authenticated_agent_id=str(agent_id),
                artifact_agent_id=str(payload.artifact.agent_id),
            )
        artifact = await self._artifacts.register_remote_artifact(payload.artifact)
        if self._artifact_uploads is not None:
            await self._artifact_uploads.request_artifact_upload(agent_id, artifact.id)

    async def mark_agent_unknown(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
        disconnect_id: UUID | None = None,
    ) -> int:
        changed = await self._commands.mark_unknown(agent_id, observed_at=observed_at)
        if self._reservation_disconnects is not None:
            await self._reservation_disconnects.mark_agent_disconnected(
                agent_id,
                disconnect_id=disconnect_id or uuid4(),
            )
        return changed


class AgentGateway:
    def __init__(
        self,
        *,
        enrollment: AgentEnrollmentService,
        presence: AgentPresenceService,
        hub: AgentConnectionHub,
        router: AgentMessageRouter,
        gateway_settings: AgentGatewaySettings,
        distributed_settings: DistributedSettings,
        protocol_journal: ProtocolMessageJournal | None = None,
        timeline: AgentTimelineSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enrollment = enrollment
        self._presence = presence
        self._hub = hub
        self._router = router
        self._settings = gateway_settings
        self._distributed = distributed_settings
        self._journal = protocol_journal
        self._timeline = timeline
        self._clock = clock
        self._monotonic = monotonic

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.websocket("/api/v1/agent-gateway/{agent_id}")
        async def agent_gateway(websocket: WebSocket, agent_id: UUID) -> None:
            await self.serve(websocket, agent_id)

        return router

    async def serve(self, websocket: WebSocket, agent_id: UUID) -> None:
        connection_id = uuid4()
        boot_id: UUID | None = None
        attached = False
        try:
            credential = _bearer_secret(websocket.headers.get("authorization"))
            authenticated = await self._enrollment.authenticate(agent_id, credential)
            await websocket.accept()
            raw = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=self._settings.handshake_timeout_seconds,
            )
            value = _decode_message(raw, self._settings.maximum_message_size_mb)
            hello = parse_agent_message(value)
            if not isinstance(hello, AgentHelloEnvelope):
                raise ProtocolMessageInvalidError("First Agent message must be AGENT_HELLO.")
            if hello.agent_id != agent_id or hello.sequence_number != 1:
                raise ProtocolMessageInvalidError(
                    "AGENT_HELLO identity or initial sequence is invalid."
                )
            boot_id = hello.payload.boot_id
            tracker = IncomingSequenceTracker(
                expected_sequence=1,
                max_recent_message_ids=self._settings.sequence_window_size,
            )
            tracker.observe(hello)
            observed_at = _utc(self._clock())
            observed_monotonic = self._monotonic()
            prior = await self._presence.active_connection(agent_id)
            await self._presence.register_authenticated_connection(
                authenticated.agent,
                connection_id=connection_id,
                boot_id=boot_id,
                protocol_version=hello.protocol_version,
                observed_at=observed_at,
                observed_monotonic=observed_monotonic,
                sequence_number=hello.sequence_number,
                agent_version=hello.payload.agent_version,
                observed_clock_offset_seconds=(observed_at - hello.sent_at).total_seconds(),
            )
            session = await self._hub.attach(
                agent_id=agent_id,
                connection_id=connection_id,
                boot_id=boot_id,
                websocket=websocket,
            )
            attached = True
            await self._record(hello, connection_id, observed_at, ProtocolMessageOutcome.HANDLED)
            welcome = await self._hub.send(
                agent_id,
                MessageType.WELCOME,
                WelcomePayload(
                    connection_id=connection_id,
                    accepted_protocol_version=hello.protocol_version,
                    server_time=observed_at,
                    heartbeat_interval_seconds=self._settings.heartbeat_interval_seconds,
                    heartbeat_timeout_seconds=self._settings.heartbeat_timeout_seconds,
                    offline_timeout_seconds=self._settings.offline_timeout_seconds,
                ),
                correlation_id=hello.message_id,
            )
            await websocket.send_text(welcome.model_dump_json())
            await self._record(
                welcome,
                connection_id,
                observed_at,
                ProtocolMessageOutcome.SENT,
            )
            await self._append_timeline(
                agent_id,
                "AGENT_CONNECTED",
                "Agent established an authenticated protocol connection.",
                correlation_id=hello.message_id,
                metadata={
                    "connection_id": str(connection_id),
                    "boot_id": str(boot_id),
                    "protocol_version": hello.protocol_version,
                },
            )
            # The WELCOME was sent directly, so it must not remain queued for the writer.
            queued_welcome = session.outgoing.get_nowait()
            if (
                queued_welcome.message_id != welcome.message_id
            ):  # pragma: no cover - local invariant
                raise RuntimeError("Agent outgoing queue ordering was corrupted")

            restarted = prior is not None and prior.connection.boot_id != boot_id
            await self._hub.send(
                agent_id,
                MessageType.RECONCILIATION_REQUEST,
                ReconciliationRequestPayload(
                    request_id=uuid4(),
                    expected_boot_id=boot_id if restarted else None,
                    last_control_plane_sequence=hello.payload.last_acknowledged_command_sequence,
                ),
            )
            await self._run_connection(websocket, session, tracker)
        except WebSocketDisconnect:
            pass
        except AgentAuthenticationFailedError:
            await _close_socket(websocket, WS_AUTHENTICATION_FAILED, "Authentication failed")
        except GatewayMessageTooLargeError:
            await _close_socket(websocket, WS_MESSAGE_TOO_LARGE, "Message too large")
        except (PlatformError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            await _close_socket(websocket, WS_PROTOCOL_ERROR, _safe_reason(exc))
        except Exception:
            await _close_socket(websocket, WS_INTERNAL_ERROR, "Agent gateway internal error")
        finally:
            if attached:
                await self._hub.detach(agent_id, connection_id)
            if boot_id is not None:
                disconnected = False
                with suppress(Exception):
                    disconnected = await self._presence.disconnect(
                        agent_id,
                        connection_id=connection_id,
                        boot_id=boot_id,
                        observed_at=_utc(self._clock()),
                    )
                if disconnected:
                    with suppress(Exception):
                        await self._router.mark_agent_unknown(
                            agent_id,
                            observed_at=_utc(self._clock()),
                            disconnect_id=connection_id,
                        )
                    with suppress(Exception):
                        await self._append_timeline(
                            agent_id,
                            "AGENT_DISCONNECTED",
                            "Agent protocol connection closed; active work awaits reconciliation.",
                            severity=AgentTimelineSeverity.WARNING,
                            metadata={
                                "connection_id": str(connection_id),
                                "boot_id": str(boot_id),
                            },
                        )

    async def _run_connection(
        self,
        websocket: WebSocket,
        session: _AgentSocketSession,
        tracker: IncomingSequenceTracker,
    ) -> None:
        reader = asyncio.create_task(
            self._reader(websocket, session, tracker),
            name=f"agent-reader-{session.agent_id}",
        )
        writer = asyncio.create_task(
            self._writer(websocket, session),
            name=f"agent-writer-{session.agent_id}",
        )
        done, pending = await asyncio.wait(
            {reader, writer},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error

    async def _reader(
        self,
        websocket: WebSocket,
        session: _AgentSocketSession,
        tracker: IncomingSequenceTracker,
    ) -> None:
        while True:
            raw = await websocket.receive_text()
            value = _decode_message(raw, self._settings.maximum_message_size_mb)
            envelope = parse_agent_message(value)
            if envelope.agent_id != session.agent_id or isinstance(envelope, AgentHelloEnvelope):
                raise ProtocolMessageInvalidError("Agent stream identity or handshake is invalid.")
            disposition = tracker.observe(envelope)
            observed_at = _utc(self._clock())
            if disposition is SequenceDisposition.DUPLICATE:
                await self._record(
                    envelope,
                    session.connection_id,
                    observed_at,
                    ProtocolMessageOutcome.DUPLICATE,
                )
                if isinstance(envelope, EventBatchEnvelope):
                    await self._acknowledge_event_batch(session, envelope)
                continue
            is_new = await self._record(
                envelope,
                session.connection_id,
                observed_at,
                ProtocolMessageOutcome.RECEIVED,
            )
            # A replayed EVENT_BATCH can represent a crash after durable receipt but before
            # domain routing. Route it again through idempotent handlers before acknowledging it.
            if not is_new and not isinstance(envelope, EventBatchEnvelope):
                continue
            try:
                await self._router.handle(
                    envelope,
                    connection_id=session.connection_id,
                    boot_id=session.boot_id,
                    observed_at=observed_at,
                    observed_monotonic=self._monotonic(),
                )
            except Exception:
                await self._finalize(
                    envelope,
                    session.connection_id,
                    observed_at,
                    ProtocolMessageOutcome.REJECTED,
                )
                raise
            await self._finalize(
                envelope,
                session.connection_id,
                observed_at,
                ProtocolMessageOutcome.HANDLED,
            )
            if isinstance(envelope, EventBatchEnvelope):
                await self._acknowledge_event_batch(session, envelope)

            if isinstance(envelope, AgentHeartbeatEnvelope):
                offset = abs((observed_at - envelope.payload.timestamp).total_seconds())
                if offset > self._distributed.maximum_clock_skew_seconds:
                    await self._presence.mark_degraded(
                        session.agent_id,
                        connection_id=session.connection_id,
                        boot_id=session.boot_id,
                    )

    async def _acknowledge_event_batch(
        self,
        session: _AgentSocketSession,
        envelope: EventBatchEnvelope,
    ) -> None:
        if not envelope.payload.events:
            raise ProtocolMessageInvalidError("EVENT_BATCH must contain at least one event.")
        await self._hub.send(
            session.agent_id,
            MessageType.EVENT_ACK,
            EventAckPayload(
                batch_message_id=envelope.message_id,
                acknowledged_event_sequence=(envelope.payload.events[-1].sequence_number),
            ),
            correlation_id=envelope.message_id,
            expected_connection_id=session.connection_id,
        )

    async def _writer(self, websocket: WebSocket, session: _AgentSocketSession) -> None:
        while True:
            envelope = await session.outgoing.get()
            await websocket.send_text(envelope.model_dump_json())
            await self._record(
                envelope,
                session.connection_id,
                _utc(self._clock()),
                ProtocolMessageOutcome.SENT,
            )

    async def _record(
        self,
        envelope: EnvelopeBase,
        connection_id: UUID,
        observed_at: datetime,
        outcome: ProtocolMessageOutcome,
    ) -> bool:
        if self._journal is None:
            return True
        canonical = _canonical_payload(envelope)
        direction = (
            ProtocolMessageDirection.AGENT_TO_CONTROL_PLANE
            if envelope.message_type
            in {
                MessageType.AGENT_HELLO,
                MessageType.AGENT_HEARTBEAT,
                MessageType.AGENT_STATUS,
                MessageType.BENCH_SNAPSHOT,
                MessageType.BENCH_ADDED,
                MessageType.BENCH_REMOVED,
                MessageType.BENCH_HEALTH_CHANGED,
                MessageType.COMMAND_ACCEPTED,
                MessageType.COMMAND_REJECTED,
                MessageType.OPERATION_STARTED,
                MessageType.OPERATION_PROGRESS,
                MessageType.OPERATION_SUCCEEDED,
                MessageType.OPERATION_FAILED,
                MessageType.OPERATION_CANCELLED,
                MessageType.WORKFLOW_PROGRESS,
                MessageType.ARTIFACT_CREATED,
                MessageType.EVENT_BATCH,
                MessageType.RECONCILIATION_REPORT,
            }
            else ProtocolMessageDirection.CONTROL_PLANE_TO_AGENT
        )
        return await self._journal.record_protocol_message(
            ProtocolMessageJournalRecord(
                message_id=envelope.message_id,
                agent_id=envelope.agent_id,
                connection_id=connection_id,
                direction=direction,
                sequence_number=envelope.sequence_number,
                message_type=envelope.message_type.value,
                correlation_id=envelope.correlation_id,
                payload_sha256=hashlib.sha256(canonical).hexdigest(),
                observed_at=observed_at,
                handled_at=(observed_at if outcome is ProtocolMessageOutcome.HANDLED else None),
                outcome=outcome,
            )
        )

    async def _finalize(
        self,
        envelope: EnvelopeBase,
        connection_id: UUID,
        observed_at: datetime,
        outcome: ProtocolMessageOutcome,
    ) -> None:
        if self._journal is None:
            return
        canonical = _canonical_payload(envelope)
        await self._journal.finalize_protocol_message(
            ProtocolMessageJournalRecord(
                message_id=envelope.message_id,
                agent_id=envelope.agent_id,
                connection_id=connection_id,
                direction=ProtocolMessageDirection.AGENT_TO_CONTROL_PLANE,
                sequence_number=envelope.sequence_number,
                message_type=envelope.message_type.value,
                correlation_id=envelope.correlation_id,
                payload_sha256=hashlib.sha256(canonical).hexdigest(),
                observed_at=observed_at,
                handled_at=observed_at,
                outcome=outcome,
            )
        )

    async def _append_timeline(
        self,
        agent_id: UUID,
        event_type: str,
        message: str,
        *,
        severity: AgentTimelineSeverity = AgentTimelineSeverity.INFO,
        correlation_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if self._timeline is None:
            return
        await self._timeline.append(
            AgentTimelineRecord(
                agent_id=agent_id,
                timestamp=_utc(self._clock()),
                event_type=event_type,
                severity=severity,
                message=message,
                correlation_id=correlation_id,
                metadata=metadata or {},
                deduplication_key=(
                    f"agent:{agent_id}:{event_type}:{correlation_id}"
                    if correlation_id is not None
                    else None
                ),
            )
        )


def _control_plane_envelope(
    message_type: MessageType,
    values: dict[str, object],
) -> EnvelopeBase:
    envelope_types: dict[MessageType, type[EnvelopeBase]] = {
        MessageType.WELCOME: WelcomeEnvelope,
        MessageType.COMMAND_REQUEST: CommandRequestEnvelope,
        MessageType.COMMAND_CANCEL: CommandCancelEnvelope,
        MessageType.INVENTORY_REFRESH_REQUEST: InventoryRefreshRequestEnvelope,
        MessageType.RECONCILIATION_REQUEST: ReconciliationRequestEnvelope,
        MessageType.ARTIFACT_UPLOAD_REQUEST: ArtifactUploadRequestEnvelope,
        MessageType.CONFIG_REFRESH_REQUEST: ConfigRefreshRequestEnvelope,
        MessageType.DRAIN_AGENT: DrainAgentEnvelope,
        MessageType.RESERVATION_ACTIVATED: ReservationActivatedEnvelope,
        MessageType.RESERVATION_RELEASED: ReservationReleasedEnvelope,
        MessageType.EVENT_ACK: EventAckEnvelope,
    }
    expected_payloads: dict[MessageType, type[object]] = {
        MessageType.WELCOME: WelcomePayload,
        MessageType.COMMAND_REQUEST: CommandRequestPayload,
        MessageType.COMMAND_CANCEL: CommandCancelPayload,
        MessageType.INVENTORY_REFRESH_REQUEST: InventoryRefreshRequestPayload,
        MessageType.RECONCILIATION_REQUEST: ReconciliationRequestPayload,
        MessageType.ARTIFACT_UPLOAD_REQUEST: ArtifactUploadRequestPayload,
        MessageType.CONFIG_REFRESH_REQUEST: ConfigRefreshRequestPayload,
        MessageType.DRAIN_AGENT: DrainAgentPayload,
        MessageType.RESERVATION_ACTIVATED: ReservationActivatedPayload,
        MessageType.RESERVATION_RELEASED: ReservationReleasedPayload,
        MessageType.EVENT_ACK: EventAckPayload,
    }
    envelope_type = envelope_types.get(message_type)
    payload_type = expected_payloads.get(message_type)
    if (
        envelope_type is None
        or payload_type is None
        or not isinstance(values.get("payload"), payload_type)
    ):
        raise ValueError(f"Unsupported or invalid control-plane message: {message_type.value}")
    return envelope_type.model_validate(values)


def _decode_message(raw: str, maximum_size_mb: int) -> object:
    if len(raw.encode("utf-8")) > maximum_size_mb * 1024 * 1024:
        raise GatewayMessageTooLargeError("Agent protocol message exceeds the configured limit.")
    return json.loads(raw)


def _canonical_payload(envelope: EnvelopeBase) -> bytes:
    """Hash stable application content rather than connection-local envelope metadata."""

    payload = envelope.model_dump(mode="json").get("payload")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bearer_secret(value: str | None) -> str:
    if value is None:
        raise AgentAuthenticationFailedError("Agent authentication failed.")
    scheme, separator, secret = value.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not secret.strip():
        raise AgentAuthenticationFailedError("Agent authentication failed.")
    return secret.strip()


async def _close_socket(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.application_state is WebSocketState.DISCONNECTED:
        return
    with suppress(RuntimeError):
        await websocket.close(code=code, reason=reason[:120])


def _safe_reason(error: BaseException) -> str:
    if isinstance(error, PlatformError):
        return error.code
    return "PROTOCOL_MESSAGE_INVALID"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Gateway clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)
