from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from lab_platform.core.backend import LabBackend
from lab_platform.core.errors import (
    BackendFailureError,
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    CapabilityNotSupportedError,
    OperationNotCancellableError,
    OperationNotFoundError,
    PlatformError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.repositories import (
    ArtifactRepository,
    EventRepository,
    OperationRepository,
    ReservationRepository,
)
from lab_platform.models import (
    BenchSnapshot,
    BenchStatus,
    EventRecord,
    FirmwareArtifact,
    FirmwareInput,
    Operation,
    OperationStatus,
    OperationType,
    Reservation,
    ReservationStatus,
)

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


class ReservationService:
    def __init__(
        self,
        backend: LabBackend,
        reservations: ReservationRepository,
        events: EventRepository,
        clock: Clock = system_clock,
    ) -> None:
        self._backend = backend
        self._reservations = reservations
        self._events = events
        self._clock = clock

    async def reserve(self, bench_id: str, owner: str) -> Reservation:
        bench = await self._backend.get_bench(bench_id)
        if not bench.online:
            raise BenchOfflineError(f"Bench {bench_id} is offline.", bench_id=bench_id)
        candidate = Reservation(
            id=uuid4(),
            bench_id=bench_id,
            owner=owner,
            created_at=self._clock(),
        )
        reservation = await self._reservations.create(candidate)
        if reservation.owner != owner:
            raise BenchAlreadyReservedError(
                f"Bench {bench_id} is reserved by another owner.",
                bench_id=bench_id,
            )
        if reservation.id == candidate.id:
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="BENCH_RESERVED",
                    source="platform",
                    bench_id=bench_id,
                    actor=owner,
                    payload={"reservation_id": str(reservation.id)},
                )
            )
        return reservation

    async def release(self, bench_id: str, owner: str) -> None:
        await self._backend.get_bench(bench_id)
        reservation = await self._reservations.get_active(bench_id)
        if reservation is None:
            return
        if reservation.owner != owner:
            raise ReservationOwnerMismatchError(
                f"Bench {bench_id} is reserved by another owner.",
                bench_id=bench_id,
            )
        released = reservation.model_copy(
            update={
                "status": ReservationStatus.RELEASED,
                "released_at": self._clock(),
            }
        )
        await self._reservations.release(released)
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type="BENCH_RELEASED",
                source="platform",
                bench_id=bench_id,
                actor=owner,
                payload={"reservation_id": str(reservation.id)},
            )
        )

    async def get_reservation(self, bench_id: str) -> Reservation | None:
        await self._backend.get_bench(bench_id)
        return await self._reservations.get_active(bench_id)

    async def require_owner(self, bench_id: str, owner: str) -> Reservation:
        reservation = await self._reservations.get_active(bench_id)
        if reservation is None:
            raise BenchNotReservedError(
                f"Bench {bench_id} must be reserved before it can be controlled.",
                bench_id=bench_id,
            )
        if reservation.owner != owner:
            raise ReservationOwnerMismatchError(
                f"Bench {bench_id} is reserved by another owner.",
                bench_id=bench_id,
            )
        return reservation


