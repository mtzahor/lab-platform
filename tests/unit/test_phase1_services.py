from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from lab_platform.core import (
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    BenchOperationInProgressError,
    BenchService,
    CapabilityNotSupportedError,
    EventService,
    OperationNotCancellableError,
    OperationNotFoundError,
    OperationRunner,
    OperationService,
    ReservationOwnerMismatchError,
    ReservationService,
)
from lab_platform.models import FirmwareInput, Operation, OperationStatus, OperationType
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
