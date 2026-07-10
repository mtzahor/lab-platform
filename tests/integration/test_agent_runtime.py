from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from lab_platform.agent.cli import main as agent_main
from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.config import AgentSettings, PlatformConfig, SimLabSettings
from lab_platform.core import AgentCore, CapabilityRegistry, EventBus, HealthMonitor
from lab_platform.models import Capability, Event, HealthStatus, PluginMetadata
from lab_platform.plugins import BasePlugin, PluginLoadError, PluginManager
from lab_platform.simlab import SimLab


def test_agent_starts_simlab_plugins_health_and_events() -> None:
    async def scenario() -> None:
        bus = EventBus()
        monitor = HealthMonitor()
        core = AgentCore(bus, monitor, CapabilityRegistry())
        events: list[Event] = []
        bus.subscribe("*", events.append)
        agent = LabAgent(
            config=PlatformConfig(),
            logger=logging.getLogger("tests.agent"),
            event_bus=bus,
            health_monitor=monitor,
            plugin_manager=PluginManager(),
            simlab=SimLab(),
            core=core,
        )

        await agent.start()
        await agent.start()

        assert len(agent.benches()) == 5
        assert len(agent.plugins()) == 3
        assert agent.health_payload() == {
            "status": "healthy",
            "version": "0.1.0-alpha",
            "benches": 5,
            "plugins": 3,
        }
        assert all(report.status is HealthStatus.HEALTHY for report in agent.health_reports())
        event_types = [event.type for event in events]
        assert "AgentStarted" in event_types
        assert event_types.count("PluginLoaded") == 3
        assert event_types.count("BenchRegistered") == 5
        assert "HealthChanged" in event_types

        await agent.shutdown()
        await agent.shutdown()
        assert agent.benches() == []
        assert agent.plugins() == []
        assert agent.health_payload()["status"] == "warning"
        assert [event.type for event in events].count("BenchOffline") == 5

    asyncio.run(scenario())


class StartupFailurePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="failure",
                version="1.0.0",
                author="tests",
                description="always fails",
                capabilities=["Failure"],
            ),
            [Capability(name="Failure")],
        )

    async def initialize(self) -> None:
        raise RuntimeError("startup failed")


def test_agent_marks_failed_start_unhealthy() -> None:
    async def scenario() -> None:
        bus = EventBus()
        monitor = HealthMonitor()
        agent = LabAgent(
            config=PlatformConfig(plugins=["failure"]),
            logger=logging.getLogger("tests.agent.failure"),
            event_bus=bus,
            health_monitor=monitor,
            plugin_manager=PluginManager({"failure": StartupFailurePlugin}),
            simlab=SimLab(),
            core=AgentCore(bus, monitor, CapabilityRegistry()),
        )

        with pytest.raises(PluginLoadError, match="Could not initialize"):
            await agent.start()
        assert agent.health_payload()["status"] == "unhealthy"

    asyncio.run(scenario())


def test_create_agent_and_agent_cli_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: integration-agent\nplugins:\n  - power\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  enabled: true\n  benches: 2\n",
        encoding="utf-8",
    )

    agent = create_agent(tmp_path)
    asyncio.run(agent.start())
    assert [bench.name for bench in agent.benches()] == ["bench-01", "bench-02"]
    asyncio.run(agent.shutdown())

    assert agent_main(["--config-dir", str(tmp_path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "Lab Agent v0.1.0-alpha" in output
    assert "✓ 2 benches registered" in output
    assert "✓ 1 plugins loaded" in output


def test_disabled_simlab_starts_without_benches(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = PlatformConfig(
        agent=AgentSettings(name="disabled-test"),
        simlab=SimLabSettings(enabled=False),
        plugins=[],
    )
    bus = EventBus()
    monitor = HealthMonitor()
    agent = LabAgent(
        config=config,
        logger=logging.getLogger("tests.agent.disabled"),
        event_bus=bus,
        health_monitor=monitor,
        plugin_manager=PluginManager(),
        simlab=SimLab(enabled=False),
        core=AgentCore(bus, monitor, CapabilityRegistry()),
    )
    asyncio.run(agent.start())
    assert agent.benches() == []
    asyncio.run(agent.shutdown())

    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  enabled: false\nplugins: []\n",
        encoding="utf-8",
    )
    assert agent_main(["--config-dir", str(tmp_path), "--once"]) == 0
    assert "✓ SimLab disabled" in capsys.readouterr().out
