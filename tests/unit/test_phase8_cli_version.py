from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from lab_platform.core import VERSION

cli_module = importlib.import_module("lab_platform.cli.main")


class VersionClient:
    def get(self, path: str) -> object:
        if path == "/api/v1/version":
            return {
                "version": "0.9.0-beta",
                "api_version": "v1",
                "protocol_version": "1.0",
                "plugin_api_version": "1.0",
                "release_channel": "preview",
                "edition": "community",
            }
        if path == "/api/v1/agents":
            return {
                "items": [
                    {
                        "id": "agent-id",
                        "name": "lab-east",
                        "version": "0.8.3",
                        "protocol_version": "1.0",
                        "upgrade": {"status": "UPGRADE_RECOMMENDED"},
                    }
                ]
            }
        raise AssertionError(path)


def test_version_all_reports_local_service_protocol_and_agent_versions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "_client", lambda _args: VersionClient())

    assert cli_module.main(["version", "--all"]) == 0

    output = capsys.readouterr().out
    assert f"CLI {VERSION}" in " ".join(output.split())
    assert "Control Plane" in output
    assert "lab-east" in output
    assert "UPGRADE_RECOMMENDED" in output


def test_version_all_json_preserves_machine_readable_component_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "_client", lambda _args: VersionClient())

    assert cli_module.main(["version", "--all", "--output", "json"]) == 0

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["cli"]["version"] == VERSION
    assert payload["control_plane"]["release_channel"] == "preview"
    assert payload["agents"][0]["version"] == "0.8.3"
    assert payload["agents"][0]["upgrade_status"] == "UPGRADE_RECOMMENDED"
