from __future__ import annotations

import logging
from pathlib import Path

from lab_platform.config import PlatformConfig, load_config
from lab_platform.core import VERSION, AgentCore, CapabilityRegistry, EventBus, HealthMonitor
from lab_platform.logging import get_logger
from lab_platform.models import Bench, Event, HealthReport, HealthStatus, PluginMetadata
from lab_platform.plugins import PluginManager
from lab_platform.simlab import SimLab


class LabAgent:
    def __init__(
        self,
        config: PlatformConfig,
        logger: logging.Logger,
        event_bus: EventBus,
        health_monitor: HealthMonitor,
        plugin_manager: PluginManager,
        simlab: SimLab,
        core: AgentCore,
    ) -> None:
        self.config = config
        self._logger = logger
        self._event_bus = event_bus
        self._health_monitor = health_monitor
        self._plugin_manager = plugin_manager
        self._simlab = simlab
        self._core = core
        self._started = False
        self._health_monitor.report(
            "configuration",
            HealthStatus.HEALTHY,
            "Configuration loaded",
        )
        self._health_monitor.report("logging", HealthStatus.HEALTHY, "Logging initialized")

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self._publish_initial_health()
            await self._set_health("event_bus", HealthStatus.HEALTHY, "Event bus started")
            await self._core.start()

            plugins = await self._plugin_manager.load(self.config.plugins)
            for plugin in plugins:
                await self._core.register_plugin(plugin.metadata, plugin.capabilities())
            await self._set_health(
                "plugins",
                HealthStatus.HEALTHY,
                f"{len(plugins)} plugins loaded",
            )

            await self._simlab.start()
            benches = self._simlab.benches()
            for bench in benches:
                await self._core.register_bench(bench)
            simlab_message = (
                f"{len(benches)} benches registered"
                if self.config.simlab.enabled
                else "SimLab disabled"
            )
            await self._set_health("simlab", HealthStatus.HEALTHY, simlab_message)
            await self._set_health("agent", HealthStatus.HEALTHY, "Agent ready")
            self._logger.info(
                "Agent ready with %d benches and %d plugins",
                len(benches),
                len(plugins),
            )
            self._started = True
        except Exception:
            await self._plugin_manager.shutdown()
            await self._simlab.shutdown()
            await self._core.shutdown()
            await self._set_health("agent", HealthStatus.UNHEALTHY, "Agent failed to start")
            self._logger.exception("Agent startup failed")
            raise

    async def shutdown(self) -> None:
        if not self._started:
            return
        await self._plugin_manager.shutdown()
        await self._simlab.shutdown()
        await self._core.shutdown()
        await self._set_health("agent", HealthStatus.WARNING, "Agent stopped")
        self._logger.info("Agent stopped")
        self._started = False

    def benches(self) -> list[Bench]:
        return self._core.benches()

    def plugins(self) -> list[PluginMetadata]:
        return self._core.plugins()

    def health_reports(self) -> list[HealthReport]:
        return self._health_monitor.component_statuses()

    def health_payload(self) -> dict[str, str | int]:
        return {
            "status": self._core.health().value,
            "version": VERSION,
            "benches": len(self.benches()),
            "plugins": len(self.plugins()),
        }

    async def _publish_initial_health(self) -> None:
        for report in self._health_monitor.component_statuses():
            await self._event_bus.publish(
                Event(
                    type="HealthChanged",
                    payload={
                        "component": report.component,
                        "status": report.status.value,
                        "message": report.message,
                    },
                )
            )

    async def _set_health(
        self,
        component: str,
        status: HealthStatus,
        message: str,
    ) -> None:
        self._health_monitor.report(component, status, message)
        await self._event_bus.publish(
            Event(
                type="HealthChanged",
                payload={
                    "component": component,
                    "status": status.value,
                    "message": message,
                },
            )
        )


def create_agent(config_dir: str | Path = "config") -> LabAgent:
    config = load_config(config_dir)
    logger = get_logger(config.agent.name, config.agent.log_level)
    event_bus = EventBus()
    health_monitor = HealthMonitor()
    capability_registry = CapabilityRegistry()
    plugin_manager = PluginManager()
    simlab = SimLab(
        enabled=config.simlab.enabled,
        bench_count=config.simlab.benches,
    )
    core = AgentCore(
        event_bus=event_bus,
        health_monitor=health_monitor,
        capability_registry=capability_registry,
    )
    return LabAgent(
        config=config,
        logger=logger,
        event_bus=event_bus,
        health_monitor=health_monitor,
        plugin_manager=plugin_manager,
        simlab=simlab,
        core=core,
    )
