from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import pytest
from lab_platform.core import (
    AgentCore,
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    BenchOperationInProgressError,
    BenchService,
    CapabilityNotSupportedError,
    CapabilityRegistry,
    EventBus,
    EventService,
    HealthMonitor,
    OperationNotCancellableError,
    OperationNotFoundError,
    OperationRunner,
    OperationService,
    ReservationOwnerMismatchError,
    ReservationService,
    Scheduler,
    StateMachine,
    StateTransitionError,
)
from lab_platform.core.clock import UtcClock, as_utc, utc_now
from lab_platform.models import (
    Bench,
    Capability,
    Event,
    FirmwareInput,
    HealthStatus,
    Operation,
    OperationStatus,
    OperationType,
    PluginMetadata,
)
from lab_platform.persistence import (
    SQLiteArtifactRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationRepository,
    SQLiteReservationRepository,
)
from lab_platform.simlab_adapter import SimLabBackend


@dataclass
class ServiceStack:
    database: SQLiteDatabase
    backend: SimLabBackend
    benches: BenchService
    reservations: ReservationService
    operations: OperationService
    events: EventService
    runner: OperationRunner


async def _stack(
    tmp_path: Path,
    *,
    speed: float = 500,
    flash_duration: float = 1,
) -> ServiceStack:
    database = SQLiteDatabase(tmp_path / "service.db")
    database.initialize()
    backend = SimLabBackend(
        bench_count=2,
        speed_multiplier=speed,
        flash_duration_seconds=flash_duration,
    )
    await backend.start()
    reservation_repository = SQLiteReservationRepository(database)
    operation_repository = SQLiteOperationRepository(database)
    event_repository = SQLiteEventRepository(database)
    runner = OperationRunner(backend, operation_repository, event_repository)
    reservations = ReservationService(backend, reservation_repository, event_repository)
    benches = BenchService(
        backend,
        reservations,
        reservation_repository,
        operation_repository,
        event_repository,
        SQLiteArtifactRepository(database),
        runner,
    )
    return ServiceStack(
        database,
        backend,
        benches,
        reservations,
        OperationService(operation_repository, event_repository, runner),
        EventService(event_repository),
        runner,
    )


async def _terminal(service: OperationService, operation: Operation) -> Operation:
    for _ in range(200):
        current = await service.get_operation(operation.id)
        if current.status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }:
            return current
        await asyncio.sleep(0.002)
    raise AssertionError("operation did not finish")


async def _close(stack: ServiceStack) -> None:
    await stack.runner.shutdown()
    await stack.backend.stop()
    stack.database.close()


