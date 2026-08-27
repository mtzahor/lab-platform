from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent import create_agent, create_app
from lab_platform.agent.runtime import _plugin_capabilities
from lab_platform.cli.client import AgentClient
from lab_platform.models import Capability, HealthStatus, PluginMetadata
from lab_platform.plugin_sdk import BaseHardwarePlugin
from lab_platform.plugins import BasePlugin, PluginManager

cli = importlib.import_module("lab_platform.cli.main")
agent_cli = importlib.import_module("lab_platform.agent.cli")


class _BrokenPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="broken",
                version="1.0.0",
                description="Fails its lifecycle without affecting healthy plugins.",
            ),
            [Capability(name="probe")],
        )

    async def initialize(self) -> None:
        raise RuntimeError("driver dependency missing")


class _CliClient(AgentClient):
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
        self.paths: list[str] = []

    def get(
        self,
        path: str,
        query: dict[str, object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        self.paths.append(path)
        return self.responses[path]


def test_resilient_manager_preserves_healthy_plugins_and_diagnoses_failures() -> None:
    async def scenario() -> None:
        manager = PluginManager({"broken": _BrokenPlugin})
        report = await manager.load_resilient(["broken", "power"])

        assert report.loaded == ["power"]
        assert [failure.code for failure in report.failures] == ["PLUGIN_INITIALIZE_FAILED"]
        assert [plugin.metadata.name for plugin in manager.plugins()] == ["power"]
        runtime = {item.name: item for item in manager.runtime_info()}
        assert runtime["broken"].status == "failed"
        assert runtime["power"].status == "healthy"

        diagnostics = {item.plugin: item for item in await manager.diagnose_all()}
        assert diagnostics["broken"].checks[0].status == "fail"
        assert diagnostics["broken"].checks[0].details == {"error_code": "PLUGIN_INITIALIZE_FAILED"}
        assert diagnostics["power"].checks[0].name == "compatibility"
        await manager.shutdown()

    asyncio.run(scenario())


def test_agent_capability_bridge_accepts_plugin_api_1_driver_metadata() -> None:
    plugin = BaseHardwarePlugin(
        PluginMetadata(
            name="reference-target",
            version="1.0.0",
            description="Plugin API 1.x reference target.",
            capabilities=["probe", "flash", "serial"],
        )
    )

    capabilities = _plugin_capabilities(plugin)

    assert [capability.name for capability in capabilities] == [
        "probe",
        "flash",
        "serial",
    ]
    assert capabilities[1].metadata == {
        "plugin": "reference-target",
        "plugin_version": "1.0.0",
    }


def test_agent_plugin_api_reports_isolated_startup_failure(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: plugin-agent\n  log_level: ERROR\n"
        "plugins:\n  - power\n  - does-not-exist\n",
        encoding="utf-8",
    )
    agent = create_agent(tmp_path)

    with TestClient(create_app(agent)) as client:
        assert agent.started
        assert [plugin.name for plugin in agent.plugins()] == ["power"]
        plugin_health = next(
            report for report in agent.health_reports() if report.component == "plugins"
        )
        assert plugin_health.status is HealthStatus.WARNING

        payload = client.get("/api/v1/plugins").json()
        by_name = {item["name"]: item for item in payload["items"]}
        assert by_name["power"]["status"] == "healthy"
        assert by_name["does-not-exist"]["error_code"] == "PLUGIN_NOT_FOUND"
        assert payload["load_report"]["loaded"] == ["power"]

        doctor = client.get("/api/v1/plugins/doctor").json()
        by_name = {item["plugin"]: item for item in doctor["items"]}
        assert by_name["does-not-exist"]["checks"][0]["status"] == "fail"
        assert client.get("/api/v1/plugins/power").json()["status"] == "healthy"
        assert client.get("/api/v1/plugins/not-configured").status_code == 404


def test_labctl_plugin_list_show_and_doctor_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    responses: dict[str, object] = {
        "/api/v1/plugins": {
            "items": [
                {
                    "name": "power",
                    "source": "builtin:power",
                    "status": "healthy",
                    "metadata": {
                        "version": "1.0.0",
                        "plugin_api_version": "1.0",
                        "capabilities": ["power"],
                    },
                    "device_count": 1,
                }
            ]
        },
        "/api/v1/plugins/power": {
            "name": "power",
            "source": "builtin:power",
            "status": "healthy",
            "metadata": {"version": "1.0.0", "plugin_api_version": "1.0"},
        },
        "/api/v1/plugins/doctor": {
            "items": [
                {
                    "plugin": "power",
                    "checks": [
                        {
                            "name": "permissions",
                            "status": "pass",
                            "message": "USB access available",
                        }
                    ],
                }
            ]
        },
    }
    client = _CliClient(responses)

    for arguments in (("list",), ("show", "power"), ("doctor",)):
        args = cli._build_parser().parse_args(["plugin", *arguments])
        assert cli._plugin_command(client, args) == 0

    assert client.paths == [
        "/api/v1/plugins",
        "/api/v1/plugins/power",
        "/api/v1/plugins/doctor",
    ]
    assert "USB access available" in capsys.readouterr().out


def test_lab_agent_doctor_covers_phase9_hardware_categories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: hardware-doctor\n  log_level: ERROR\nplugins:\n  - power\n",
        encoding="utf-8",
    )

    assert agent_cli.main(["doctor", "--config-dir", str(tmp_path), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in payload["checks"]}

    assert {
        "plugins",
        "plugin:power",
        "resources",
        "drivers",
        "resource conflicts",
        "permissions:artifacts",
        "serial",
        "usb",
        "external tools",
    } <= names
