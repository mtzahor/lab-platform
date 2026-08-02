from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    CommandCancelEnvelope,
    CommandCancelPayload,
    DrainAgentPayload,
    MessageType,
    ProtocolMessageInvalidError,
    SupportedEnvelope,
    parse_agent_message,
)
from lab_platform.control_plane.gateway import (
    WS_AGENT_SUPERSEDED,
    WS_BACKPRESSURE,
    AgentConnectionHub,
    AgentMessageRouter,
    GatewayMessageTooLargeError,
    _bearer_secret,
    _canonical_payload,
    _close_socket,
    _control_plane_envelope,
    _decode_message,
    _safe_reason,
    _utc,
)
from lab_platform.control_plane_core.commands import RemoteCommandService
from lab_platform.control_plane_core.errors import AgentAuthenticationFailedError
from lab_platform.control_plane_core.inventory import InventoryService
from lab_platform.control_plane_core.presence import AgentPresenceService
from lab_platform.core.errors import BenchNotFoundError
from lab_platform.models import RemoteArtifactMetadata
from starlette.websockets import WebSocketState

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
AGENT_ID = UUID(int=401)
BOOT_ID = UUID(int=402)


class FakeWebSocket:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.fail_close = fail_close
        self.closed: list[tuple[int, str]] = []

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self.fail_close:
            raise RuntimeError("already closing")
        self.closed.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED


def test_connection_hub_fences_replacements_and_bounds_outgoing_queue() -> None:
    async def scenario() -> None:
        def clock() -> datetime:
            return NOW

        hub = AgentConnectionHub(outgoing_queue_size=1, clock=clock)
        first_socket = FakeWebSocket()
        first_connection = UUID(int=403)
        await hub.attach(
            agent_id=AGENT_ID,
            connection_id=first_connection,
            boot_id=BOOT_ID,
            websocket=cast(Any, first_socket),
        )
        assert await hub.is_connected(AGENT_ID) is True
        assert await hub.metrics() == {"connected_agents": 1, "queued_messages": 0}

        replacement_socket = FakeWebSocket()
        replacement_connection = UUID(int=404)
        session = await hub.attach(
            agent_id=AGENT_ID,
            connection_id=replacement_connection,
            boot_id=UUID(int=405),
            websocket=cast(Any, replacement_socket),
        )
        assert first_socket.closed == [(WS_AGENT_SUPERSEDED, "Superseded connection")]
        assert await hub.detach(AGENT_ID, first_connection) is False

        envelope = await hub.send(
            AGENT_ID,
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(drain=True),
            correlation_id=UUID(int=406),
            expected_connection_id=replacement_connection,
        )
        assert envelope.sequence_number == 1
        assert session.outgoing.get_nowait() is envelope

        target_command_id = UUID(int=407)
        cancel_receipt = await hub.send_cancel(
            AGENT_ID,
            CommandCancelPayload(command_id=target_command_id, reason="CI cancelled"),
            correlation_id=target_command_id,
        )
        cancel_envelope = session.outgoing.get_nowait()
        assert cancel_receipt.sequence_number == cancel_envelope.sequence_number == 2
        assert cancel_envelope.message_type is MessageType.COMMAND_CANCEL
        assert isinstance(cancel_envelope, CommandCancelEnvelope)
        assert cancel_envelope.payload.command_id == target_command_id

        await hub.send(
            AGENT_ID,
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(drain=False),
        )
        with pytest.raises(RuntimeError, match="superseded"):
            await hub.send(
                AGENT_ID,
                MessageType.DRAIN_AGENT,
                DrainAgentPayload(drain=False),
                expected_connection_id=first_connection,
            )
        with pytest.raises(RuntimeError, match="queue is full"):
            await hub.send(
                AGENT_ID,
                MessageType.DRAIN_AGENT,
                DrainAgentPayload(drain=True),
            )
        assert replacement_socket.closed == [(WS_BACKPRESSURE, "Outgoing queue full")]
        assert await hub.is_connected(AGENT_ID) is False
        with pytest.raises(RuntimeError, match="no active"):
            await hub.require_session(AGENT_ID)
        assert await hub.close_agent(UUID(int=999)) is False
        assert await hub.detach(AGENT_ID, replacement_connection) is True
        assert await hub.detach(AGENT_ID, replacement_connection) is False

    with pytest.raises(ValueError, match="positive"):
        AgentConnectionHub(outgoing_queue_size=0)
    asyncio.run(scenario())