def test_reservation_rules_and_bench_projection(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await _stack(tmp_path)
        try:
            reservation = await stack.reservations.reserve("bench-01", "alice")
            assert await stack.reservations.reserve("bench-01", "alice") == reservation
            with pytest.raises(BenchAlreadyReservedError):
                await stack.reservations.reserve("bench-01", "bob")

            bench = await stack.benches.get_bench("bench-01")
            assert bench.status.value == "reserved"
            assert bench.reserved_by == "alice"
            assert [item.status.value for item in await stack.benches.list_benches()] == [
                "reserved",
                "available",
            ]
            with pytest.raises(ReservationOwnerMismatchError):
                await stack.reservations.release("bench-01", "bob")
            await stack.reservations.release("bench-01", "alice")
            await stack.reservations.release("bench-01", "anyone")
            assert await stack.reservations.get_reservation("bench-01") is None

            stack.backend.simulator.set_online("bench-02", False)
            with pytest.raises(BenchOfflineError):
                await stack.reservations.reserve("bench-02", "alice")
            assert (await stack.benches.get_bench("bench-02")).status.value == "offline"
        finally:
            await _close(stack)

    asyncio.run(scenario())


def test_power_flash_locking_failure_and_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await _stack(tmp_path, speed=100, flash_duration=2)
        firmware = FirmwareInput(
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            sha256="c" * 64,
            size_bytes=4,
            version="3.0.0",
        )
        try:
            with pytest.raises(BenchNotReservedError):
                await stack.benches.power_on("bench-01", "alice")
            await stack.reservations.reserve("bench-01", "alice")

            power = await stack.benches.power_cycle("bench-01", "alice")
            with pytest.raises(BenchOperationInProgressError):
                await stack.benches.power_off("bench-01", "alice")
            completed_power = await _terminal(stack.operations, power)
            assert completed_power.status is OperationStatus.SUCCEEDED
            assert completed_power.progress == 100

            flash = await stack.benches.flash_firmware("bench-01", "alice", firmware)
            completed_flash = await _terminal(stack.operations, flash)
            assert completed_flash.message == "Firmware 3.0.0 installed"
            assert (await stack.benches.get_bench("bench-01")).firmware_version == "3.0.0"

            stack.backend.simulator.inject_failure("bench-01", "flash_failure")
            failed = await stack.benches.flash_firmware("bench-01", "alice", firmware)
            completed_failure = await _terminal(stack.operations, failed)
            assert completed_failure.status is OperationStatus.FAILED
            assert completed_failure.error_code == "SIMULATION_FAILURE"

            stack.backend.simulator.inject_failure("bench-01", "usb_disconnect")
            disconnected = await stack.benches.flash_firmware("bench-01", "alice", firmware)
            assert (
                await _terminal(stack.operations, disconnected)
            ).status is OperationStatus.FAILED

            with pytest.raises(CapabilityNotSupportedError):
                await stack.benches.flash_firmware("bench-02", "alice", firmware)
            with pytest.raises(OperationNotFoundError):
                await stack.operations.get_operation(UUID(int=0))

            flashes = await stack.operations.list_operations(
                bench_id="bench-01",
                operation_type=OperationType.FLASH_FIRMWARE,
                limit=10,
            )
            assert len(flashes) == 3
            events = await stack.events.list_events(bench_id="bench-01", limit=100)
            event_types = {event.type for event in events}
            assert {
                "BENCH_RESERVED",
                "POWER_CYCLE_REQUESTED",
                "POWER_CYCLE_COMPLETED",
                "FLASH_REQUESTED",
                "FLASH_PROGRESS",
                "FLASH_COMPLETED",
                "FLASH_FAILED",
                "USB_DISCONNECTED",
            } <= event_types
        finally:
            await _close(stack)

    asyncio.run(scenario())


def test_operation_cancellation_rules(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await _stack(tmp_path, speed=1, flash_duration=30)
        firmware = FirmwareInput(
            filename="slow.bin",
            local_path=tmp_path / "slow.bin",
            sha256="d" * 64,
            size_bytes=4,
        )
        try:
            await stack.reservations.reserve("bench-01", "alice")
            operation = await stack.benches.flash_firmware("bench-01", "alice", firmware)
            await asyncio.sleep(0)
            with pytest.raises(ReservationOwnerMismatchError):
                await stack.operations.cancel_operation(operation.id, "bob")
            requested = await stack.operations.cancel_operation(operation.id, "alice")
            assert requested.status in {
                OperationStatus.CANCEL_REQUESTED,
                OperationStatus.CANCELLED,
            }
            cancelled = await _terminal(stack.operations, operation)
            assert cancelled.status is OperationStatus.CANCELLED
            with pytest.raises(OperationNotCancellableError):
                await stack.operations.cancel_operation(operation.id, "alice")
        finally:
            await _close(stack)

    asyncio.run(scenario())


def test_as_utc_normalizes_aware_values_and_names_rejected_field() -> None:
    offset = datetime(2026, 7, 29, 12, tzinfo=timezone(timedelta(hours=3)))
    assert as_utc(offset) == datetime(2026, 7, 29, 9, tzinfo=UTC)
    assert as_utc(offset).tzinfo is UTC

    naive = datetime(2026, 7, 29, 12)
    with pytest.raises(ValueError, match="^A timezone-aware datetime is required$"):
        as_utc(naive)
    with pytest.raises(ValueError, match="^Gateway clock must be timezone-aware$"):
        as_utc(naive, field="Gateway clock")


def test_utc_now_and_utc_clock_return_aware_utc_timestamps() -> None:
    assert utc_now().tzinfo is UTC
    assert UtcClock().now().tzinfo is UTC


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
