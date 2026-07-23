from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core.workflows import (
    SingleBackendResolver,
    WorkflowBenchBusyError,
    WorkflowCapabilityMismatchError,
    WorkflowInvalidError,
    WorkflowReservationRequiredError,
    WorkflowRunner,
    WorkflowService,
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
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.workflows import SQLiteWorkflowRepository
from lab_platform.simlab_adapter import SimLabBackend

NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


class FakeBackend:
    def __init__(self, *, capabilities: list[str] | None = None) -> None:
        self.capabilities = capabilities or ["firmware", "reset", "serial", "probe"]
        self.online = True
        self.calls: list[str] = []
        self.serial_text = ["BOOTING", "SELF_TEST=PASS", "READY"]
        self.firmware_inputs: list[FirmwareInput] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_benches(self) -> list[BenchSnapshot]:
        return [await self.get_bench("bench-01")]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return BenchSnapshot(
            id=bench_id,
            name="Workflow bench",
            status=BenchStatus.AVAILABLE if self.online else BenchStatus.OFFLINE,
            online=self.online,
            powered=True,
            capabilities=self.capabilities,
        )

    async def power_on(self, bench_id: str) -> None:
        raise AssertionError("workflows must not call power actions")

    async def power_off(self, bench_id: str) -> None:
        raise AssertionError("workflows must not call power actions")

    async def power_cycle(self, bench_id: str) -> None:
        raise AssertionError("workflows must not call power actions")

    async def reset(self, bench_id: str) -> None:
        self.calls.append("reset")

    async def probe(self, bench_id: str) -> TargetHealth:
        self.calls.append("probe")
        return TargetHealth(
            bench_id=bench_id,
            status=TargetHealthStatus.ONLINE,
            chip_type="fake",
        )

    async def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:
        self.calls.append("flash")
        self.firmware_inputs.append(firmware)
        yield BackendProgress(percent=0, message="Starting")
        yield BackendProgress(percent=100, message="Complete")

    async def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]:
        self.calls.append("read_serial")
        for text in self.serial_text:
            yield SerialLine(timestamp=NOW, text=text)
            if request.until_pattern == text:
                return


class FakeReservations:
    def __init__(self) -> None:
        self.id = uuid4()
        self.active = True
        self.calls: list[tuple[str, str]] = []

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        self.calls.append((bench_id, owner))
        if not self.active:
            raise RuntimeError("reservation expired")
        return self.id


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event


class FakeOperationLocks:
    def __init__(self) -> None:
        self.acquired: list[tuple[str, UUID, str]] = []
        self.released: list[tuple[str, UUID]] = []

    async def acquire(self, bench_id: str, operation_id: UUID, owner: str) -> object:
        self.acquired.append((bench_id, operation_id, owner))
        return object()

    async def release(self, bench_id: str, operation_id: UUID) -> None:
        self.released.append((bench_id, operation_id))


@dataclass
class WorkflowStack:
    database: SQLiteDatabase
    repository: SQLiteWorkflowRepository
    backend: FakeBackend
    reservations: FakeReservations
    events: FakeEvents
    locks: FakeOperationLocks
    runner: WorkflowRunner
    service: WorkflowService