class OperationRunner:
    def __init__(
        self,
        backend: LabBackend,
        operations: OperationRepository,
        events: EventRepository,
        clock: Clock = system_clock,
    ) -> None:
        self._backend = backend
        self._operations = operations
        self._events = events
        self._clock = clock
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._logger = logging.getLogger("lab-platform.operations")

    def schedule(self, operation: Operation, firmware: FirmwareInput | None = None) -> None:
        task = asyncio.create_task(self._run(operation.id, firmware))
        self._tasks[operation.id] = task
        task.add_done_callback(lambda completed: self._remove(operation.id, completed))

    def cancel(self, operation_id: UUID) -> bool:
        task = self._tasks.get(operation_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, operation_id: UUID, firmware: FirmwareInput | None) -> None:
        operation = await self._operations.get(operation_id)
        if operation is None or operation.status is not OperationStatus.PENDING:
            return
        running = operation.transition(
            OperationStatus.RUNNING,
            now=self._clock(),
            progress=0,
            message="Starting operation",
        )
        await self._operations.update(running)
        self._log_operation(running)
        try:
            await self._execute(running, firmware)
            latest = await self._require_operation(operation_id)
            completed = latest.transition(
                OperationStatus.SUCCEEDED,
                now=self._clock(),
                progress=100,
                message=self._completion_message(latest, firmware),
            )
            await self._operations.update(completed)
            self._log_operation(completed)
            await self._events.create(
                self._operation_event(completed, self._completed_event_type(completed.type))
            )
            if completed.type in {OperationType.POWER_ON, OperationType.POWER_CYCLE}:
                await self._events.create(self._operation_event(completed, "BOOT_COMPLETE"))
        except asyncio.CancelledError:
            await self._mark_cancelled(operation_id)
        except Exception as exc:
            await self._mark_failed(operation_id, exc)

    async def _execute(self, operation: Operation, firmware: FirmwareInput | None) -> None:
        if operation.type is OperationType.POWER_ON:
            await self._set_progress(operation.id, 20, "Powering on")
            await self._backend.power_on(operation.bench_id)
        elif operation.type is OperationType.POWER_OFF:
            await self._set_progress(operation.id, 20, "Powering off")
            await self._backend.power_off(operation.bench_id)
        elif operation.type is OperationType.POWER_CYCLE:
            await self._set_progress(operation.id, 20, "Powering off")
            await self._backend.power_cycle(operation.bench_id)
        elif operation.type is OperationType.FLASH_FIRMWARE:
            if firmware is None:
                raise BackendFailureError("Firmware metadata is missing.")
            async for progress in self._backend.flash_firmware(operation.bench_id, firmware):
                await self._set_progress(operation.id, progress.percent, progress.message)
        else:  # pragma: no cover - exhaustive enum guard
            raise BackendFailureError(f"Unsupported operation type: {operation.type}")

    async def _set_progress(self, operation_id: UUID, percent: int, message: str) -> None:
        latest = await self._require_operation(operation_id)
        updated = latest.model_copy(update={"progress": percent, "message": message})
        await self._operations.update(updated)
        if latest.type is OperationType.FLASH_FIRMWARE:
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="FLASH_PROGRESS",
                    source="simlab",
                    bench_id=latest.bench_id,
                    operation_id=latest.id,
                    actor=latest.requested_by,
                    payload={"progress": percent, "message": message},
                )
            )

    async def _mark_cancelled(self, operation_id: UUID) -> None:
        operation = await self._operations.get(operation_id)
        if operation is None or operation.status is OperationStatus.CANCELLED:
            return
        if operation.status is OperationStatus.RUNNING:
            operation = operation.transition(
                OperationStatus.CANCEL_REQUESTED,
                now=self._clock(),
                message="Cancellation requested",
            )
            await self._operations.update(operation)
            self._log_operation(operation)
        if operation.status is OperationStatus.CANCEL_REQUESTED:
            operation = operation.transition(
                OperationStatus.CANCELLED,
                now=self._clock(),
                message="Operation cancelled",
            )
            await self._operations.update(operation)
            await self._events.create(self._operation_event(operation, "OPERATION_CANCELLED"))

    async def _mark_failed(self, operation_id: UUID, exc: Exception) -> None:
        operation = await self._operations.get(operation_id)
        if operation is None or operation.status not in {
            OperationStatus.RUNNING,
            OperationStatus.CANCEL_REQUESTED,
        }:
            return
        code = exc.code if isinstance(exc, PlatformError) else "BACKEND_FAILURE"
        failed = operation.transition(
            OperationStatus.FAILED,
            now=self._clock(),
            message="Operation failed",
            error_code=code,
            error_message=str(exc),
        )
        await self._operations.update(failed)
        self._log_operation(failed)
        event_type = (
            "FLASH_FAILED" if operation.type is OperationType.FLASH_FIRMWARE else "BACKEND_ERROR"
        )
        await self._events.create(self._operation_event(failed, event_type))
        simulated_event = self._simulated_failure_event(str(exc))
        if simulated_event is not None:
            await self._events.create(self._operation_event(failed, simulated_event))

    async def _require_operation(self, operation_id: UUID) -> Operation:
        operation = await self._operations.get(operation_id)
        if operation is None:  # pragma: no cover - repository invariant
            raise OperationNotFoundError(
                f"Operation {operation_id} does not exist.", operation_id=str(operation_id)
            )
        return operation

    @staticmethod
    def _completion_message(operation: Operation, firmware: FirmwareInput | None) -> str:
        if operation.type is OperationType.FLASH_FIRMWARE:
            version = firmware.version if firmware is not None else None
            return f"Firmware {version} installed" if version else "Flash completed"
        return f"{operation.type.value.replace('_', ' ').title()} completed"

    @staticmethod
    def _completed_event_type(operation_type: OperationType) -> str:
        return {
            OperationType.POWER_ON: "POWER_ON_COMPLETED",
            OperationType.POWER_OFF: "POWER_OFF_COMPLETED",
            OperationType.POWER_CYCLE: "POWER_CYCLE_COMPLETED",
            OperationType.FLASH_FIRMWARE: "FLASH_COMPLETED",
        }[operation_type]

    @staticmethod
    def _simulated_failure_event(message: str) -> str | None:
        normalized = message.lower()
        if "usb disconnect" in normalized:
            return "USB_DISCONNECTED"
        if "kernel panic" in normalized:
            return "KERNEL_PANIC"
        if "overheat" in normalized:
            return "TEMPERATURE_WARNING"
        return None

    def _operation_event(self, operation: Operation, event_type: str) -> EventRecord:
        return EventRecord(
            timestamp=self._clock(),
            type=event_type,
            source="platform",
            bench_id=operation.bench_id,
            operation_id=operation.id,
            actor=operation.requested_by,
            payload={"status": operation.status.value},
        )

    def _remove(self, operation_id: UUID, completed: asyncio.Task[None]) -> None:
        if self._tasks.get(operation_id) is completed:
            self._tasks.pop(operation_id, None)

    def _log_operation(self, operation: Operation) -> None:
        self._logger.info(
            "Operation state changed",
            extra={
                "operation_id": str(operation.id),
                "bench_id": operation.bench_id,
                "operation_type": operation.type.value,
                "owner": operation.requested_by,
                "status": operation.status.value,
            },
        )


