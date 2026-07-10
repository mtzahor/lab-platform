from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from lab_platform.agent import AgentHttpServer, create_agent
from lab_platform.cli.client import AgentClient
from lab_platform.cli.main import main as cli_main


def test_http_api_and_cli_read_models(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  log_level: ERROR\nplugins:\n  - power\n  - serial\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  benches: 2\n",
        encoding="utf-8",
    )
    agent = create_agent(tmp_path)
    asyncio.run(agent.start())
    server = AgentHttpServer(agent, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"
    client = AgentClient(base_url)

    try:
        assert client.get("health") == {
            "status": "healthy",
            "version": "0.1.0-alpha",
            "benches": 2,
            "plugins": 2,
        }
        assert client.get("version") == {"version": "0.1.0-alpha"}
        plugins = client.get("plugins")
        assert isinstance(plugins, list)
        assert [plugin["name"] for plugin in plugins] == ["power", "serial"]
        benches = client.get("benches")
        assert isinstance(benches, list)
        assert [bench["name"] for bench in benches] == ["bench-01", "bench-02"]

        with pytest.raises(HTTPError) as error:
            urlopen(f"{base_url}/missing")
        assert error.value.code == 404
        assert json.loads(error.value.read()) == {"error": "not found"}

        assert cli_main(["--url", base_url, "health"]) == 0
        assert '"status": "healthy"' in capsys.readouterr().out
        assert cli_main(["--url", base_url, "benches"]) == 0
        bench_output = capsys.readouterr().out
        assert "NAME" in bench_output
        assert "bench-01" in bench_output
        assert "Power, Serial, Firmware" in bench_output
        assert cli_main(["--url", base_url, "plugins"]) == 0
        assert "power" in capsys.readouterr().out
    finally:
        server.shutdown()
        thread.join(timeout=2)
        asyncio.run(agent.shutdown())

    assert not thread.is_alive()


def test_cli_local_commands_and_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "labctl 0.1.0-alpha"

    assert cli_main(["config", "validate", "--config-dir", str(tmp_path)]) == 0
    assert "Configuration valid" in capsys.readouterr().out

    (tmp_path / "agent.yaml").write_text("agent:\n  port: nope\n", encoding="utf-8")
    assert cli_main(["config", "validate", "--config-dir", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err

    assert cli_main(["--url", "http://127.0.0.1:1", "health"]) == 1
    assert "Could not read" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command, payload",
    [
        ("health", []),
        ("benches", {}),
        ("plugins", {}),
    ],
)
def test_cli_rejects_invalid_agent_payloads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    payload: object,
) -> None:
    monkeypatch.setattr(AgentClient, "get", lambda self, path: payload)

    assert cli_main([command]) == 1
    assert "invalid" in capsys.readouterr().err


def test_cli_rejects_invalid_capability_payloads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        AgentClient,
        "get",
        lambda self, path: [{"name": "bad", "capabilities": "not-a-list"}],
    )

    assert cli_main(["benches"]) == 1
    assert "invalid bench capabilities" in capsys.readouterr().err
    assert cli_main(["plugins"]) == 1
    assert "invalid plugin capabilities" in capsys.readouterr().err
