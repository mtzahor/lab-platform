from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from lab_platform.agent_protocol.commands import (
    ArtifactUploadRequestPayload,
    CommandCancelPayload,
    CommandRequestPayload,
    ConfigRefreshRequestPayload,
    DrainAgentPayload,
    EventAckPayload,
    InventoryRefreshRequestPayload,
    ReconciliationRequestPayload,
    ReservationActivatedPayload,
    ReservationReleasedPayload,
)
from lab_platform.agent_protocol.errors import ProtocolMessageInvalidError
from lab_platform.agent_protocol.events import (
    AgentStatusPayload,
    ArtifactCreatedPayload,
    BenchAddedPayload,
    BenchHealthChangedPayload,
    BenchRemovedPayload,
    CommandAcceptedPayload,
    CommandRejectedPayload,
    EventBatchPayload,
    OperationEventPayload,
    ReconciliationReportPayload,
    WorkflowProgressPayload,
)
from lab_platform.agent_protocol.messages import (
    AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES,
    CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES,
    AgentHeartbeatPayload,
    AgentHelloPayload,
    BenchSnapshotPayload,
    MessageType,
    WelcomePayload,
)
from lab_platform.agent_protocol.versioning import (
    ProtocolVersion,
    negotiate_protocol_version,
)
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator


class MessageDirection(StrEnum):
    AGENT_TO_CONTROL_PLANE = "agent_to_control_plane"
    CONTROL_PLANE_TO_AGENT = "control_plane_to_agent"


class EnvelopeBase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)

    protocol_version: str
    message_id: UUID
    message_type: MessageType
    agent_id: UUID
    sent_at: datetime
    correlation_id: UUID | None = None
    sequence_number: int = Field(ge=1, strict=True)

    @field_validator("protocol_version")
    @classmethod
    def validate_protocol_version(cls, value: str) -> str:
        version = str(ProtocolVersion.parse(value))
        negotiate_protocol_version(version)
        return version

    @field_validator("sent_at")
    @classmethod
    def normalize_sent_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sent_at must be timezone-aware")
        return value.astimezone(UTC)


class AgentHelloEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.AGENT_HELLO]
    payload: AgentHelloPayload


class AgentHeartbeatEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.AGENT_HEARTBEAT]
    payload: AgentHeartbeatPayload


class AgentStatusEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.AGENT_STATUS]
    payload: AgentStatusPayload


class BenchSnapshotEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.BENCH_SNAPSHOT]
    payload: BenchSnapshotPayload


class BenchAddedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.BENCH_ADDED]
    payload: BenchAddedPayload


class BenchRemovedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.BENCH_REMOVED]
    payload: BenchRemovedPayload


class BenchHealthChangedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.BENCH_HEALTH_CHANGED]
    payload: BenchHealthChangedPayload


class CommandAcceptedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.COMMAND_ACCEPTED]
    payload: CommandAcceptedPayload


class CommandRejectedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.COMMAND_REJECTED]
    payload: CommandRejectedPayload


class OperationEventEnvelope(EnvelopeBase):
    message_type: Literal[
        MessageType.OPERATION_STARTED,
        MessageType.OPERATION_PROGRESS,
        MessageType.OPERATION_SUCCEEDED,
        MessageType.OPERATION_FAILED,
        MessageType.OPERATION_CANCELLED,
    ]
    payload: OperationEventPayload


class WorkflowProgressEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.WORKFLOW_PROGRESS]
    payload: WorkflowProgressPayload


class ArtifactCreatedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.ARTIFACT_CREATED]
    payload: ArtifactCreatedPayload


class EventBatchEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.EVENT_BATCH]
    payload: EventBatchPayload


class ReconciliationReportEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.RECONCILIATION_REPORT]
    payload: ReconciliationReportPayload


class WelcomeEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.WELCOME]
    payload: WelcomePayload


class EventAckEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.EVENT_ACK]
    payload: EventAckPayload


class CommandRequestEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.COMMAND_REQUEST]
    payload: CommandRequestPayload


class CommandCancelEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.COMMAND_CANCEL]
    payload: CommandCancelPayload


class InventoryRefreshRequestEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.INVENTORY_REFRESH_REQUEST]
    payload: InventoryRefreshRequestPayload


class ReconciliationRequestEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.RECONCILIATION_REQUEST]
    payload: ReconciliationRequestPayload


class ArtifactUploadRequestEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.ARTIFACT_UPLOAD_REQUEST]
    payload: ArtifactUploadRequestPayload


class ConfigRefreshRequestEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.CONFIG_REFRESH_REQUEST]
    payload: ConfigRefreshRequestPayload


class DrainAgentEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.DRAIN_AGENT]
    payload: DrainAgentPayload


class ReservationActivatedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.RESERVATION_ACTIVATED]
    payload: ReservationActivatedPayload


class ReservationReleasedEnvelope(EnvelopeBase):
    message_type: Literal[MessageType.RESERVATION_RELEASED]
    payload: ReservationReleasedPayload


