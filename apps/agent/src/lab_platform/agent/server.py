from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from lab_platform.agent.runtime import LabAgent


class AgentHttpServer:
    def __init__(self, agent: LabAgent, host: str, port: int) -> None:
        self._server = ThreadingHTTPServer(
            (host, port),
            _make_handler(agent),
        )
        self._serving = threading.Event()

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._serving.set()
        try:
            self._server.serve_forever()
        finally:
            self._serving.clear()

    def shutdown(self) -> None:
        if self._serving.is_set():
            self._server.shutdown()
        self._server.server_close()


def _make_handler(agent: LabAgent) -> type[BaseHTTPRequestHandler]:
    class AgentRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(agent.health_payload())
                return
            if path == "/version":
                self._send_json({"version": agent.health_payload()["version"]})
                return
            if path == "/plugins":
                self._send_json([plugin.model_dump(mode="json") for plugin in agent.plugins()])
                return
            if path == "/benches":
                self._send_json([bench.model_dump(mode="json") for bench in agent.benches()])
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(
            self,
            payload: object,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return AgentRequestHandler