def test_gateway_transport_helpers_validate_secrets_sizes_types_and_timestamps() -> None:
    assert _bearer_secret("bEaReR   secret-value  ") == "secret-value"
    for value in (None, "", "Basic value", "Bearer   "):
        with pytest.raises(AgentAuthenticationFailedError):
            _bearer_secret(value)

    assert _decode_message('{"ok": true}', 1) == {"ok": True}
    with pytest.raises(json.JSONDecodeError):
        _decode_message("not-json", 1)
    with pytest.raises(GatewayMessageTooLargeError):
        _decode_message('"' + "x" * (1024 * 1024) + '"', 1)

    envelope = _control_plane_envelope(
        MessageType.DRAIN_AGENT,
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": UUID(int=410),
            "message_type": MessageType.DRAIN_AGENT,
            "agent_id": AGENT_ID,
            "sent_at": NOW,
            "correlation_id": None,
            "sequence_number": 1,
            "payload": DrainAgentPayload(drain=True),
        },
    )
    assert json.loads(_canonical_payload(envelope)) == {"deadline": None, "drain": True}
    with pytest.raises(ValueError, match="Unsupported or invalid"):
        _control_plane_envelope(
            MessageType.DRAIN_AGENT,
            {
                "protocol_version": PROTOCOL_VERSION,
                "message_id": UUID(int=411),
                "message_type": MessageType.DRAIN_AGENT,
                "agent_id": AGENT_ID,
                "sent_at": NOW,
                "correlation_id": None,
                "sequence_number": 1,
                "payload": object(),
            },
        )
    with pytest.raises(ValueError, match="Unsupported or invalid"):
        _control_plane_envelope(MessageType.AGENT_HEARTBEAT, {})

    assert _safe_reason(BenchNotFoundError("missing")) == "BENCH_NOT_FOUND"
    assert _safe_reason(ProtocolMessageInvalidError("invalid")) == "PROTOCOL_MESSAGE_INVALID"
    assert _safe_reason(ValueError("sensitive detail")) == "PROTOCOL_MESSAGE_INVALID"
    assert _utc(NOW) == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        _utc(NOW.replace(tzinfo=None))


def test_close_socket_is_idempotent_bounded_and_suppresses_close_races() -> None:
    async def scenario() -> None:
        socket = FakeWebSocket()
        await _close_socket(cast(Any, socket), 4400, "x" * 200)
        assert socket.closed == [(4400, "x" * 120)]
        await _close_socket(cast(Any, socket), 4401, "ignored")
        assert len(socket.closed) == 1

        raced = FakeWebSocket(fail_close=True)
        await _close_socket(cast(Any, raced), 4500, "failure")
        assert raced.closed == []

    asyncio.run(scenario())


class RecordingPresence:
    def __init__(self) -> None:
        self.heartbeats: list[dict[str, object]] = []

    async def record_heartbeat(self, _agent_id: UUID, **kwargs: object) -> None:
        self.heartbeats.append(kwargs)

    async def get_agent(self, agent_id: UUID) -> object:
        return {"id": agent_id}


