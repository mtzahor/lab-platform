from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from lab_platform.agent_protocol import (
    ActorAttributedControlPayload,
    CommandCancelPayload,
    DrainAgentPayload,
    InventoryRefreshRequestPayload,
    MessageType,
    ReconciliationRequestPayload,
    parse_control_plane_message,
)
from lab_platform.models import ActorContext, PrincipalType
from pydantic import ValidationError

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)
AGENT_ID = UUID(int=70_001)
PRINCIPAL_ID = UUID(int=70_002)
SNAPSHOT_ID = UUID(int=70_003)


def _actor(snapshot_id: UUID | None = SNAPSHOT_ID) -> ActorContext:
    return ActorContext(
        principal_id=PRINCIPAL_ID,
        principal_type=PrincipalType.USER,
        display_name="Control Operator",
        organisation_id=UUID(int=70_004),
        authorisation_snapshot_id=snapshot_id,
    )


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        (
            MessageType.COMMAND_CANCEL,
            CommandCancelPayload(
                command_id=UUID(int=70_010),
                reason="operator request",
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.INVENTORY_REFRESH_REQUEST,
            InventoryRefreshRequestPayload(
                request_id=UUID(int=70_011),
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.RECONCILIATION_REQUEST,
            ReconciliationRequestPayload(
                request_id=UUID(int=70_012),
                expected_boot_id=UUID(int=70_013),
                last_control_plane_sequence=9,
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(
                drain=True,
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
    ],
)
def test_principal_initiated_control_payload_round_trips_actor_and_snapshot(
    message_type: MessageType,
    payload: ActorAttributedControlPayload,
) -> None:
    serialized = payload.model_dump(mode="json")
    assert serialized["actor_context"] == _actor().model_dump(mode="json")
    assert serialized["authorisation_snapshot_id"] == str(SNAPSHOT_ID)
    parsed = parse_control_plane_message(
        {
            "protocol_version": "1.0",
            "message_id": str(UUID(int=70_020)),
            "message_type": message_type.value,
            "agent_id": str(AGENT_ID),
            "sent_at": NOW.isoformat(),
            "correlation_id": str(UUID(int=70_021)),
            "sequence_number": 1,
            "payload": serialized,
        }
    )

    assert isinstance(parsed.payload, ActorAttributedControlPayload)
    assert parsed.payload.actor_context == _actor()
    assert parsed.payload.authorisation_snapshot_id == SNAPSHOT_ID


@pytest.mark.parametrize(
    "payload",
    [
        CommandCancelPayload(command_id=UUID(int=70_030)),
        InventoryRefreshRequestPayload(request_id=UUID(int=70_031)),
        ReconciliationRequestPayload(request_id=UUID(int=70_032)),
        DrainAgentPayload(drain=False),
    ],
)
def test_phase5_and_automatic_control_payloads_remain_valid_without_actor(
    payload: ActorAttributedControlPayload,
) -> None:
    assert payload.actor_context is None
    assert payload.authorisation_snapshot_id is None
    serialized = payload.model_dump(mode="json")
    assert "actor_context" not in serialized
    assert "authorisation_snapshot_id" not in serialized


@pytest.mark.parametrize(
    ("payload_type", "values"),
    [
        (CommandCancelPayload, {"command_id": UUID(int=70_040)}),
        (InventoryRefreshRequestPayload, {"request_id": UUID(int=70_041)}),
        (ReconciliationRequestPayload, {"request_id": UUID(int=70_042)}),
        (DrainAgentPayload, {"drain": True}),
    ],
)
def test_control_payload_rejects_mismatched_or_unattributed_snapshot(
    payload_type: type[ActorAttributedControlPayload],
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="snapshot IDs differ"):
        payload_type.model_validate(
            {
                **values,
                "actor_context": _actor(),
                "authorisation_snapshot_id": UUID(int=70_099),
            }
        )
    with pytest.raises(ValidationError, match="snapshot requires actor context"):
        payload_type.model_validate(
            {
                **values,
                "authorisation_snapshot_id": SNAPSHOT_ID,
            }
        )
    with pytest.raises(ValidationError, match="actor context requires authorisation snapshot"):
        payload_type.model_validate(
            {
                **values,
                "actor_context": _actor(snapshot_id=None),
            }
        )
