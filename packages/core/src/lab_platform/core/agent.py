from __future__ import annotations

from collections.abc import Sequence

from lab_platform.core.capabilities import CapabilityRegistry
from lab_platform.core.events import EventBus
from lab_platform.core.health import HealthMonitor
from lab_platform.models import Bench, Capability, Event, HealthStatus, PluginMetadata


class AgentCore:
    def __init__(
        self,
        event_bus: EventBus,
        health_monitor: HealthMonitor,
        capability_registry: CapabilityRegistry,
    ) -> None:
        self._event_bus = event_bus
        self._health_monitor = health_monitor
        self._capability_registry = capability_registry
        self._benches: dict[str, Bench] = {}
        self._plugins: dict[str, PluginMetadata] = {}

    async def start(self) -> None:
        self._health_monitor.report("core", HealthStatus.HEALTHY, "Core started")
        await self._event_bus.publish(Event(type="AgentStarted"))

    async def register_bench(self, bench: Bench) -> None:
        self._benches[bench.name] = bench
        await self._event_bus.publish(Event(type="BenchRegistered", payload={"name": bench.name}))

    async def register_plugin(
        self,
        plugin: PluginMetadata,
        capabilities: Sequence[Capability] = (),
    ) -> None:
        self._plugins[plugin.name] = plugin
        self._capability_registry.register_many(capabilities)
        await self._event_bus.publish(Event(type="PluginLoaded", payload={"name": plugin.name}))

    def benches(self) -> list[Bench]:
        return [self._benches[name] for name in sorted(self._benches)]

    def plugins(self) -> list[PluginMetadata]:
        return [self._plugins[name] for name in sorted(self._plugins)]

    def capabilities(self) -> list[Capability]:
        return self._capability_registry.capabilities()

    def health(self) -> HealthStatus:
        return self._health_monitor.overall_status()

    async def shutdown(self) -> None:
        for bench in self.benches():
            await self._event_bus.publish(Event(type="BenchOffline", payload={"name": bench.name}))
        self._benches.clear()
        self._plugins.clear()
        self._capability_registry.clear()
        self._health_monitor.report("core", HealthStatus.WARNING, "Core stopped")
        await self._event_bus.publish(Event(type="AgentStopped"))