class RecordingInventory:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reconcile_snapshot(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("snapshot")

    async def apply_bench_added(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("added")

    async def apply_bench_removed(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("removed")

    async def apply_bench_health_changed(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("health")


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def accepted(self, _agent_id: UUID, payload: object) -> None:
        self.calls.append(("accepted", payload))

    async def rejected(self, _agent_id: UUID, payload: object) -> None:
        self.calls.append(("rejected", payload))

    async def operation_event(
        self,
        _agent_id: UUID,
        message_type: MessageType,
        payload: object,
    ) -> None:
        self.calls.append((message_type.value, payload))

    async def mark_unknown(self, _agent_id: UUID, *, observed_at: datetime) -> int:
        self.calls.append(("unknown", observed_at))
        return 2


class RecordingArtifacts:
    def __init__(self) -> None:
        self.registered: list[RemoteArtifactMetadata] = []

    async def register_remote_artifact(
        self,
        artifact: RemoteArtifactMetadata,
    ) -> RemoteArtifactMetadata:
        stored = artifact.model_copy(update={"id": UUID(int=490)})
        self.registered.append(stored)
        return stored


class RecordingOptionalHandlers:
    def __init__(self) -> None:
        self.uploads: list[tuple[UUID, UUID]] = []
        self.confirmations: list[object] = []
        self.reconciliations: list[tuple[UUID, object]] = []
        self.disconnects: list[tuple[UUID, UUID]] = []

    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> None:
        self.uploads.append((agent_id, artifact_id))

    async def confirm(self, payload: object) -> bool:
        self.confirmations.append(payload)
        return True

    async def reconcile(self, report_id: UUID, report: object) -> None:
        self.reconciliations.append((report_id, report))

    async def mark_agent_disconnected(
        self,
        agent_id: UUID,
        *,
        disconnect_id: UUID,
    ) -> None:
        self.disconnects.append((agent_id, disconnect_id))


def _incoming(
    message_type: MessageType,
    sequence_number: int,
    payload: dict[str, object],
) -> SupportedEnvelope:
    return parse_agent_message(
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": str(UUID(int=450 + sequence_number)),
            "message_type": message_type.value,
            "agent_id": str(AGENT_ID),
            "sent_at": NOW.isoformat(),
            "correlation_id": None,
            "sequence_number": sequence_number,
            "payload": payload,
        }
    )


def _bench_payload() -> dict[str, object]:
    return {
        "local_bench_id": "bench-01",
        "name": "Router bench",
        "backend_id": "simlab",
        "kind": "simulated",
        "target_type": "esp32",
        "connectivity": "online",
        "health": "healthy",
        "capabilities": ["reset"],
        "labels": {"board": "esp32"},
    }


def test_message_router_dispatches_inventory_commands_receipts_artifacts_and_reconciliation() -> (
    None
):
    async def scenario() -> None:
        presence = RecordingPresence()
        inventory = RecordingInventory()
        commands = RecordingCommands()
        artifacts = RecordingArtifacts()
        handlers = RecordingOptionalHandlers()
        router = AgentMessageRouter(
            presence=cast(AgentPresenceService, presence),
            inventory=cast(InventoryService, inventory),
            commands=cast(RemoteCommandService, commands),
            artifacts=cast(Any, artifacts),
            artifact_uploads=cast(Any, handlers),
            lease_receipts=cast(Any, handlers),
            reservation_disconnects=cast(Any, handlers),
            reconciliation=cast(Any, handlers),
        )
        reservation_id = UUID(int=470)
        command_id = UUID(int=471)
        local_artifact_id = UUID(int=472)
        messages = [
            _incoming(
                MessageType.BENCH_SNAPSHOT,
                1,
                {
                    "boot_id": str(BOOT_ID),
                    "generated_at": NOW.isoformat(),
                    "benches": [_bench_payload()],
                },
            ),
            _incoming(
                MessageType.BENCH_ADDED,
                2,
                {
                    "boot_id": str(BOOT_ID),
                    "bench": _bench_payload(),
                    "changed_at": NOW.isoformat(),
                },
            ),
            _incoming(
                MessageType.BENCH_REMOVED,
                3,
                {
                    "boot_id": str(BOOT_ID),
                    "local_bench_id": "bench-01",
                    "changed_at": NOW.isoformat(),
                },
            ),
            _incoming(
                MessageType.BENCH_HEALTH_CHANGED,
                4,
                {
                    "boot_id": str(BOOT_ID),
                    "local_bench_id": "bench-01",
                    "connectivity": "degraded",
                    "health": "warning",
                    "changed_at": NOW.isoformat(),
                },
            ),
            _incoming(
                MessageType.COMMAND_ACCEPTED,
                5,
                {"command_id": str(command_id), "accepted_at": NOW.isoformat()},
            ),
            _incoming(
                MessageType.COMMAND_REJECTED,
                6,
                {
                    "command_id": str(UUID(int=473)),
                    "rejected_at": NOW.isoformat(),
                    "error_code": "SAFETY_REJECTED",
                    "error_message": "Local policy rejected the command.",
                },
            ),
            _incoming(
                MessageType.OPERATION_SUCCEEDED,
                7,
                {
                    "command_id": str(command_id),
                    "occurred_at": NOW.isoformat(),
                    "progress": 100,
                    "result": {"ok": True},
                },
            ),
            _incoming(
                MessageType.AGENT_STATUS,
                8,
                {
                    "agent_id": str(AGENT_ID),
                    "boot_id": str(BOOT_ID),
                    "status": "ONLINE",
                    "changed_at": NOW.isoformat(),
                    "lease_application": {
                        "reservation_id": str(reservation_id),
                        "agent_id": str(AGENT_ID),
                        "bench_id": "router-agent/bench-01",
                        "lease_version": 2,
                        "confirmed_at": NOW.isoformat(),
                    },
                },
            ),
            _incoming(
                MessageType.ARTIFACT_CREATED,
                9,
                {
                    "artifact": {
                        "agent_id": str(AGENT_ID),
                        "local_artifact_id": str(local_artifact_id),
                        "command_id": str(command_id),
                        "name": "results.xml",
                        "artifact_type": "junit",
                        "content_type": "application/xml",
                        "size_bytes": 2,
                        "sha256": "0" * 64,
                        "created_at": NOW.isoformat(),
                    }
                },
            ),
            _incoming(
                MessageType.RECONCILIATION_REPORT,
                10,
                {
                    "report": {
                        "agent_id": str(AGENT_ID),
                        "boot_id": str(BOOT_ID),
                        "generated_at": NOW.isoformat(),
                        "active_commands": [],
                        "recent_commands": [],
                        "local_reservation_leases": [],
                        "bench_snapshots": [],
                        "buffered_event_count": 0,
                    }
                },
            ),
        ]
        connection_id = UUID(int=480)
        for envelope in messages:
            await router.handle(
                envelope,
                connection_id=connection_id,
                boot_id=BOOT_ID,
                observed_at=NOW + timedelta(seconds=1),
                observed_monotonic=200.0,
            )

        assert inventory.calls == ["snapshot", "added", "removed", "health"]
        assert [entry[0] for entry in commands.calls] == [
            "accepted",
            "rejected",
            MessageType.OPERATION_SUCCEEDED.value,
        ]
        assert len(handlers.confirmations) == 1
        assert artifacts.registered[0].local_artifact_id == local_artifact_id
        assert handlers.uploads == [(AGENT_ID, UUID(int=490))]
        assert handlers.reconciliations[0][0] == messages[-1].message_id
        assert len(presence.heartbeats) == len(messages)
        assert all(item["observed_clock_offset_seconds"] is None for item in presence.heartbeats)

        disconnect_id = UUID(int=491)
        assert (
            await router.mark_agent_unknown(
                AGENT_ID,
                observed_at=NOW,
                disconnect_id=disconnect_id,
            )
            == 2
        )
        assert handlers.disconnects == [(AGENT_ID, disconnect_id)]

    asyncio.run(scenario())