class BenchService:
    def __init__(
        self,
        backend: LabBackend,
        reservations: ReservationService,
        reservation_repository: ReservationRepository,
        operations: OperationRepository,
        events: EventRepository,
        artifacts: ArtifactRepository,
        runner: OperationRunner,
        clock: Clock = system_clock,
    ) -> None:
        self._backend = backend
        self._reservations = reservations
        self._reservation_repository = reservation_repository
        self._operations = operations
        self._events = events
        self._artifacts = artifacts
        self._runner = runner
        self._clock = clock

    async def list_benches(self) -> list[BenchSnapshot]:
        benches = await self._backend.list_benches()
        return [await self._with_reservation(bench) for bench in benches]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return await self._with_reservation(await self._backend.get_bench(bench_id))

    async def power_on(self, bench_id: str, owner: str) -> Operation:
        return await self._submit(bench_id, owner, OperationType.POWER_ON)

    async def power_off(self, bench_id: str, owner: str) -> Operation:
        return await self._submit(bench_id, owner, OperationType.POWER_OFF)

    async def power_cycle(self, bench_id: str, owner: str) -> Operation:
        return await self._submit(bench_id, owner, OperationType.POWER_CYCLE)

    async def flash_firmware(self, bench_id: str, owner: str, firmware: FirmwareInput) -> Operation:
        await self._artifacts.save(
            FirmwareArtifact(
                sha256=firmware.sha256,
                filename=firmware.filename,
                local_path=firmware.local_path,
                size_bytes=firmware.size_bytes,
                version=firmware.version,
                created_at=self._clock(),
            )
        )
        return await self._submit(
            bench_id,
            owner,
            OperationType.FLASH_FIRMWARE,
            firmware,
        )

    async def _submit(
        self,
        bench_id: str,
        owner: str,
        operation_type: OperationType,
        firmware: FirmwareInput | None = None,
    ) -> Operation:
        bench = await self._backend.get_bench(bench_id)
        required_capability = (
            "firmware" if operation_type is OperationType.FLASH_FIRMWARE else "power"
        )
        if required_capability not in {item.lower() for item in bench.capabilities}:
            raise CapabilityNotSupportedError(
                f"Bench {bench_id} does not support {required_capability}.",
                bench_id=bench_id,
                capability=required_capability,
            )
        await self._reservations.require_owner(bench_id, owner)
        operation = Operation.pending(
            bench_id,
            operation_type,
            owner,
            now=self._clock(),
        )
        await self._operations.create(operation)
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type=self._requested_event_type(operation_type),
                source="platform",
                bench_id=bench_id,
                operation_id=operation.id,
                actor=owner,
                payload={},
            )
        )
        self._runner.schedule(operation, firmware)
        return operation

    async def _with_reservation(self, bench: BenchSnapshot) -> BenchSnapshot:
        reservation = await self._reservation_repository.get_active(bench.id)
        if reservation is None:
            status = BenchStatus.AVAILABLE if bench.online else BenchStatus.OFFLINE
            return bench.model_copy(update={"reserved_by": None, "status": status})
        return bench.model_copy(
            update={"reserved_by": reservation.owner, "status": BenchStatus.RESERVED}
        )

    @staticmethod
    def _requested_event_type(operation_type: OperationType) -> str:
        return {
            OperationType.POWER_ON: "POWER_ON_REQUESTED",
            OperationType.POWER_OFF: "POWER_OFF_REQUESTED",
            OperationType.POWER_CYCLE: "POWER_CYCLE_REQUESTED",
            OperationType.FLASH_FIRMWARE: "FLASH_REQUESTED",
        }[operation_type]