def _stack(
    path: Path,
    *,
    backend: FakeBackend | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> WorkflowStack:
    database = SQLiteDatabase(path)
    database.initialize()
    repository = SQLiteWorkflowRepository(database)
    selected_backend = backend or FakeBackend()
    reservations = FakeReservations()
    events = FakeEvents()
    locks = FakeOperationLocks()
    resolver = SingleBackendResolver(selected_backend)
    runner = WorkflowRunner(
        repository,
        resolver,
        reservations,
        events,
        locks,
        clock=lambda: NOW,
        sleep=sleep,
    )
    service = WorkflowService(
        repository,
        resolver,
        reservations,
        runner,
        events,
        locks,
        clock=lambda: NOW,
    )
    return WorkflowStack(
        database,
        repository,
        selected_backend,
        reservations,
        events,
        locks,
        runner,
        service,
    )


def test_yaml_parser_is_strict_safe_and_resolves_relative_firmware(tmp_path: Path) -> None:
    path = tmp_path / "smoke.yaml"
    path.write_text(
        """
name: smoke-test
version: 1
description: portable smoke test
requirements:
  capabilities: [Firmware, serial, reset]
steps:
  - action: flash
    firmware: images/demo.bin
    version: 1.2.3
  - action: reset
  - action: read_serial
    until_pattern: READY
    timeout_seconds: 20
  - action: assert_serial
    pattern: SELF_TEST=PASS
""",
        encoding="utf-8",
    )

    definition = load_workflow_yaml(path)
    assert definition.requirements.capabilities == ["firmware", "serial", "reset"]
    assert str(definition.steps[0].firmware) == str(tmp_path / "images/demo.bin")  # type: ignore[union-attr]

    invalid_sources = [
        "name: unsafe\nversion: 1\nrequirements: {capabilities: []}\n"
        "steps: [{action: shell, command: whoami}]",
        "name: missing-cap\nversion: 1\nrequirements: {capabilities: []}\nsteps: [{action: reset}]",
        "name: regex\nversion: 1\nrequirements: {capabilities: [serial]}\n"
        "steps: [{action: assert_serial, pattern: '['}]",
        "!!python/object/apply:os.system ['whoami']",
    ]
    for source in invalid_sources:
        with pytest.raises(WorkflowInvalidError):
            parse_workflow_yaml(source)


def test_input_resolution_is_literal_complete_and_non_mutating() -> None:
    definition = parse_workflow_yaml(
        """
name: parameterized
version: 1
requirements:
  capabilities: [firmware, serial]
steps:
  - action: flash
    firmware: ${firmware}
    version: ${version}
  - action: read_serial
    until_pattern: ${ready_pattern}
  - action: assert_serial
    pattern: ^${assertion}$
"""
    )
    resolved = resolve_workflow_inputs(
        definition,
        {
            "firmware": "demo.bin",
            "version": "3.0.0",
            "ready_pattern": "READY",
            "assertion": "SELF_TEST=PASS",
        },
    )
    assert str(resolved.steps[0].firmware) == "demo.bin"  # type: ignore[union-attr]
    assert resolved.steps[0].version == "3.0.0"  # type: ignore[union-attr]
    assert str(definition.steps[0].firmware) == "${firmware}"  # type: ignore[union-attr]

    with pytest.raises(WorkflowInvalidError, match="required"):
        resolve_workflow_inputs(definition, {"firmware": "demo.bin"})
    with pytest.raises(WorkflowInvalidError, match="Unknown workflow inputs"):
        resolve_workflow_inputs(
            definition,
            {
                "firmware": "demo.bin",
                "version": "1",
                "ready_pattern": "READY",
                "assertion": "PASS",
                "unused": "value",
            },
        )


def test_sequential_workflow_executes_every_supported_action(tmp_path: Path) -> None:
    async def scenario() -> None:
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        stack = _stack(tmp_path / "workflow.db", sleep=fake_sleep)
        firmware = tmp_path / "demo.bin"
        firmware.write_bytes(b"firmware")
        try:
            stored = await stack.service.register_yaml(
                """
name: complete-smoke
version: 1
requirements:
  capabilities: [firmware, reset, serial, probe]
steps:
  - action: flash
    firmware: ${firmware}
    version: 3.0.0
  - action: reset
  - action: read_serial
    until_pattern: READY
  - action: assert_serial
    pattern: SELF_TEST=PASS
  - action: wait
    seconds: 0.25
  - action: probe
""",
                base_directory=tmp_path,
            )
            pending = await stack.service.start(
                stored.name,
                bench_id="bench-01",
                owner="alice",
                inputs={"firmware": firmware.name},
            )
            completed = await stack.service.wait(pending.id)
            results = await stack.service.list_step_results(pending.id)

            assert completed.status is WorkflowRunStatus.SUCCEEDED
            assert completed.current_step is None
            assert stack.backend.calls == ["flash", "reset", "read_serial", "probe"]
            assert stack.backend.firmware_inputs[0].local_path == firmware
            assert waits == [0.25]
            assert [result.action for result in results] == list(WorkflowAction)
            assert all(result.status is WorkflowStepStatus.SUCCEEDED for result in results)
            assert results[3].output["matched_line"] == "SELF_TEST=PASS"
            assert [item[1] for item in stack.locks.acquired] == [pending.id]
            assert stack.locks.released == [("bench-01", pending.id)]
            assert [event.type for event in stack.events.items] == [
                "WORKFLOW_STARTED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_STEP_STARTED",
                "WORKFLOW_STEP_SUCCEEDED",
                "WORKFLOW_COMPLETED",
            ]
            assert str(stored.steps[0].firmware).endswith("${firmware}")  # type: ignore[union-attr]
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_capability_reservation_and_assertion_failures_are_recorded(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        stack = _stack(tmp_path / "failures.db", backend=backend)
        try:
            await stack.service.register_yaml(
                """
name: assertion
version: 1
requirements: {capabilities: [serial]}
steps:
  - action: read_serial
  - action: assert_serial
    pattern: NEVER
"""
            )
            pending = await stack.service.start("assertion", bench_id="bench-01", owner="alice")
            failed = await stack.service.wait(pending.id)
            results = await stack.service.list_step_results(pending.id)
            assert failed.status is WorkflowRunStatus.FAILED
            assert failed.error_code == "WORKFLOW_ASSERTION_FAILED"
            assert [result.status for result in results] == [
                WorkflowStepStatus.SUCCEEDED,
                WorkflowStepStatus.FAILED,
            ]
            assert stack.locks.released == [("bench-01", pending.id)]

            await stack.service.register_yaml(
                """
name: needs-reset
version: 1
requirements: {capabilities: [reset]}
steps: [{action: reset}]
"""
            )
            backend.capabilities = ["serial"]
            with pytest.raises(WorkflowCapabilityMismatchError):
                await stack.service.start("needs-reset", bench_id="bench-01", owner="alice")
            stack.reservations.active = False
            with pytest.raises(WorkflowReservationRequiredError):
                await stack.service.start("assertion", bench_id="bench-01", owner="alice")
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_reservation_expiry_stops_before_the_next_step(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack: WorkflowStack

        async def expire_reservation(seconds: float) -> None:
            stack.reservations.active = False

        stack = _stack(tmp_path / "expiry.db", sleep=expire_reservation)
        try:
            await stack.service.register_yaml(
                """
name: expires
version: 1
requirements: {capabilities: []}
steps:
  - action: wait
    seconds: 1
  - action: wait
    seconds: 1
"""
            )
            pending = await stack.service.start("expires", bench_id="bench-01", owner="alice")
            failed = await stack.service.wait(pending.id)
            assert failed.status is WorkflowRunStatus.FAILED
            assert failed.error_code == "WORKFLOW_RESERVATION_REQUIRED"
            results = await stack.service.list_step_results(pending.id)
            assert len(results) == 1
            assert results[0].status is WorkflowStepStatus.SUCCEEDED
            assert stack.locks.released == [("bench-01", pending.id)]
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_cancellation_and_shutdown_have_distinct_terminal_states(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def blocked_sleep(seconds: float) -> None:
            entered.set()
            await asyncio.Event().wait()

        stack = _stack(tmp_path / "cancel.db", sleep=blocked_sleep)
        try:
            await stack.service.register_yaml(
                """
name: slow
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 30}]
"""
            )
            running = await stack.service.start("slow", bench_id="bench-01", owner="alice")
            await entered.wait()
            requested = await stack.service.cancel(running.id, "alice")
            assert requested.status is WorkflowRunStatus.CANCEL_REQUESTED
            cancelled = await stack.service.wait(running.id)
            assert cancelled.status is WorkflowRunStatus.CANCELLED
            assert cancelled.error_code == "WORKFLOW_CANCELLED"
            assert (await stack.service.list_step_results(running.id))[0].status is (
                WorkflowStepStatus.CANCELLED
            )
            assert stack.locks.released == [("bench-01", running.id)]

            entered.clear()
            interrupted = await stack.service.start("slow", bench_id="bench-01", owner="alice")
            await entered.wait()
            await stack.runner.shutdown()
            restarted = await stack.service.get_run(interrupted.id)
            assert restarted.status is WorkflowRunStatus.FAILED
            assert restarted.error_code == "AGENT_RESTARTED"
            assert (await stack.service.list_step_results(interrupted.id))[0].status is (
                WorkflowStepStatus.FAILED
            )
            assert stack.locks.released[-1] == ("bench-01", interrupted.id)
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_workflows_run_concurrently_on_different_benches_and_reject_same_bench(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered = 0
        both_running = asyncio.Event()
        release = asyncio.Event()

        async def blocked_sleep(seconds: float) -> None:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_running.set()
            await release.wait()

        stack = _stack(tmp_path / "concurrent-workflows.db", sleep=blocked_sleep)
        try:
            await stack.service.register_yaml(
                """
name: concurrent
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 30}]
"""
            )
            first = await stack.service.start("concurrent", bench_id="bench-01", owner="alice")
            second = await stack.service.start("concurrent", bench_id="bench-02", owner="bob")
            await asyncio.wait_for(both_running.wait(), timeout=1)

            assert (await stack.service.get_run(first.id)).status is WorkflowRunStatus.RUNNING
            assert (await stack.service.get_run(second.id)).status is WorkflowRunStatus.RUNNING
            with pytest.raises(WorkflowBenchBusyError):
                await stack.service.start("concurrent", bench_id="bench-01", owner="carol")

            release.set()
            completed = await asyncio.gather(
                stack.service.wait(first.id),
                stack.service.wait(second.id),
            )
            assert [run.status for run in completed] == [
                WorkflowRunStatus.SUCCEEDED,
                WorkflowRunStatus.SUCCEEDED,
            ]
            assert {bench_id for bench_id, _, _ in stack.locks.acquired} == {
                "bench-01",
                "bench-02",
            }
        finally:
            release.set()
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_checked_in_esp32_smoke_workflow_runs_unchanged_on_simlab(tmp_path: Path) -> None:
    async def scenario() -> None:
        workflow_path = (
            Path(__file__).resolve().parents[2] / "examples" / "workflows" / "esp32-smoke-test.yaml"
        )
        backend = SimLabBackend(
            bench_count=1,
            bench_prefix="portable",
            speed_multiplier=1000,
            flash_duration_seconds=0,
        )
        database = SQLiteDatabase(tmp_path / "portable-workflow.db")
        database.initialize()
        repository = SQLiteWorkflowRepository(database)
        reservations = FakeReservations()
        events = FakeEvents()
        locks = FakeOperationLocks()
        resolver = SingleBackendResolver(backend)
        runner = WorkflowRunner(repository, resolver, reservations, events, locks)
        service = WorkflowService(repository, resolver, reservations, runner, events, locks)
        await backend.start()
        try:
            definition = await service.register_yaml(
                workflow_path.read_text(encoding="utf-8"),
                source_name=str(workflow_path),
                base_directory=workflow_path.parent,
            )
            pending = await service.start(
                definition.name,
                bench_id="portable-01",
                owner="alice",
                inputs={"firmware": "../firmware/demo.bin"},
            )
            completed = await service.wait(pending.id)

            assert completed.status is WorkflowRunStatus.SUCCEEDED
            assert (await backend.get_bench("portable-01")).firmware_version == "0.4.0"
        finally:
            await runner.shutdown()
            await backend.stop()
            database.close()

    asyncio.run(scenario())


def test_cancel_before_task_start_releases_lock_and_finishes(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "prestart-cancel.db")
        try:
            await stack.service.register_yaml(
                """
name: cancel-now
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 1}]
"""
            )
            pending = await stack.service.start("cancel-now", bench_id="bench-01", owner="alice")
            await stack.service.cancel(pending.id, "alice")
            cancelled = await stack.service.wait(pending.id)
            assert cancelled.status is WorkflowRunStatus.CANCELLED
            assert await stack.service.list_step_results(pending.id) == []
            assert stack.locks.released == [("bench-01", pending.id)]
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())


def test_sqlite_repository_enforces_active_run_and_recovers_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "repository.db")
        database.initialize()
        repository = SQLiteWorkflowRepository(database)
        definition = parse_workflow_yaml(
            """
name: persisted
version: 1
requirements: {capabilities: []}
steps:
  - action: wait
    seconds: 1
  - action: wait
    seconds: 1
"""
        )
        try:
            assert await repository.save_definition(definition) == definition
            assert await repository.save_definition(definition) == definition
            assert await repository.get_definition("persisted") == definition
            assert await repository.list_definitions() == [definition]

            first = WorkflowRun(
                workflow_name="persisted",
                workflow_version=1,
                bench_id="bench-01",
                owner="alice",
                reservation_id=uuid4(),
                status=WorkflowRunStatus.RUNNING,
                current_step=1,
                created_at=NOW,
                started_at=NOW,
            )
            await repository.create_run(first)
            with pytest.raises(WorkflowBenchBusyError):
                await repository.create_run(
                    first.model_copy(update={"id": uuid4(), "owner": "bob"})
                )
            succeeded = WorkflowStepResult(
                workflow_run_id=first.id,
                step_index=0,
                action=WorkflowAction.WAIT,
                status=WorkflowStepStatus.SUCCEEDED,
                started_at=NOW,
                completed_at=NOW,
                output={"seconds": 1},
            )
            active = WorkflowStepResult(
                workflow_run_id=first.id,
                step_index=1,
                action=WorkflowAction.WAIT,
                status=WorkflowStepStatus.RUNNING,
                started_at=NOW,
            )
            await repository.create_step_result(succeeded)
            await repository.create_step_result(active)

            recovered = await repository.recover_interrupted(NOW)
            assert len(recovered) == 1
            assert recovered[0].status is WorkflowRunStatus.FAILED
            assert recovered[0].error_code == "AGENT_RESTARTED"
            results = await repository.list_step_results(first.id)
            assert results[0] == succeeded
            assert results[1].status is WorkflowStepStatus.FAILED
            assert results[1].error_code == "AGENT_RESTARTED"
            assert await repository.recover_interrupted(NOW) == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_service_recovery_emits_a_workflow_failure_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = _stack(tmp_path / "service-recovery.db")
        try:
            definition = await stack.service.register_yaml(
                """
name: recovering
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 1}]
"""
            )
            interrupted = WorkflowRun(
                workflow_name=definition.name,
                workflow_version=definition.version,
                bench_id="bench-01",
                owner="alice",
                reservation_id=stack.reservations.id,
                status=WorkflowRunStatus.RUNNING,
                created_at=NOW,
                started_at=NOW,
            )
            await stack.repository.create_run(interrupted)
            assert await stack.service.recover_interrupted() == 1
            recovered = await stack.service.get_run(interrupted.id)
            assert recovered.status is WorkflowRunStatus.FAILED
            assert stack.events.items[-1].type == "WORKFLOW_FAILED"
            assert stack.events.items[-1].source == "recovery"
        finally:
            await stack.runner.shutdown()
            stack.database.close()

    asyncio.run(scenario())
