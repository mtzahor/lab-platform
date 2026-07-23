from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from lab_platform.core.workflows import (
    SingleBackendResolver,
    WorkflowInvalidError,
    WorkflowNotCancellableError,
    WorkflowOwnerMismatchError,
    WorkflowReservationRequiredError,
    WorkflowRunner,
    WorkflowRunNotFoundError,
    WorkflowService,
    WorkflowStepFailedError,
    WorkflowTargetUnavailableError,
    load_workflow_yaml,
    parse_workflow_yaml,
    resolve_workflow_inputs,
)
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    BenchStatus,
    EventRecord,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.models.workflows import (
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.workflows import SQLiteWorkflowRepository
from pydantic import ValidationError

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


class EdgeBackend:
    def __init__(self) -> None:
        self.online = True
        self.capabilities = ["firmware", "reset", "serial", "probe"]
        self.probe_status = TargetHealthStatus.ONLINE
        self.reset_error: Exception | None = None
        self.serial_delay = 0.0
        self.serial_text: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_benches(self) -> list[BenchSnapshot]:
        return [await self.get_bench("bench-01")]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return BenchSnapshot(
            id=bench_id,
            name="Edge bench",
            status=BenchStatus.AVAILABLE if self.online else BenchStatus.OFFLINE,
            online=self.online,
            powered=True,
            capabilities=self.capabilities,
        )

    async def power_on(self, bench_id: str) -> None:
        return None

    async def power_off(self, bench_id: str) -> None:
        return None

    async def power_cycle(self, bench_id: str) -> None:
        return None

    async def reset(self, bench_id: str) -> None:
        if self.reset_error is not None:
            raise self.reset_error

    async def probe(self, bench_id: str) -> TargetHealth:
        return TargetHealth(bench_id=bench_id, status=self.probe_status)

    async def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:
        yield BackendProgress(percent=100, message=firmware.filename)

    async def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]:
        if self.serial_delay:
            await asyncio.sleep(self.serial_delay)
        for text in self.serial_text:
            yield SerialLine(timestamp=NOW, text=text)


class EdgeReservations:
    def __init__(self) -> None:
        self.id = uuid4()
        self.error: Exception | None = None

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        if self.error is not None:
            raise self.error
        return self.id


class EdgeEvents:
    def __init__(self) -> None:
        self.items: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event


class EdgeLocks:
    def __init__(self) -> None:
        self.released: list[tuple[str, UUID]] = []

    async def acquire(self, bench_id: str, operation_id: UUID, owner: str) -> object:
        return object()

    async def release(self, bench_id: str, operation_id: UUID) -> None:
        self.released.append((bench_id, operation_id))


class PassiveRunner:
    def schedule(self, definition: WorkflowDefinition, run: WorkflowRun) -> None:
        return None

    def request_cancel(self, run_id: UUID) -> bool:
        return False

    async def wait(self, run_id: UUID) -> WorkflowRun:
        raise AssertionError("The passive runner never executes runs")


@dataclass
class EdgeStack:
    database: SQLiteDatabase
    repository: SQLiteWorkflowRepository
    backend: EdgeBackend
    reservations: EdgeReservations
    runner: WorkflowRunner
    service: WorkflowService


def _stack(path: Path) -> EdgeStack:
    database = SQLiteDatabase(path)
    database.initialize()
    repository = SQLiteWorkflowRepository(database)
    backend = EdgeBackend()
    reservations = EdgeReservations()
    resolver = SingleBackendResolver(backend)
    runner = WorkflowRunner(
        repository,
        resolver,
        reservations,
        clock=lambda: NOW,
    )
    service = WorkflowService(
        repository,
        resolver,
        reservations,
        runner,
        clock=lambda: NOW,
    )
    return EdgeStack(database, repository, backend, reservations, runner, service)


def _wait_definition(name: str = "wait-edge", *, seconds: float = 0.01) -> WorkflowDefinition:
    return parse_workflow_yaml(
        f"""
name: {name}
version: 1
requirements: {{capabilities: []}}
steps: [{{action: wait, seconds: {seconds}}}]
"""
    )


def test_workflow_timestamps_normalize_to_utc_and_reject_naive_values() -> None:
    offset_time = datetime(2026, 7, 20, 15, tzinfo=timezone(timedelta(hours=3)))
    run_values: dict[str, object] = {
        "workflow_name": "timestamps",
        "workflow_version": 1,
        "bench_id": "bench-01",
        "owner": "alice",
        "reservation_id": uuid4(),
        "created_at": offset_time,
        "started_at": offset_time,
        "completed_at": offset_time,
    }
    run = WorkflowRun.model_validate(run_values)
    assert run.created_at == NOW
    assert run.started_at == NOW
    assert run.completed_at == NOW
    assert run.created_at.tzinfo is UTC

    step_values: dict[str, object] = {
        "workflow_run_id": run.id,
        "step_index": 0,
        "action": WorkflowAction.WAIT,
        "status": WorkflowStepStatus.SUCCEEDED,
        "started_at": offset_time,
        "completed_at": offset_time,
    }
    result = WorkflowStepResult.model_validate(step_values)
    assert result.started_at == NOW
    assert result.completed_at == NOW
    assert result.started_at.tzinfo is UTC

    naive = datetime(2026, 7, 20, 12)
    for field in ("created_at", "started_at", "completed_at"):
        with pytest.raises(ValidationError, match="timezone-aware"):
            WorkflowRun.model_validate(run_values | {field: naive})
    for field in ("started_at", "completed_at"):
        with pytest.raises(ValidationError, match="timezone-aware"):
            WorkflowStepResult.model_validate(step_values | {field: naive})


