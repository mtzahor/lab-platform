from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    ArtifactCreatedPayload,
    CommandAcceptedPayload,
    CommandRejectedPayload,
    EventAckEnvelope,
    EventAckPayload,
    EventBatchEnvelope,
    EventBatchPayload,
    IncomingSequenceTracker,
    MessageType,
    OperationEventPayload,
    ProtocolMessageInvalidError,
)
from lab_platform.control_plane.config import AgentGatewaySettings, DistributedSettings
from lab_platform.control_plane.gateway import (
    AgentGateway,
    AgentMessageRouter,
    _AgentSocketSession,
    _control_plane_envelope,
)
from lab_platform.control_plane_core.commands import RemoteCommandService
from lab_platform.control_plane_core.inventory import InventoryService
from lab_platform.control_plane_core.presence import AgentPresenceService
from lab_platform.models import (
    BufferedAgentEvent,
    BufferedEventPriority,
    RemoteArtifactMetadata,
)
from pydantic import BaseModel

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
AGENT_ID = UUID(int=1)


class FakeRemoteCommands:
    def __init__(self) -> None:
        self.accepted_events: list[tuple[UUID, CommandAcceptedPayload]] = []
        self.rejected_events: list[tuple[UUID, CommandRejectedPayload]] = []
        self.operation_events: list[tuple[UUID, MessageType, OperationEventPayload]] = []

    async def accepted(self, agent_id: UUID, payload: CommandAcceptedPayload) -> None:
        self.accepted_events.append((agent_id, payload))

    async def rejected(self, agent_id: UUID, payload: CommandRejectedPayload) -> None:
        self.rejected_events.append((agent_id, payload))

    async def operation_event(
        self,
        agent_id: UUID,
        message_type: MessageType,
        payload: OperationEventPayload,
    ) -> None:
        self.operation_events.append((agent_id, message_type, payload))


class ReaderComplete(Exception):
    pass


class OneMessageWebSocket:
    def __init__(self, message: str) -> None:
        self._message = message
        self._received = False

    async def receive_text(self) -> str:
        if self._received:
            raise ReaderComplete
        self._received = True
        return self._message


class OrderedJournal:
    def __init__(self, order: list[str], *, is_new: bool = True) -> None:
        self._order = order
        self._is_new = is_new

    async def record_protocol_message(self, _record: object) -> bool:
        self._order.append("record")
        return self._is_new

    async def finalize_protocol_message(self, _record: object) -> None:
        self._order.append("finalize")


class OrderedRouter:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def handle(self, envelope: object, **_kwargs: object) -> None:
        assert isinstance(envelope, EventBatchEnvelope)
        self._order.append("route")


class OrderedHub:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.sent: list[tuple[UUID, MessageType, object, UUID | None, UUID | None]] = []

    async def send(
        self,
        agent_id: UUID,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None = None,
        expected_connection_id: UUID | None = None,
    ) -> object:
        self._order.append("ack")
        self.sent.append(
            (
                agent_id,
                message_type,
                payload,
                correlation_id,
                expected_connection_id,
            )
        )
        return payload


def _event(
    sequence_number: int,
    message_type: MessageType | str,
    payload: BaseModel | None,
) -> BufferedAgentEvent:
    document = payload.model_dump(mode="json") if payload is not None else {}
    return BufferedAgentEvent(
        id=UUID(int=100 + sequence_number),
        agent_id=AGENT_ID,
        sequence_number=sequence_number,
        event_type=str(message_type),
        payload=document,
        priority=BufferedEventPriority.STATE,
        created_at=NOW,
    )


def test_gateway_routes_buffered_command_acknowledgements_before_operation_events() -> None:
    async def scenario() -> None:
        command_id = UUID(int=10)
        accepted = CommandAcceptedPayload(
            command_id=command_id,
            accepted_at=NOW,
        )
        rejected = CommandRejectedPayload(
            command_id=UUID(int=11),
            rejected_at=NOW,
            error_code="REMOTE_COMMAND_REJECTED",
            error_message="Local safety policy rejected the command.",
        )
        operation = OperationEventPayload(
            command_id=command_id,
            occurred_at=NOW,
            progress=0,
            message="Command execution started.",
        )
        envelope = EventBatchEnvelope(
            protocol_version=PROTOCOL_VERSION,
            message_id=UUID(int=200),
            message_type=MessageType.EVENT_BATCH,
            agent_id=AGENT_ID,
            sent_at=NOW,
            sequence_number=2,
            payload=EventBatchPayload(
                events=(
                    _event(1, MessageType.COMMAND_ACCEPTED, accepted),
                    _event(2, MessageType.COMMAND_REJECTED, rejected),
                    _event(3, MessageType.OPERATION_STARTED, operation),
                    _event(4, "FUTURE_OPTIONAL_EVENT", None),
                )
            ),
        )
        commands = FakeRemoteCommands()
        router = AgentMessageRouter(
            presence=cast(AgentPresenceService, object()),
            inventory=cast(InventoryService, object()),
            commands=cast(RemoteCommandService, commands),
        )

        await router._handle_event_batch(envelope)

        assert commands.accepted_events == [(AGENT_ID, accepted)]
        assert commands.rejected_events == [(AGENT_ID, rejected)]
        assert commands.operation_events == [(AGENT_ID, MessageType.OPERATION_STARTED, operation)]

    asyncio.run(scenario())


