from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.control_plane import cli as control_plane_cli
from lab_platform.control_plane.server import ControlPlaneHttpServer


def _config(tmp_path: Path, *, tls: bool = False) -> ControlPlaneConfig:
    control_plane: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8443,
        "public_url": "https://127.0.0.1:8443" if tls else "http://127.0.0.1:8443",
    }
    if tls:
        control_plane.update(
            {
                "tls_certificate_path": tmp_path / "server.crt",
                "tls_private_key_path": tmp_path / "server.key",
            }
        )
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": control_plane,
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "development": {"allow_insecure_agent_transport": not tls},
        }
    )


def test_control_plane_once_initializes_and_reports_metrics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "control-plane.yaml"
    config_path.write_text(
        "control_plane:\n"
        "  host: 127.0.0.1\n"
        "  port: 8443\n"
        "  public_url: http://127.0.0.1:8443\n"
        f"database:\n  url: sqlite:///{tmp_path / 'once.db'}\n"
        f"artifacts:\n  directory: {tmp_path / 'once-artifacts'}\n"
        "development:\n  allow_insecure_agent_transport: true\n",
        encoding="utf-8",
    )

    assert control_plane_cli.main(["--config", str(config_path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "Configuration loaded" in output
    assert "Database migrations applied" in output
    assert "0 Agents online" in output
    assert "0 benches in unified inventory" in output


def test_control_plane_migrate_command_applies_schema_without_starting_services(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "control-plane.yaml"
    database_path = tmp_path / "migrated.db"
    config_path.write_text(
        "control_plane:\n"
        "  host: 127.0.0.1\n"
        "  port: 8443\n"
        "  public_url: http://127.0.0.1:8443\n"
        f"database:\n  url: sqlite:///{database_path}\n"
        f"artifacts:\n  directory: {tmp_path / 'artifacts'}\n"
        "development:\n  allow_insecure_agent_transport: true\n",
        encoding="utf-8",
    )

    assert control_plane_cli.main(["migrate", "--config", str(config_path)]) == 0
    assert database_path.exists()
    output = capsys.readouterr().out
    assert "Database migrations applied" in output
    assert "Agents online" not in output


def test_control_plane_main_serving_path_handles_interrupt_and_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, tls=True)
    events: list[Any] = []

    class FakeServer:
        def __init__(self, runtime: ControlPlaneRuntime, *, host: str, port: int) -> None:
            events.append((runtime.config, host, port))

        def serve_forever(self) -> None:
            events.append("serve")
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            events.append("shutdown")

    monkeypatch.setattr(control_plane_cli, "load_control_plane_config", lambda _path: config)
    monkeypatch.setattr(control_plane_cli, "ControlPlaneHttpServer", FakeServer)

    assert control_plane_cli.main(["--host", "0.0.0.0", "--port", "9443"]) == 0
    assert events[0][1:] == ("0.0.0.0", 9443)
    assert events[1:] == ["serve", "shutdown"]
    output = capsys.readouterr().out
    assert "Listening on https://127.0.0.1:8443" in output
    assert "Shutting down" in output
    assert control_plane_cli._with_bind_host(config, None) is config


def test_control_plane_http_server_binds_runs_and_closes_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self, port: int) -> None:
            self.port = port
            self.closed = False

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", self.port

        def shutdown(self, _direction: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    with pytest.raises(ValueError, match="must match the validated configuration host"):
        ControlPlaneHttpServer(
            ControlPlaneRuntime(_config(tmp_path / "mismatched")),
            host="0.0.0.0",
            port=0,
        )

    sockets = [FakeSocket(18080), FakeSocket(18081)]
    monkeypatch.setattr(
        "lab_platform.control_plane.server.uvicorn.Config.bind_socket",
        lambda _config: sockets.pop(0),
    )
    runtime = ControlPlaneRuntime(_config(tmp_path))
    server = ControlPlaneHttpServer(runtime, host="127.0.0.1", port=0)
    assert server.host == "127.0.0.1"
    assert server.port == 18080
    server.shutdown()

    second_runtime = ControlPlaneRuntime(_config(tmp_path / "second"))
    tls_server = ControlPlaneHttpServer(second_runtime, host="127.0.0.1", port=0)
    observed: dict[str, Any] = {}

    def run(*, sockets: list[object]) -> None:
        observed["sockets"] = sockets

    monkeypatch.setattr(tls_server._server, "run", run)
    tls_server.serve_forever()
    assert observed["sockets"] == [tls_server._socket]
    tls_server.shutdown()


def test_with_bind_host_rejects_invalid_serialized_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: Any = SimpleNamespace(model_dump=lambda **_kwargs: {"control_plane": "bad"})
    with pytest.raises(TypeError, match="serialize as a mapping"):
        control_plane_cli._with_bind_host(malformed, "127.0.0.1")