def test_workflow_file_and_placeholder_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(WorkflowInvalidError, match="must contain a YAML mapping"):
        parse_workflow_yaml("- not\n- a\n- mapping")
    with pytest.raises(WorkflowInvalidError, match="Could not read workflow file"):
        load_workflow_yaml(tmp_path / "missing.yaml")

    malformed = parse_workflow_yaml(
        """
name: malformed
version: 1
requirements: {capabilities: [firmware]}
steps: [{action: flash, firmware: "${unfinished"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="Malformed workflow placeholder"):
        resolve_workflow_inputs(malformed, None)

    parameterized = parse_workflow_yaml(
        """
name: parameterized-edge
version: 1
requirements: {capabilities: [serial]}
steps: [{action: assert_serial, pattern: "${pattern}"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="Invalid workflow input name"):
        resolve_workflow_inputs(parameterized, {"bad-name": "value", "pattern": "READY"})
    non_string = cast(Mapping[str, str], {"pattern": 42})
    with pytest.raises(WorkflowInvalidError, match="must be a string"):
        resolve_workflow_inputs(parameterized, non_string)
    with pytest.raises(WorkflowInvalidError, match="Resolved workflow inputs are invalid"):
        resolve_workflow_inputs(parameterized, {"pattern": "["})


def test_sqlite_workflow_repository_conflicts_and_missing_updates(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "workflow-repository-edges.db")
        database.initialize()
        repository = SQLiteWorkflowRepository(database)
        definition = _wait_definition("repository-edge")
        conflicting = _wait_definition("repository-edge", seconds=2)
        version_two = definition.model_copy(update={"version": 2})
        try:
            await repository.save_definition(definition)
            with pytest.raises(WorkflowInvalidError, match="different content"):
                await repository.save_definition(conflicting)
            await repository.save_definition(version_two)
            assert await repository.get_definition("repository-edge") == version_two
            assert await repository.get_definition("repository-edge", 1) == definition
            assert await repository.get_definition("missing", 99) is None
            assert await repository.list_definitions() == [version_two, definition]

            missing_run = WorkflowRun(
                workflow_name=definition.name,
                workflow_version=definition.version,
                bench_id="bench-missing",
                owner="alice",
                reservation_id=uuid4(),
                created_at=NOW,
            )
            with pytest.raises(WorkflowRunNotFoundError):
                await repository.update_run(missing_run)

            run = missing_run.model_copy(update={"bench_id": "bench-01"})
            await repository.create_run(run)
            duplicate_id = run.model_copy(update={"bench_id": "bench-02"})
            with pytest.raises(WorkflowInvalidError, match="could not be persisted"):
                await repository.create_run(duplicate_id)

            orphan = WorkflowStepResult(
                workflow_run_id=uuid4(),
                step_index=0,
                action=WorkflowAction.WAIT,
                status=WorkflowStepStatus.RUNNING,
                started_at=NOW,
            )
            with pytest.raises(WorkflowInvalidError, match="could not be persisted"):
                await repository.create_step_result(orphan)

            result = orphan.model_copy(update={"workflow_run_id": run.id})
            await repository.create_step_result(result)
            with pytest.raises(WorkflowInvalidError, match="could not be persisted"):
                await repository.create_step_result(result)
            missing_result = result.model_copy(update={"id": uuid4()})
            with pytest.raises(WorkflowRunNotFoundError, match="does not exist"):
                await repository.update_step_result(missing_result)
        finally:
            database.close()

    asyncio.run(scenario())


def test_runner_records_unavailable_targets_and_step_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "runner-edge-failures.db")
        try:
            await stack.service.register(
                parse_workflow_yaml(
                    """
name: serial-pattern-missing
version: 1
requirements: {capabilities: [serial]}
steps: [{action: read_serial, until_pattern: READY, timeout_seconds: 1}]
"""
                )
            )
            missing_pattern = await stack.service.start(
                "serial-pattern-missing", bench_id="bench-01", owner="alice"
            )
            failed = await stack.service.wait(missing_pattern.id)
            assert failed.status is WorkflowRunStatus.FAILED
            assert failed.error_code == WorkflowStepFailedError.code
            assert (await stack.service.list_step_results(missing_pattern.id))[0].status is (
                WorkflowStepStatus.FAILED
            )

            await stack.service.register(
                parse_workflow_yaml(
                    """
name: probe-offline
version: 1
requirements: {capabilities: [probe]}
steps: [{action: probe}]
"""
                )
            )
            stack.backend.probe_status = TargetHealthStatus.OFFLINE
            probe = await stack.service.start("probe-offline", bench_id="bench-01", owner="alice")
            probe_failed = await stack.service.wait(probe.id)
            assert probe_failed.error_code == WorkflowTargetUnavailableError.code

            await stack.service.register(
                parse_workflow_yaml(
                    """
name: reset-failure
version: 1
requirements: {capabilities: [reset]}
steps: [{action: reset}]
"""
                )
            )
            stack.backend.reset_error = RuntimeError("reset transport failed")
            reset = await stack.service.start("reset-failure", bench_id="bench-01", owner="alice")
            reset_failed = await stack.service.wait(reset.id)
            assert reset_failed.error_code == WorkflowStepFailedError.code
            assert reset_failed.error_message == "reset transport failed"

            stack.backend.reset_error = None
            stack.backend.online = False
            with pytest.raises(WorkflowTargetUnavailableError):
                await stack.service.start("reset-failure", bench_id="bench-01", owner="alice")
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_runner_handles_timeout_firmware_and_reservation_drift(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "runner-input-failures.db")
        try:
            await stack.service.register_yaml(
                """
name: serial-timeout
version: 1
requirements: {capabilities: [serial]}
steps: [{action: read_serial, timeout_seconds: 0.001}]
"""
            )
            stack.backend.serial_delay = 0.05
            timed_out = await stack.service.start(
                "serial-timeout", bench_id="bench-01", owner="alice"
            )
            timeout_result = await stack.service.wait(timed_out.id)
            assert timeout_result.error_code == WorkflowStepFailedError.code
            assert "timed out" in (timeout_result.error_message or "")
            stack.backend.serial_delay = 0

            for name, firmware in (
                ("firmware-missing", tmp_path / "missing.bin"),
                ("firmware-empty", tmp_path / "empty.bin"),
            ):
                if name == "firmware-empty":
                    firmware.touch()
                await stack.service.register_yaml(
                    f"""
name: {name}
version: 1
requirements: {{capabilities: [firmware]}}
steps: [{{action: flash, firmware: {firmware}}}]
"""
                )
                run = await stack.service.start(name, bench_id="bench-01", owner="alice")
                failed = await stack.service.wait(run.id)
                assert failed.error_code == WorkflowStepFailedError.code
                assert "Firmware file" in (failed.error_message or "")

            await stack.service.register(_wait_definition("reservation-drift"))
            drifted = await stack.service.start(
                "reservation-drift", bench_id="bench-01", owner="alice"
            )
            stack.reservations.id = uuid4()
            reservation_failed = await stack.service.wait(drifted.id)
            assert reservation_failed.error_code == WorkflowReservationRequiredError.code

            assert not stack.runner.request_cancel(uuid4())
            with pytest.raises(WorkflowRunNotFoundError):
                await stack.runner.wait(uuid4())
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_service_cancel_fallback_and_terminal_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "cancel-fallback.db")
        database.initialize()
        repository = SQLiteWorkflowRepository(database)
        definition = _wait_definition("cancel-fallback")
        await repository.save_definition(definition)
        backend = EdgeBackend()
        reservations = EdgeReservations()
        events = EdgeEvents()
        locks = EdgeLocks()
        runner = cast(WorkflowRunner, PassiveRunner())
        service = WorkflowService(
            repository,
            SingleBackendResolver(backend),
            reservations,
            runner,
            events,
            locks,
            clock=lambda: NOW,
        )
        try:
            pending = WorkflowRun(
                workflow_name=definition.name,
                workflow_version=definition.version,
                bench_id="bench-01",
                owner="alice",
                reservation_id=reservations.id,
                created_at=NOW,
            )
            await repository.create_run(pending)
            with pytest.raises(WorkflowOwnerMismatchError):
                await service.cancel(pending.id, "bob")
            cancelled = await service.cancel(pending.id, "alice")
            assert cancelled.status is WorkflowRunStatus.CANCELLED
            assert await service.cancel(pending.id, "alice") == cancelled
            assert locks.released == [("bench-01", pending.id)]
            assert events.items[-1].type == "WORKFLOW_CANCELLED"

            terminal = pending.model_copy(
                update={
                    "id": uuid4(),
                    "bench_id": "bench-02",
                    "status": WorkflowRunStatus.SUCCEEDED,
                    "completed_at": NOW,
                }
            )
            await repository.create_run(terminal)
            with pytest.raises(WorkflowNotCancellableError):
                await service.cancel(terminal.id, "alice")
            with pytest.raises(WorkflowRunNotFoundError):
                await service.get_run(uuid4())
            with pytest.raises(WorkflowRunNotFoundError):
                await service.list_step_results(uuid4())
        finally:
            database.close()

    asyncio.run(scenario())