def test_gateway_builds_a_typed_event_ack_envelope() -> None:
    batch_message_id = UUID(int=250)
    envelope = _control_plane_envelope(
        MessageType.EVENT_ACK,
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": UUID(int=251),
            "message_type": MessageType.EVENT_ACK,
            "agent_id": AGENT_ID,
            "sent_at": NOW,
            "correlation_id": batch_message_id,
            "sequence_number": 3,
            "payload": EventAckPayload(
                batch_message_id=batch_message_id,
                acknowledged_event_sequence=7,
            ),
        },
    )

    assert isinstance(envelope, EventAckEnvelope)
    assert envelope.payload.batch_message_id == batch_message_id


def test_gateway_rejects_artifact_identity_before_persisting_metadata() -> None:
    class ArtifactSink:
        def __init__(self) -> None:
            self.registered: list[RemoteArtifactMetadata] = []

        async def register_remote_artifact(
            self,
            artifact: RemoteArtifactMetadata,
        ) -> RemoteArtifactMetadata:
            self.registered.append(artifact)
            return artifact

    async def scenario() -> None:
        sink = ArtifactSink()
        router = AgentMessageRouter(
            presence=cast(AgentPresenceService, object()),
            inventory=cast(InventoryService, object()),
            commands=cast(RemoteCommandService, object()),
            artifacts=cast(Any, sink),
        )
        payload = ArtifactCreatedPayload(
            artifact=RemoteArtifactMetadata(
                agent_id=UUID(int=2),
                local_artifact_id=UUID(int=3),
                command_id=UUID(int=4),
                name="result.xml",
                artifact_type="junit",
                size_bytes=1,
                sha256="0" * 64,
                created_at=NOW,
            )
        )

        with pytest.raises(ProtocolMessageInvalidError, match="authenticated stream"):
            await router._handle_artifact_created(AGENT_ID, payload)
        assert sink.registered == []

    asyncio.run(scenario())


@pytest.mark.parametrize("is_new", [True, False], ids=["first-delivery", "replay"])
def test_gateway_acknowledges_event_batch_only_after_routing_and_finalizing(
    is_new: bool,
) -> None:
    async def scenario() -> None:
        batch = EventBatchEnvelope(
            protocol_version=PROTOCOL_VERSION,
            message_id=UUID(int=300),
            message_type=MessageType.EVENT_BATCH,
            agent_id=AGENT_ID,
            sent_at=NOW,
            sequence_number=2,
            payload=EventBatchPayload(events=(_event(7, "FUTURE_OPTIONAL_EVENT", None),)),
        )
        order: list[str] = []
        hub = OrderedHub(order)
        connection_id = UUID(int=301)
        websocket = OneMessageWebSocket(batch.model_dump_json())
        session = _AgentSocketSession(
            agent_id=AGENT_ID,
            connection_id=connection_id,
            boot_id=UUID(int=302),
            websocket=cast(Any, websocket),
            outgoing=asyncio.Queue(),
        )
        gateway = AgentGateway(
            enrollment=cast(Any, object()),
            presence=cast(Any, object()),
            hub=cast(Any, hub),
            router=cast(Any, OrderedRouter(order)),
            gateway_settings=AgentGatewaySettings(),
            distributed_settings=DistributedSettings(),
            protocol_journal=cast(Any, OrderedJournal(order, is_new=is_new)),
            clock=lambda: NOW,
            monotonic=lambda: 100.0,
        )

        with pytest.raises(ReaderComplete):
            await gateway._reader(
                cast(Any, websocket),
                session,
                IncomingSequenceTracker(expected_sequence=2),
            )

        assert order == ["record", "route", "finalize", "ack"]
        assert hub.sent == [
            (
                AGENT_ID,
                MessageType.EVENT_ACK,
                EventAckPayload(
                    batch_message_id=batch.message_id,
                    acknowledged_event_sequence=7,
                ),
                batch.message_id,
                connection_id,
            )
        ]

    asyncio.run(scenario())
