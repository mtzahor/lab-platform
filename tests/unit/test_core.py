from __future__ import annotations

import asyncio
from enum import StrEnum

import pytest
from lab_platform.core import (
    AgentCore,
    CapabilityRegistry,
    EventBus,
    HealthMonitor,
    Scheduler,
    StateMachine,
    StateTransitionError,
)
from lab_platform.models import (
    Bench,
    Capability,
    Event,
    HealthStatus,
    PluginMetadata,
)


def test_event_bus_supports_sync_async_wildcard_and_unsubscribe() -> None:
    bus = EventBus()
    received: list[str] = []

    def sync_handler(event: Event) -> None:
        received.append(f"sync:{event.type}")

    async def async_handler(event: Event) -> None:
        await asyncio.sleep(0)
        received.append(f"async:{event.type}")

    unsubscribe = bus.subscribe("Ready", sync_handler)
    bus.subscribe("*", async_handler)

    asyncio.run(bus.publish(Event(type="Ready")))
    unsubscribe()
    unsubscribe()
    asyncio.run(bus.publish(Event(type="Ready")))
    asyncio.run(bus.publish(Event(type="*")))

    assert received == ["sync:Ready", "async:Ready", "async:Ready", "async:*"]


def test_health_monitor_aggregates_worst_status_and_sorts_reports() -> None:
    monitor = HealthMonitor()
    assert monitor.overall_status() is HealthStatus.HEALTHY

    monitor.report("simlab", HealthStatus.WARNING, "slow")
    monitor.report("agent", HealthStatus.HEALTHY)
    assert [report.component for report in monitor.component_statuses()] == ["agent", "simlab"]
    assert monitor.overall_status() is HealthStatus.WARNING

    monitor.report("simlab", HealthStatus.UNHEALTHY, "offline")
    assert monitor.overall_status() is HealthStatus.UNHEALTHY


def test_capability_registry_rejects_conflicting_definitions() -> None:
    registry = CapabilityRegistry()
    power = Capability(name="Power", description="control")

    registry.register(power)
    registry.register_many([power, Capability(name="Serial")])

    assert registry.get("Power") == power
    assert registry.get("missing") is None
    assert [item.name for item in registry.capabilities()] == ["Power", "Serial"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Capability(name="Power", description="different"))
    registry.clear()
    assert registry.capabilities() == []


class Lifecycle(StrEnum):
    NEW = "new"
    READY = "ready"
    STOPPED = "stopped"


def test_state_machine_allows_only_declared_transitions() -> None:
    machine: StateMachine[Lifecycle] = StateMachine(
        Lifecycle.NEW,
        {
            Lifecycle.NEW: [Lifecycle.READY],
            Lifecycle.READY: [Lifecycle.STOPPED],
        },
    )

    assert machine.state.value == "new"
    assert machine.can_transition(Lifecycle.READY)
    machine.transition_to(Lifecycle.READY)
    assert machine.state.value == "ready"
    with pytest.raises(StateTransitionError, match="Cannot transition"):
        machine.transition_to(Lifecycle.NEW)


def test_scheduler_runs_and_cancels_tasks() -> None:
    async def scenario() -> None:
        scheduler = Scheduler()
        completed = asyncio.Event()

        async def finish() -> None:
            completed.set()

        scheduler.schedule_once("finish", finish)
        with pytest.raises(ValueError, match="already scheduled"):
            scheduler.schedule_once("finish", finish)
        await completed.wait()
        await asyncio.sleep(0)

        blocker = asyncio.Event()

        async def wait_forever() -> None:
            await blocker.wait()

        scheduler.schedule_once("blocked", wait_forever, delay_seconds=0.001)
        await scheduler.shutdown()
        scheduler.schedule_once("new", finish)
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_agent_core_registers_entities_emits_events_and_clears_on_shutdown() -> None:
    async def scenario() -> None:
        bus = EventBus()
        monitor = HealthMonitor()
        registry = CapabilityRegistry()
        core = AgentCore(bus, monitor, registry)
        event_types: list[str] = []
        bus.subscribe("*", lambda event: event_types.append(event.type))

        await core.start()
        await core.register_plugin(
            PluginMetadata(
                name="power",
                version="1.0.0",
                author="test",
                description="test",
                capabilities=["Power"],
            ),
            [Capability(name="Power")],
        )
        await core.register_bench(Bench(name="bench-02"))
        await core.register_bench(Bench(name="bench-01"))

        assert [bench.name for bench in core.benches()] == ["bench-01", "bench-02"]
        assert [plugin.name for plugin in core.plugins()] == ["power"]
        assert [capability.name for capability in core.capabilities()] == ["Power"]
        assert core.health() is HealthStatus.HEALTHY

        await core.shutdown()
        assert core.benches() == []
        assert core.plugins() == []
        assert event_types == [
            "AgentStarted",
            "PluginLoaded",
            "BenchRegistered",
            "BenchRegistered",
            "BenchOffline",
            "BenchOffline",
            "AgentStopped",
        ]

    asyncio.run(scenario())
