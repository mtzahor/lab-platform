from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from lab_platform.core.backend import LabBackend, bind_operation_id, reset_operation_id
from lab_platform.core.errors import (
    BackendFailureError,
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    CapabilityNotSupportedError,
    OperationArtifactNotFoundError,
    OperationNotCancellableError,
    OperationNotFoundError,
    PlatformError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.operation_locks import OperationLockService
from lab_platform.core.repositories import (
    ActiveReservationRepository,
    ArtifactRepository,
    EventRepository,
    OperationArtifactRepository,
    OperationRepository,
    ReservationRepository,
)
from lab_platform.core.reservation_ports import BenchAvailability
from lab_platform.models import (
    BenchSnapshot,
    BenchStatus,
    EventRecord,
    FirmwareArtifact,
    FirmwareInput,
    Operation,
    OperationArtifact,
    OperationStatus,
    OperationType,
    Reservation,
    ReservationStatus,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
)

Clock = Callable[[], datetime]
ProbeResultHandler = Callable[[TargetHealth], Awaitable[None]]
ProbeFailureHandler = Callable[[str], Awaitable[None]]


class ReservationOwnerGuard(Protocol):
    async def require_owner(self, bench_id: str, owner: str) -> object: ...


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
        operation_artifacts: OperationArtifactRepository | None = None,
        artifacts_directory: Path | None = None,
        clock: Clock = system_clock,
        operation_locks: OperationLockService | None = None,
    ) -> None:
        self._backend = backend
        self._operations = operations
        self._events = events
        self._operation_artifacts = operation_artifacts
        self._artifacts_directory = artifacts_directory
        self._clock = clock
        self._operation_locks = operation_locks
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._logger = logging.getLogger("lab-platform.operations")

    def schedule(
        self,
        operation: Operation,
        firmware: FirmwareInput | None = None,
        serial_request: SerialReadRequest | None = None,
    ) -> None:
        task = asyncio.create_task(self._run(operation.id, firmware, serial_request))
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

    async def _run(
        self,
        operation_id: UUID,
        firmware: FirmwareInput | None,
        serial_request: SerialReadRequest | None,
    ) -> None:
        operation = await self._operations.get(operation_id)
        if operation is None or operation.status is not OperationStatus.PENDING:
            if operation is not None and self._operation_locks is not None:
                await self._operation_locks.release(operation.bench_id, operation.id)
            return
        running = operation.transition(
            OperationStatus.RUNNING,
            now=self._clock(),
            progress=0,
            message="Starting operation",
        )
        await self._operations.update(running)
        self._log_operation(running)
        operation_token = bind_operation_id(str(operation_id))
        try:
            await self._execute(running, firmware, serial_request)
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
        finally:
            reset_operation_id(operation_token)
            if self._operation_locks is not None:
                try:
                    await self._operation_locks.release(running.bench_id, running.id)
                except Exception:
                    self._logger.exception(
                        "Could not release operation lock",
                        extra={
                            "operation_id": str(running.id),
                            "bench_id": running.bench_id,
                        },
                    )

    async def _execute(
        self,
        operation: Operation,
        firmware: FirmwareInput | None,
        serial_request: SerialReadRequest | None,
    ) -> None:
        serial_lines: list[SerialLine] = []
        try:
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
                    serial_lines.extend(progress.serial_lines)
            elif operation.type is OperationType.RESET:
                await self._set_progress(operation.id, 20, "Resetting target")
                await self._backend.reset(operation.bench_id)
            elif operation.type is OperationType.SERIAL_READ:
                if serial_request is None:
                    raise BackendFailureError("Serial read request is missing.")
                await self._set_progress(operation.id, 10, "Opening serial port")
                async for line in self._backend.read_serial(operation.bench_id, serial_request):
                    serial_lines.append(line)
                await self._set_progress(operation.id, 90, f"Captured {len(serial_lines)} lines")
            else:  # pragma: no cover - exhaustive enum guard
                raise BackendFailureError(f"Unsupported operation type: {operation.type}")
        finally:
            if serial_lines or operation.type is OperationType.SERIAL_READ:
                await self._persist_serial_capture(operation.id, serial_lines)

    async def _set_progress(self, operation_id: UUID, percent: int, message: str) -> None:
        latest = await self._require_operation(operation_id)
        updated = latest.model_copy(update={"progress": percent, "message": message})
        await self._operations.update(updated)
        if latest.type in {OperationType.FLASH_FIRMWARE, OperationType.SERIAL_READ}:
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="FLASH_PROGRESS",
                    source="platform",
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
        if operation.type is OperationType.SERIAL_READ:
            return "Serial capture completed"
        return f"{operation.type.value.replace('_', ' ').title()} completed"

    @staticmethod
    def _completed_event_type(operation_type: OperationType) -> str:
        return {
            OperationType.POWER_ON: "POWER_ON_COMPLETED",
            OperationType.POWER_OFF: "POWER_OFF_COMPLETED",
            OperationType.POWER_CYCLE: "POWER_CYCLE_COMPLETED",
            OperationType.FLASH_FIRMWARE: "FLASH_COMPLETED",
            OperationType.RESET: "RESET_COMPLETED",
            OperationType.SERIAL_READ: "SERIAL_READ_COMPLETED",
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

    async def _persist_serial_capture(self, operation_id: UUID, lines: list[SerialLine]) -> None:
        if self._operation_artifacts is None or self._artifacts_directory is None:
            return
        directory = self._artifacts_directory / "operations" / str(operation_id)
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        path = directory / "serial.log"
        content = "".join(f"{line.text}\n" for line in lines)
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        encoded = content.encode("utf-8")
        await self._operation_artifacts.save(
            OperationArtifact(
                operation_id=operation_id,
                type="serial_log",
                path=path,
                size_bytes=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest(),
                created_at=self._clock(),
            )
        )


class BenchService:
    def __init__(
        self,
        backend: LabBackend,
        reservations: ReservationOwnerGuard,
        reservation_repository: ActiveReservationRepository,
        operations: OperationRepository,
        events: EventRepository,
        artifacts: ArtifactRepository,
        runner: OperationRunner,
        clock: Clock = system_clock,
        operation_locks: OperationLockService | None = None,
        availability: BenchAvailability | None = None,
        probe_result_handler: ProbeResultHandler | None = None,
        probe_failure_handler: ProbeFailureHandler | None = None,
    ) -> None:
        self._backend = backend
        self._reservations = reservations
        self._reservation_repository = reservation_repository
        self._operations = operations
        self._events = events
        self._artifacts = artifacts
        self._runner = runner
        self._clock = clock
        self._operation_locks = operation_locks
        self._availability = availability
        self._probe_result_handler = probe_result_handler
        self._probe_failure_handler = probe_failure_handler

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

    async def reset(self, bench_id: str, owner: str) -> Operation:
        return await self._submit(bench_id, owner, OperationType.RESET)

    async def probe(self, bench_id: str, owner: str) -> TargetHealth:
        bench = await self._backend.get_bench(bench_id)
        if "probe" not in {item.lower() for item in bench.capabilities}:
            raise CapabilityNotSupportedError(
                f"Bench {bench_id} does not support probe.",
                bench_id=bench_id,
                capability="probe",
            )
        if self._operation_locks is None:
            await self._reservations.require_owner(bench_id, owner)
            return await self._backend.probe(bench_id)
        probe_id = uuid4()
        active = await self._reservation_repository.get_active(bench_id)
        maintenance_probe = active is None and (
            not bench.online
            or self._availability is not None
            and not self._availability.is_online(bench_id)
        )
        if maintenance_probe:
            await self._operation_locks.acquire_maintenance(
                bench_id,
                probe_id,
                owner,
                lease_seconds=60,
            )
        else:
            await self._reservations.require_owner(bench_id, owner)
            await self._operation_locks.acquire(bench_id, probe_id, owner)
        try:
            health = await self._backend.probe(bench_id)
            if self._probe_result_handler is not None:
                await self._probe_result_handler(health)
            return health
        except asyncio.CancelledError:
            if self._probe_failure_handler is not None:
                await self._probe_failure_handler(bench_id)
            raise
        except Exception:
            if self._probe_failure_handler is not None:
                await self._probe_failure_handler(bench_id)
            raise
        finally:
            await self._operation_locks.release(bench_id, probe_id)

    async def read_serial(
        self,
        bench_id: str,
        owner: str,
        request: SerialReadRequest,
    ) -> Operation:
        return await self._submit(
            bench_id,
            owner,
            OperationType.SERIAL_READ,
            serial_request=request,
        )

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
        serial_request: SerialReadRequest | None = None,
    ) -> Operation:
        bench = await self._backend.get_bench(bench_id)
        required_capability = {
            OperationType.FLASH_FIRMWARE: "firmware",
            OperationType.SERIAL_READ: "serial",
            OperationType.RESET: "reset",
            OperationType.POWER_ON: "power",
            OperationType.POWER_OFF: "power",
            OperationType.POWER_CYCLE: "power",
        }[operation_type]
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
        try:
            if self._operation_locks is not None:
                await self._operation_locks.acquire(bench_id, operation.id, owner)
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
            self._runner.schedule(operation, firmware, serial_request)
        except Exception:
            cancelled = operation.transition(
                OperationStatus.CANCELLED,
                now=self._clock(),
                message="Operation could not acquire the bench lock",
            )
            await self._operations.update(cancelled)
            if self._operation_locks is not None:
                await self._operation_locks.release(bench_id, operation.id)
            raise
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
            OperationType.RESET: "RESET_REQUESTED",
            OperationType.SERIAL_READ: "SERIAL_READ_REQUESTED",
        }[operation_type]


