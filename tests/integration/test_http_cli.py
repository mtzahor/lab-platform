from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest
from lab_platform.agent import AgentHttpServer, LabAgent, create_agent
from lab_platform.cli.client import AgentApiError, AgentClient
from lab_platform.cli.main import main as cli_main


def _start_agent(root: Path) -> tuple[LabAgent, AgentHttpServer, threading.Thread, str]:
    (root / "agent.yaml").write_text(
        "agent:\n  log_level: ERROR\nplugins: []\n",
        encoding="utf-8",
    )
    (root / "simlab.yaml").write_text(
        "simlab:\n  benches: 2\n  speed_multiplier: 200\n  flash_duration_seconds: 1\n",
        encoding="utf-8",
    )
    agent = create_agent(root)
    asyncio.run(agent.start())
    server = AgentHttpServer(agent, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.host}:{server.port}"
    client = AgentClient(base_url)
    for _ in range(100):
        try:
            client.get("/api/v1/health")
            break
        except Exception:
            time.sleep(0.01)
    return agent, server, thread, base_url


def test_complete_cli_workflow_uses_real_versioned_http_api(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent, server, thread, base_url = _start_agent(tmp_path)
    firmware = tmp_path / "demo.bin"
    firmware.write_bytes(b"phase-1-firmware")
    prefix = ["--server", base_url]
    try:
        assert cli_main([*prefix, "version"]) == 0
        assert capsys.readouterr().out.strip() == "labctl 0.5.0-alpha"

        assert cli_main([*prefix, "health", "--output", "json"]) == 0
        health = json.loads(capsys.readouterr().out)
        assert health["backend"] == "simlab"
        assert health["benches"] == {"online": 2, "total": 2}

        assert cli_main([*prefix, "bench", "list"]) == 0
        listing = capsys.readouterr().out
        assert "bench-01" in listing
        assert "AVAILABLE" in listing.upper()

        assert cli_main([*prefix, "bench", "reserve", "bench-01", "--owner", "michael"]) == 0
        assert "Reserved bench-01" in capsys.readouterr().out

        assert cli_main([*prefix, "bench", "show", "bench-01"]) == 0
        assert "michael" in capsys.readouterr().out

        assert cli_main([*prefix, "bench", "probe", "bench-01", "--owner", "michael"]) == 0
        assert "SimLab" in capsys.readouterr().out

        assert cli_main([*prefix, "bench", "power-cycle", "bench-01", "--owner", "michael"]) == 0
        output = capsys.readouterr().out
        operation_id = re.search(r"Operation created: ([0-9a-f-]+)", output)
        assert operation_id is not None
        power_operation = operation_id.group(1)

        assert (
            cli_main(
                [
                    *prefix,
                    "operation",
                    "watch",
                    power_operation,
                    "--interval",
                    "0.01",
                ]
            )
            == 0
        )
        assert "Status: Succeeded" in capsys.readouterr().out

        assert (
            cli_main(
                [
                    *prefix,
                    "bench",
                    "flash",
                    "bench-01",
                    str(firmware),
                    "--owner",
                    "michael",
                    "--version",
                    "2.0.0",
                ]
            )
            == 0
        )
        flash_output = capsys.readouterr().out
        assert "SHA-256:" in flash_output
        flash_id = re.search(r"Operation created: ([0-9a-f-]+)", flash_output)
        assert flash_id is not None

        assert (
            cli_main(
                [
                    *prefix,
                    "operation",
                    "watch",
                    flash_id.group(1),
                    "--interval",
                    "0.01",
                ]
            )
            == 0
        )
        assert "Firmware 2.0.0 installed" in capsys.readouterr().out

        assert (
            cli_main(
                [
                    *prefix,
                    "bench",
                    "serial",
                    "read",
                    "bench-01",
                    "--owner",
                    "michael",
                    "--until",
                    "READY",
                ]
            )
            == 0
        )
        serial_output = capsys.readouterr().out
        assert "FIRMWARE_VERSION=2.0.0" in serial_output
        assert "READY" in serial_output

        assert cli_main([*prefix, "bench", "reset", "bench-01", "--owner", "michael"]) == 0
        reset_id = re.search(r"Operation created: ([0-9a-f-]+)", capsys.readouterr().out)
        assert reset_id is not None
        assert (
            cli_main(
                [
                    *prefix,
                    "operation",
                    "watch",
                    reset_id.group(1),
                    "--interval",
                    "0.01",
                ]
            )
            == 0
        )
        assert "Status: Succeeded" in capsys.readouterr().out

        assert cli_main([*prefix, "operation", "list"]) == 0
        assert "Flash Firmware" in capsys.readouterr().out
        assert cli_main([*prefix, "event", "list", "--bench-id", "bench-01"]) == 0
        assert "FLASH_COMPLETED" in capsys.readouterr().out

        assert cli_main([*prefix, "bench", "release", "bench-01", "--owner", "michael"]) == 0
        assert "Released bench-01" in capsys.readouterr().out
    finally:
        server.shutdown()
        thread.join(timeout=3)
        asyncio.run(agent.shutdown())
    assert not thread.is_alive()


def test_http_errors_and_cli_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent, server, thread, base_url = _start_agent(tmp_path)
    client = AgentClient(base_url)
    try:
        with pytest.raises(AgentApiError) as missing:
            client.get("/api/v1/benches/missing")
        assert (missing.value.status, missing.value.code) == (404, "BENCH_NOT_FOUND")

        client.post("/api/v1/benches/bench-01/reservation", {"owner": "alice"})
        with pytest.raises(AgentApiError) as conflict:
            client.post("/api/v1/benches/bench-01/reservation", {"owner": "bob"})
        assert conflict.value.code == "BENCH_ALREADY_RESERVED"
        assert (
            cli_main(["--server", base_url, "bench", "reserve", "bench-01", "--owner", "bob"]) == 4
        )
        assert "BENCH_ALREADY_RESERVED" in capsys.readouterr().err

        assert cli_main(["--server", base_url, "bench", "show", "missing"]) == 3
        assert "BENCH_NOT_FOUND" in capsys.readouterr().err
    finally:
        server.shutdown()
        thread.join(timeout=3)
        asyncio.run(agent.shutdown())

    assert cli_main(["--server", "http://127.0.0.1:1", "health"]) == 6
    assert "Could not read" in capsys.readouterr().err


def test_cli_config_resolution_validation_and_invalid_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["config", "validate", "--config-dir", str(tmp_path)]) == 0
    assert "Configuration valid" in capsys.readouterr().out

    (tmp_path / "agent.yaml").write_text("agent:\n  port: nope\n", encoding="utf-8")
    assert cli_main(["config", "validate", "--config-dir", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err

    monkeypatch.setattr(AgentClient, "get", lambda self, path, query=None: [])
    assert cli_main(["health"]) == 1
    assert "invalid" in capsys.readouterr().err

    monkeypatch.setattr(AgentClient, "get", lambda self, path, query=None: {"items": {}})
    assert cli_main(["bench", "list"]) == 1
    assert "invalid items" in capsys.readouterr().err