class OperationService:
    def __init__(
        self,
        operations: OperationRepository,
        events: EventRepository,
        runner: OperationRunner,
        clock: Clock = system_clock,
    ) -> None:
        self._operations = operations
        self._events = events
        self._runner = runner
        self._clock = clock

    async def get_operation(self, operation_id: UUID) -> Operation:
        operation = await self._operations.get(operation_id)
        if operation is None:
            raise OperationNotFoundError(
                f"Operation {operation_id} does not exist.",
                operation_id=str(operation_id),
            )
        return operation

    async def recover_interrupted(self) -> int:
        return await recover_interrupted_operations(
            self._operations,
            self._events,
            self._clock,
        )

    async def list_operations(
        self,
        *,
        bench_id: str | None = None,
        status: OperationStatus | None = None,
        operation_type: OperationType | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        return await self._operations.list(
            bench_id=bench_id,
            status=status,
            operation_type=operation_type,
            limit=limit,
        )

    async def cancel_operation(self, operation_id: UUID, owner: str) -> Operation:
        operation = await self.get_operation(operation_id)
        if operation.requested_by != owner:
            raise ReservationOwnerMismatchError(
                f"Operation {operation_id} was requested by another owner.",
                operation_id=str(operation_id),
            )
        if operation.status is OperationStatus.PENDING:
            cancelled = operation.transition(
                OperationStatus.CANCELLED,
                now=self._clock(),
                message="Operation cancelled",
            )
            await self._operations.update(cancelled)
            self._runner.cancel(operation_id)
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="OPERATION_CANCELLED",
                    source="platform",
                    bench_id=operation.bench_id,
                    operation_id=operation.id,
                    actor=owner,
                    payload={},
                )
            )
            return cancelled
        if operation.status is OperationStatus.RUNNING:
            requested = operation.transition(
                OperationStatus.CANCEL_REQUESTED,
                now=self._clock(),
                message="Cancellation requested",
            )
            await self._operations.update(requested)
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="OPERATION_CANCEL_REQUESTED",
                    source="platform",
                    bench_id=operation.bench_id,
                    operation_id=operation.id,
                    actor=owner,
                    payload={},
                )
            )
            if not self._runner.cancel(operation_id):
                cancelled = requested.transition(
                    OperationStatus.CANCELLED,
                    now=self._clock(),
                    message="Operation cancelled",
                )
                return await self._operations.update(cancelled)
            return requested
        raise OperationNotCancellableError(
            f"Operation {operation_id} cannot be cancelled from {operation.status.value}.",
            operation_id=str(operation_id),
        )


class EventService:
    def __init__(self, events: EventRepository) -> None:
        self._events = events

    async def list_events(
        self,
        *,
        bench_id: str | None = None,
        event_type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        return await self._events.list(
            bench_id=bench_id,
            event_type=event_type,
            after=after,
            before=before,
            limit=limit,
        )


async def recover_interrupted_operations(
    operations: OperationRepository,
    events: EventRepository,
    clock: Clock = system_clock,
) -> int:
    recovered = await operations.recover_incomplete(clock())
    if recovered:
        await events.create(
            EventRecord(
                timestamp=clock(),
                type="BACKEND_ERROR",
                source="platform",
                payload={"code": "AGENT_RESTARTED", "operations_failed": recovered},
            )
        )
    return recovered
