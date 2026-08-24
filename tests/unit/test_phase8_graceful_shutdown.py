from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

from lab_platform.control_plane.gateway import WS_SERVICE_RESTART, AgentConnectionHub
from starlette.websockets import WebSocket, WebSocketState


class RecordingSocket:
    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.closed: tuple[int, str] | None = None

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED


def test_agent_hub_closes_and_forgets_every_session_during_shutdown() -> None:
    async def scenario() -> None:
        hub = AgentConnectionHub()
        sockets = [RecordingSocket(), RecordingSocket()]
        for socket in sockets:
            await hub.attach(
                agent_id=uuid4(),
                connection_id=uuid4(),
                boot_id=uuid4(),
                websocket=cast(WebSocket, cast(Any, socket)),
            )

        assert await hub.close_all() == 2
        assert all(
            socket.closed == (WS_SERVICE_RESTART, "Control plane is shutting down")
            for socket in sockets
        )
        assert await hub.metrics() == {"connected_agents": 0, "queued_messages": 0}
        assert await hub.close_all() == 0

    asyncio.run(scenario())
