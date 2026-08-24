from __future__ import annotations

import socket
import threading
from contextlib import suppress

import uvicorn
from lab_platform.control_plane.api import create_app
from lab_platform.control_plane.runtime import ControlPlaneRuntime


class ControlPlaneHttpServer:
    def __init__(
        self,
        runtime: ControlPlaneRuntime,
        *,
        host: str,
        port: int,
    ) -> None:
        settings = runtime.config.control_plane
        if host != settings.host:
            raise ValueError(
                "Control-plane server bind host must match the validated configuration host"
            )
        self._config = uvicorn.Config(
            create_app(runtime),
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            ssl_certfile=(
                str(settings.tls_certificate_path)
                if settings.tls_certificate_path is not None
                else None
            ),
            ssl_keyfile=(
                str(settings.tls_private_key_path)
                if settings.tls_private_key_path is not None
                else None
            ),
            proxy_headers=runtime.config.proxy.enabled,
            forwarded_allow_ips=(
                ",".join(runtime.config.proxy.trusted_networks)
                if runtime.config.proxy.enabled
                else ""
            ),
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
            return
        with suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
