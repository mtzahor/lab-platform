from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from lab_platform.agent_protocol import PROTOCOL_VERSION, MessageType
from lab_platform.control_plane.config import AgentGatewaySettings, DistributedSettings
from lab_platform.control_plane.gateway import WS_PROTOCOL_ERROR, AgentGateway
from lab_platform.control_plane_core.enrollment import AgentEnrollmentService
from lab_platform.control_plane_core.errors import AgentIncompatibleError
from starlette.websockets import WebSocketState

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
AGENT_ID = UUID(int=80_001)


class _UnreachedEnrollmentRepository:
    def __init__(self) -> None:
        self.lookups = 0

    async def get_token_by_hash(self, _token_hash: str) -> None:
        self.lookups += 1
        return None


def test_enrollment_checks_application_compatibility_before_consuming_token() -> None:
    async def scenario() -> None:
        repository = _UnreachedEnrollmentRepository()
        validated: list[tuple[str, str]] = []

        def reject(version: str, protocol: str) -> None:
            validated.append((version, protocol))
            raise AgentIncompatibleError(
                "Agent version unsupported.",
                minimum_supported_version="0.8.0",
            )

        service = AgentEnrollmentService(
            cast(Any, repository),
            compatibility_validator=reject,
        )

        with pytest.raises(AgentIncompatibleError, match="unsupported") as error:
            await service.enroll(
                plaintext_token="lpe_valid-looking-enrollment-secret",
                request_id=UUID(int=80_002),
                agent_version="0.7.9",
                protocol_version="1.7",
            )

        assert validated == [("0.7.9", PROTOCOL_VERSION)]
        assert repository.lookups == 0
        assert error.value.details == {"minimum_supported_version": "0.8.0"}

    asyncio.run(scenario())


class _GatewayEnrollment:
    def __init__(self) -> None:
        self.credentials: list[tuple[UUID, str | None]] = []

    async def authenticate(self, agent_id: UUID, credential: str | None) -> object:
        self.credentials.append((agent_id, credential))
        return SimpleNamespace(agent=SimpleNamespace(id=agent_id))


class _HandshakeWebSocket:
    def __init__(self, hello: dict[str, object]) -> None:
        self.headers = {"authorization": "Bearer agent-secret"}
        self.application_state = WebSocketState.CONNECTED
        self._hello = json.dumps(hello)
        self.accepted = False
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        return self._hello

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED


def _hello(*, version: str) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(UUID(int=80_003)),
        "message_type": MessageType.AGENT_HELLO.value,
        "agent_id": str(AGENT_ID),
        "sent_at": NOW.isoformat(),
        "correlation_id": None,
        "sequence_number": 1,
        "payload": {
            "agent_version": version,
            "protocol_version": PROTOCOL_VERSION,
            "agent_name": "phase8-agent",
            "boot_id": str(UUID(int=80_004)),
            "capabilities": [],
            "last_acknowledged_command_sequence": 0,
        },
    }


def test_gateway_rechecks_compatibility_at_each_authenticated_handshake() -> None:
    async def scenario() -> None:
        enrollment = _GatewayEnrollment()
        validated: list[tuple[str, str]] = []

        def reject(version: str, protocol: str) -> None:
            validated.append((version, protocol))
            raise AgentIncompatibleError(
                "Agent version unsupported.",
                minimum_supported_version="0.8.0",
            )

        gateway = AgentGateway(
            enrollment=cast(Any, enrollment),
            presence=cast(Any, object()),
            hub=cast(Any, object()),
            router=cast(Any, object()),
            gateway_settings=AgentGatewaySettings(),
            distributed_settings=DistributedSettings(),
            compatibility_validator=reject,
            clock=lambda: NOW,
        )
        websocket = _HandshakeWebSocket(_hello(version="0.7.9"))

        await gateway.serve(cast(Any, websocket), AGENT_ID)

        assert enrollment.credentials == [(AGENT_ID, "agent-secret")]
        assert websocket.accepted is True
        assert validated == [("0.7.9", PROTOCOL_VERSION)]
        assert websocket.closed == [(WS_PROTOCOL_ERROR, "AGENT_INCOMPATIBLE")]

    asyncio.run(scenario())
