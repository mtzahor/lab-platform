from __future__ import annotations

import socket
import threading
from contextlib import suppress

import uvicorn
from lab_platform.agent.api import create_app
from lab_platform.agent.runtime import LabAgent


class AgentHttpServer:
    """Small Uvicorn wrapper retained for CLI startup and real-server tests."""

    def __init__(self, agent: LabAgent, host: str, port: int) -> None:
        self._config = uvicorn.Config(
            create_app(agent),
            host=host,
            port=port,
            log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._socket = self._config.bind_socket()
        address = self._socket.getsockname()
        self._host = str(address[0])
        self._port = int(address[1])
        self._serving = threading.Event()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def serve_forever(self) -> None:
        self._serving.set()
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._serving.clear()

    def shutdown(self) -> None:
        if self._serving.is_set():
            self._server.should_exit = True
        else:
            with suppress(OSError):
                self._socket.shutdown(socket.SHUT_RDWR)
            self._socket.close()