SupportedEnvelope: TypeAlias = Annotated[
    AgentHelloEnvelope
    | AgentHeartbeatEnvelope
    | AgentStatusEnvelope
    | BenchSnapshotEnvelope
    | BenchAddedEnvelope
    | BenchRemovedEnvelope
    | BenchHealthChangedEnvelope
    | CommandAcceptedEnvelope
    | CommandRejectedEnvelope
    | OperationEventEnvelope
    | WorkflowProgressEnvelope
    | ArtifactCreatedEnvelope
    | EventBatchEnvelope
    | ReconciliationReportEnvelope
    | WelcomeEnvelope
    | EventAckEnvelope
    | CommandRequestEnvelope
    | CommandCancelEnvelope
    | InventoryRefreshRequestEnvelope
    | ReconciliationRequestEnvelope
    | ArtifactUploadRequestEnvelope
    | ConfigRefreshRequestEnvelope
    | DrainAgentEnvelope
    | ReservationActivatedEnvelope
    | ReservationReleasedEnvelope,
    Field(discriminator="message_type"),
]

SUPPORTED_MESSAGE_TYPES = frozenset(MessageType)
_ENVELOPE_ADAPTER: TypeAdapter[SupportedEnvelope] = TypeAdapter(SupportedEnvelope)


def parse_envelope(
    value: object,
    *,
    expected_direction: MessageDirection,
) -> SupportedEnvelope:
    """Validate one Phase 5 protocol envelope and its typed payload."""

    if not isinstance(value, dict):
        raise ProtocolMessageInvalidError(
            "Agent protocol message must be a JSON object.",
            received_type=type(value).__name__,
        )
    raw_message_type = value.get("message_type")
    if not isinstance(raw_message_type, str):
        raise ProtocolMessageInvalidError(
            "Agent protocol message type must be a string.",
            received_type=type(raw_message_type).__name__,
        )
    try:
        message_type = MessageType(raw_message_type)
    except ValueError as exc:
        raise ProtocolMessageInvalidError(
            "Unknown Agent protocol message type.",
            message_type=raw_message_type,
        ) from exc
    validate_message_direction(message_type, expected_direction)

    raw_version = value.get("protocol_version")
    if isinstance(raw_version, str):
        negotiate_protocol_version(raw_version)

    try:
        envelope = _ENVELOPE_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ProtocolMessageInvalidError(
            "Agent protocol envelope validation failed.",
            validation_errors=_sanitized_validation_errors(exc),
        ) from exc

    _validate_envelope_coherence(envelope)
    return envelope


def parse_agent_message(value: object) -> SupportedEnvelope:
    return parse_envelope(value, expected_direction=MessageDirection.AGENT_TO_CONTROL_PLANE)


def parse_control_plane_message(value: object) -> SupportedEnvelope:
    return parse_envelope(value, expected_direction=MessageDirection.CONTROL_PLANE_TO_AGENT)


def validate_message_direction(
    message_type: MessageType,
    expected_direction: MessageDirection,
) -> None:
    allowed = (
        AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES
        if expected_direction is MessageDirection.AGENT_TO_CONTROL_PLANE
        else CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES
    )
    if message_type not in allowed:
        raise ProtocolMessageInvalidError(
            "Agent protocol message arrived in the wrong direction.",
            message_type=message_type.value,
            expected_direction=expected_direction.value,
        )


def _validate_envelope_coherence(envelope: SupportedEnvelope) -> None:
    if isinstance(envelope, AgentHelloEnvelope):
        if envelope.payload.protocol_version != envelope.protocol_version:
            raise ProtocolMessageInvalidError(
                "AGENT_HELLO payload and envelope protocol versions differ.",
                envelope_version=envelope.protocol_version,
                payload_version=envelope.payload.protocol_version,
            )
    elif isinstance(envelope, WelcomeEnvelope):
        negotiated = negotiate_protocol_version(envelope.protocol_version)
        if (
            envelope.protocol_version != negotiated
            or envelope.payload.accepted_protocol_version != negotiated
        ):
            raise ProtocolMessageInvalidError(
                "WELCOME envelope and payload must use the negotiated protocol version.",
                expected_version=negotiated,
                envelope_version=envelope.protocol_version,
                accepted_version=envelope.payload.accepted_protocol_version,
            )

    payload_agent_id: UUID | None = None
    if isinstance(envelope, (AgentHeartbeatEnvelope, AgentStatusEnvelope)):
        payload_agent_id = envelope.payload.agent_id
    elif isinstance(envelope, CommandRequestEnvelope):
        payload_agent_id = envelope.payload.command.agent_id
    elif isinstance(envelope, ReconciliationReportEnvelope):
        payload_agent_id = envelope.payload.report.agent_id
    elif isinstance(envelope, EventBatchEnvelope) and any(
        event.agent_id != envelope.agent_id for event in envelope.payload.events
    ):
        raise ProtocolMessageInvalidError(
            "EVENT_BATCH contains an event for a different Agent.",
            envelope_agent_id=str(envelope.agent_id),
        )
    if payload_agent_id is not None and payload_agent_id != envelope.agent_id:
        raise ProtocolMessageInvalidError(
            "Protocol payload and envelope Agent IDs differ.",
            envelope_agent_id=str(envelope.agent_id),
            payload_agent_id=str(payload_agent_id),
        )


def _sanitized_validation_errors(error: ValidationError) -> list[dict[str, object]]:
    return [
        {
            "type": issue["type"],
            "location": [str(part) for part in issue["loc"]],
        }
        for issue in error.errors(include_url=False, include_input=False)
    ]