class OperationService:
    def __init__(
        self,
        operations: OperationRepository,
        events: EventRepository,
        runner: OperationRunner,
        artifacts: OperationArtifactRepository | None = None,
        clock: Clock = system_clock,
        operation_locks: OperationLockService | None = None,
    ) -> None:
        self._operations = operations
        self._events = events
        self._runner = runner
        self._artifacts = artifacts
        self._clock = clock
        self._operation_locks = operation_locks

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
            if self._operation_locks is not None:
                await self._operation_locks.release(operation.bench_id, operation.id)
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
                updated = await self._operations.update(cancelled)
                if self._operation_locks is not None:
                    await self._operation_locks.release(operation.bench_id, operation.id)
                return updated
            return requested
        raise OperationNotCancellableError(
            f"Operation {operation_id} cannot be cancelled from {operation.status.value}.",
            operation_id=str(operation_id),
        )

    async def cancel_for_expiry(self, operation_id: UUID) -> bool:
        """Request cancellation for a grace overrun without bypassing normal cleanup."""

        operation = await self._operations.get(operation_id)
        if operation is None:
            return False
        if operation.status in {OperationStatus.PENDING, OperationStatus.RUNNING}:
            await self.cancel_operation(operation_id, operation.requested_by)
            return True
        if operation.status is OperationStatus.CANCEL_REQUESTED:
            self._runner.cancel(operation_id)
            return True
        return False

    async def list_artifacts(self, operation_id: UUID) -> list[OperationArtifact]:
        await self.get_operation(operation_id)
        if self._artifacts is None:
            return []
        return await self._artifacts.list_for_operation(operation_id)

    async def read_serial_artifact(self, operation_id: UUID) -> str:
        artifacts = await self.list_artifacts(operation_id)
        artifact = next((item for item in artifacts if item.type == "serial_log"), None)
        if artifact is None or not artifact.path.is_file():
            raise OperationArtifactNotFoundError(
                f"Operation {operation_id} has no serial log artifact.",
                operation_id=str(operation_id),
                artifact_type="serial_log",
            )
        return await asyncio.to_thread(artifact.path.read_text, encoding="utf-8")


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
