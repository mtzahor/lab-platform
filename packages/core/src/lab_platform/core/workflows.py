from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import yaml
from lab_platform.core.backend import LabBackend
from lab_platform.core.errors import PlatformError
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    EventRecord,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.models.workflows import (
    ACTIVE_WORKFLOW_RUN_STATUSES,
    AssertSerialWorkflowStep,
    FlashWorkflowStep,
    ProbeWorkflowStep,
    ReadSerialWorkflowStep,
    ResetWorkflowStep,
    WaitWorkflowStep,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStep,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from pydantic import ValidationError

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
ProbeResultHandler = Callable[[TargetHealth], Awaitable[None]]
ProbeFailureHandler = Callable[[str], Awaitable[None]]


def workflow_clock() -> datetime:
    return datetime.now(UTC)


class WorkflowError(PlatformError):
    pass


class WorkflowNotFoundError(WorkflowError):
    code = "WORKFLOW_NOT_FOUND"


class WorkflowInvalidError(WorkflowError):
    code = "WORKFLOW_INVALID"


class WorkflowCapabilityMismatchError(WorkflowError):
    code = "WORKFLOW_CAPABILITY_MISMATCH"


class WorkflowRunNotFoundError(WorkflowError):
    code = "WORKFLOW_RUN_NOT_FOUND"


class WorkflowAssertionFailedError(WorkflowError):
    code = "WORKFLOW_ASSERTION_FAILED"


class WorkflowReservationRequiredError(WorkflowError):
    code = "WORKFLOW_RESERVATION_REQUIRED"


class WorkflowCancelledError(WorkflowError):
    code = "WORKFLOW_CANCELLED"


class WorkflowStepFailedError(WorkflowError):
    code = "WORKFLOW_STEP_FAILED"


class WorkflowTargetUnavailableError(WorkflowError):
    code = "BACKEND_UNAVAILABLE"


class WorkflowBenchBusyError(WorkflowError):
    code = "BENCH_OPERATION_IN_PROGRESS"


class WorkflowNotCancellableError(WorkflowError):
    code = "WORKFLOW_NOT_CANCELLABLE"


class WorkflowOwnerMismatchError(WorkflowError):
    code = "RESERVATION_OWNER_MISMATCH"


class WorkflowRepository(Protocol):
    async def save_definition(self, definition: WorkflowDefinition) -> WorkflowDefinition: ...

    async def get_definition(
        self, name: str, version: int | None = None
    ) -> WorkflowDefinition | None: ...

    async def list_definitions(self) -> list[WorkflowDefinition]: ...

    async def create_run(self, run: WorkflowRun) -> WorkflowRun: ...

    async def get_run(self, run_id: UUID) -> WorkflowRun | None: ...

    async def update_run(self, run: WorkflowRun) -> WorkflowRun: ...

    async def create_step_result(self, result: WorkflowStepResult) -> WorkflowStepResult: ...

    async def update_step_result(self, result: WorkflowStepResult) -> WorkflowStepResult: ...

    async def list_step_results(self, run_id: UUID) -> list[WorkflowStepResult]: ...

    async def recover_interrupted(self, now: datetime) -> list[WorkflowRun]: ...


class WorkflowBackendResolver(Protocol):
    async def get_backend_for_bench(self, bench_id: str) -> LabBackend: ...


class WorkflowReservationAuthorizer(Protocol):
    async def require_active(self, bench_id: str, owner: str) -> UUID: ...


class WorkflowEventSink(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...


class WorkflowOperationLock(Protocol):
    async def acquire(self, bench_id: str, operation_id: UUID, owner: str) -> object: ...

    async def release(self, bench_id: str, operation_id: UUID) -> object: ...


class SingleBackendResolver:
    """Compatibility adapter for a deployment that still owns one backend."""

    def __init__(self, backend: LabBackend) -> None:
        self._backend = backend

    async def get_backend_for_bench(self, bench_id: str) -> LabBackend:
        await self._backend.get_bench(bench_id)
        return self._backend


def parse_workflow_yaml(
    source: str,
    *,
    source_name: str = "<workflow>",
    base_directory: Path | None = None,
) -> WorkflowDefinition:
    """Parse a declarative workflow using PyYAML's non-executing safe loader."""

    try:
        payload = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise WorkflowInvalidError(
            f"Invalid workflow YAML in {source_name}: {exc}", source=source_name
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowInvalidError(
            f"Workflow {source_name} must contain a YAML mapping.", source=source_name
        )
    try:
        definition = WorkflowDefinition.model_validate(payload)
    except ValidationError as exc:
        raise WorkflowInvalidError(
            f"Invalid workflow definition in {source_name}.",
            source=source_name,
            validation_errors=exc.errors(include_url=False),
        ) from exc
    if base_directory is None:
        return definition
    resolved_steps: list[WorkflowStep] = []
    for step in definition.steps:
        if isinstance(step, FlashWorkflowStep) and not step.firmware.is_absolute():
            step = step.model_copy(update={"firmware": base_directory / step.firmware})
        resolved_steps.append(step)
    return definition.model_copy(update={"steps": resolved_steps})


def load_workflow_yaml(path: Path) -> WorkflowDefinition:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowInvalidError(
            f"Could not read workflow file {path}: {exc}", source=str(path)
        ) from exc
    return parse_workflow_yaml(source, source_name=str(path), base_directory=path.parent)


def validate_workflow_capabilities(definition: WorkflowDefinition, bench: BenchSnapshot) -> None:
    available = {capability.strip().lower() for capability in bench.capabilities}
    missing = sorted(set(definition.requirements.capabilities).difference(available))
    if missing:
        raise WorkflowCapabilityMismatchError(
            f"Bench {bench.id} is missing workflow capabilities: {', '.join(missing)}.",
            bench_id=bench.id,
            missing_capabilities=missing,
        )


def resolve_workflow_inputs(
    definition: WorkflowDefinition,
    inputs: Mapping[str, str] | None,
) -> WorkflowDefinition:
    """Resolve literal ``${name}`` placeholders without evaluating expressions."""

    supplied = dict(inputs or {})
    for name, value in supplied.items():
        if not _INPUT_NAME.fullmatch(name):
            raise WorkflowInvalidError(f"Invalid workflow input name: {name!r}.")
        if not isinstance(value, str):
            raise WorkflowInvalidError(f"Workflow input {name!r} must be a string.")

    used: set[str] = set()

    def substitute(value: str, *, field: str) -> str:
        malformed_check = _INPUT_PLACEHOLDER.sub("", value)
        if "${" in malformed_check:
            raise WorkflowInvalidError(f"Malformed workflow placeholder in {field}.")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in supplied:
                raise WorkflowInvalidError(
                    f"Workflow input {name!r} is required.", missing_input=name
                )
            used.add(name)
            return supplied[name]

        return _INPUT_PLACEHOLDER.sub(replace, value)

    steps: list[WorkflowStep] = []
    for index, step in enumerate(definition.steps):
        if isinstance(step, FlashWorkflowStep):
            updates: dict[str, object] = {
                "firmware": Path(substitute(str(step.firmware), field=f"steps[{index}].firmware"))
            }
            if step.version is not None:
                updates["version"] = substitute(step.version, field=f"steps[{index}].version")
            step = step.model_copy(update=updates)
        elif isinstance(step, ReadSerialWorkflowStep) and step.until_pattern is not None:
            step = step.model_copy(
                update={
                    "until_pattern": substitute(
                        step.until_pattern, field=f"steps[{index}].until_pattern"
                    )
                }
            )
        elif isinstance(step, AssertSerialWorkflowStep):
            step = step.model_copy(
                update={"pattern": substitute(step.pattern, field=f"steps[{index}].pattern")}
            )
        steps.append(step)

    unknown = sorted(set(supplied).difference(used))
    if unknown:
        raise WorkflowInvalidError(
            f"Unknown workflow inputs: {', '.join(unknown)}.", unknown_inputs=unknown
        )
    try:
        payload = definition.model_dump(mode="python")
        payload["steps"] = [step.model_dump(mode="python") for step in steps]
        return WorkflowDefinition.model_validate(payload)
    except ValidationError as exc:
        raise WorkflowInvalidError(
            "Resolved workflow inputs are invalid.",
            validation_errors=exc.errors(include_url=False),
        ) from exc


class WorkflowRunner:
    def __init__(
        self,
        repository: WorkflowRepository,
        backends: WorkflowBackendResolver,
        reservations: WorkflowReservationAuthorizer,
        events: WorkflowEventSink | None = None,
        operation_locks: WorkflowOperationLock | None = None,
        *,
        clock: Clock = workflow_clock,
        sleep: Sleeper = asyncio.sleep,
        probe_result_handler: ProbeResultHandler | None = None,
        probe_failure_handler: ProbeFailureHandler | None = None,
    ) -> None:
        self._repository = repository
        self._backends = backends
        self._reservations = reservations
        self._events = events
        self._operation_locks = operation_locks
        self._clock = clock
        self._sleep = sleep
        self._probe_result_handler = probe_result_handler
        self._probe_failure_handler = probe_failure_handler
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cleanup_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._cancellation_requests: set[UUID] = set()
        self._shutdown_requests: set[UUID] = set()
        self._released_locks: set[UUID] = set()

    def schedule(self, definition: WorkflowDefinition, run: WorkflowRun) -> None:
        existing = self._tasks.get(run.id)
        if existing is not None and not existing.done():
            raise WorkflowBenchBusyError(
                f"Workflow run {run.id} is already scheduled.", workflow_run_id=str(run.id)
            )
        task = asyncio.create_task(self._run(definition, run.id))
        self._tasks[run.id] = task
        task.add_done_callback(lambda completed: self._schedule_cleanup(run, completed))

    def request_cancel(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        self._cancellation_requests.add(run_id)
        task.cancel()
        return True

    async def wait(self, run_id: UUID) -> WorkflowRun:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        cleanup = self._cleanup_tasks.get(run_id)
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)
        return await self._require_run(run_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.items())
        for run_id, task in tasks:
            if not task.done():
                self._shutdown_requests.add(run_id)
                task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
            await asyncio.sleep(0)
        cleanup_tasks = list(self._cleanup_tasks.values())
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, definition: WorkflowDefinition, run_id: UUID) -> None:
        run = await self._require_run(run_id)
        if run.status is WorkflowRunStatus.CANCEL_REQUESTED:
            await self._mark_cancelled(run)
            return
        if run.status is not WorkflowRunStatus.PENDING:
            return

        run = run.model_copy(
            update={
                "status": WorkflowRunStatus.RUNNING,
                "started_at": self._clock(),
                "error_code": None,
                "error_message": None,
            }
        )
        await self._repository.update_run(run)
        await self._emit("WORKFLOW_STARTED", run)

        serial_lines: list[SerialLine] = []
        active_result: WorkflowStepResult | None = None
        try:
            backend = await self._backends.get_backend_for_bench(run.bench_id)
            await self._ensure_reservation(run)
            await self._ensure_bench_ready(definition, backend, run.bench_id)
            for index, step in enumerate(definition.steps):
                await self._ensure_reservation(run)
                await self._ensure_bench_ready(definition, backend, run.bench_id)
                run = run.model_copy(update={"current_step": index})
                await self._repository.update_run(run)
                active_result = WorkflowStepResult(
                    workflow_run_id=run.id,
                    step_index=index,
                    action=WorkflowAction(step.action),
                    status=WorkflowStepStatus.RUNNING,
                    started_at=self._clock(),
                )
                await self._repository.create_step_result(active_result)
                await self._emit(
                    "WORKFLOW_STEP_STARTED",
                    run,
                    step_result=active_result,
                )
                try:
                    output = await self._execute_step(
                        backend,
                        run.bench_id,
                        step,
                        serial_lines,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    active_result = active_result.model_copy(
                        update={
                            "status": WorkflowStepStatus.FAILED,
                            "completed_at": self._clock(),
                            "error_code": _error_code(exc),
                            "error_message": str(exc),
                        }
                    )
                    await self._repository.update_step_result(active_result)
                    await self._emit(
                        "WORKFLOW_STEP_FAILED",
                        run,
                        step_result=active_result,
                    )
                    active_result = None
                    raise
                active_result = active_result.model_copy(
                    update={
                        "status": WorkflowStepStatus.SUCCEEDED,
                        "completed_at": self._clock(),
                        "output": output,
                    }
                )
                await self._repository.update_step_result(active_result)
                await self._emit(
                    "WORKFLOW_STEP_SUCCEEDED",
                    run,
                    step_result=active_result,
                )
                active_result = None

            run = (await self._require_run(run.id)).model_copy(
                update={
                    "status": WorkflowRunStatus.SUCCEEDED,
                    "current_step": None,
                    "completed_at": self._clock(),
                }
            )
            await self._repository.update_run(run)
            await self._emit("WORKFLOW_COMPLETED", run)
        except asyncio.CancelledError:
            await self._finish_interrupted_step(run_id, active_result)
            latest = await self._require_run(run_id)
            if run_id in self._cancellation_requests:
                await self._mark_cancelled(latest)
            else:
                await self._mark_failed(
                    latest,
                    WorkflowStepFailedError(
                        "Agent restarted while the workflow was running.",
                        reason="agent_restart",
                    ),
                    error_code="AGENT_RESTARTED",
                )
        except Exception as exc:
            await self._mark_failed(await self._require_run(run_id), exc)
        finally:
            self._cancellation_requests.discard(run_id)
            self._shutdown_requests.discard(run_id)
            await self._release_operation_lock(run)

    async def _execute_step(
        self,
        backend: LabBackend,
        bench_id: str,
        step: WorkflowStep,
        serial_lines: list[SerialLine],
    ) -> dict[str, Any]:
        if isinstance(step, FlashWorkflowStep):
            firmware = await asyncio.to_thread(_firmware_input, step)
            progress: list[BackendProgress] = []
            async for item in backend.flash_firmware(bench_id, firmware):
                progress.append(item)
                serial_lines.extend(item.serial_lines)
            return {
                "firmware": str(step.firmware),
                "version": step.version,
                "sha256": firmware.sha256,
                "progress": [item.model_dump(mode="json") for item in progress],
            }
        if isinstance(step, ResetWorkflowStep):
            await backend.reset(bench_id)
            return {}
        if isinstance(step, ReadSerialWorkflowStep):
            captured: list[SerialLine] = []
            request = SerialReadRequest(
                until_pattern=step.until_pattern,
                timeout_seconds=step.timeout_seconds,
                max_lines=step.max_lines,
            )
            try:
                async with asyncio.timeout(step.timeout_seconds):
                    async for line in backend.read_serial(bench_id, request):
                        captured.append(line)
            except TimeoutError as exc:
                raise WorkflowStepFailedError(
                    f"Serial read timed out after {step.timeout_seconds:g} seconds."
                ) from exc
            serial_lines.extend(captured)
            if step.until_pattern is not None and not any(
                re.search(step.until_pattern, line.text) for line in captured
            ):
                raise WorkflowStepFailedError(
                    f"Serial pattern {step.until_pattern!r} was not observed."
                )
            return {"lines": [line.model_dump(mode="json") for line in captured]}
        if isinstance(step, AssertSerialWorkflowStep):
            match = next(
                (line for line in serial_lines if re.search(step.pattern, line.text)), None
            )
            if match is None:
                raise WorkflowAssertionFailedError(
                    f"Serial output did not match {step.pattern!r}.", pattern=step.pattern
                )
            return {"pattern": step.pattern, "matched_line": match.text}
        if isinstance(step, WaitWorkflowStep):
            await self._sleep(step.seconds)
            return {"seconds": step.seconds}
        if isinstance(step, ProbeWorkflowStep):
            try:
                health = await backend.probe(bench_id)
                if self._probe_result_handler is not None:
                    await self._probe_result_handler(health)
            except asyncio.CancelledError:
                if self._probe_failure_handler is not None:
                    await self._probe_failure_handler(bench_id)
                raise
            except Exception:
                if self._probe_failure_handler is not None:
                    await self._probe_failure_handler(bench_id)
                raise
            _require_target_available(health)
            return {"health": health.model_dump(mode="json")}
        raise WorkflowInvalidError(f"Unsupported workflow step: {step!r}")

    async def _ensure_reservation(self, run: WorkflowRun) -> None:
        try:
            reservation_id = await self._reservations.require_active(run.bench_id, run.owner)
        except Exception as exc:
            raise WorkflowReservationRequiredError(
                f"Workflow run {run.id} requires an active reservation owned by {run.owner}.",
                bench_id=run.bench_id,
                owner=run.owner,
            ) from exc
        if reservation_id != run.reservation_id:
            raise WorkflowReservationRequiredError(
                f"The reservation for workflow run {run.id} is no longer active.",
                bench_id=run.bench_id,
                reservation_id=str(run.reservation_id),
            )

    @staticmethod
    async def _ensure_bench_ready(
        definition: WorkflowDefinition,
        backend: LabBackend,
        bench_id: str,
    ) -> None:
        bench = await backend.get_bench(bench_id)
        if not bench.online:
            raise WorkflowTargetUnavailableError(
                f"Bench {bench_id} became unavailable.", bench_id=bench_id
            )
        validate_workflow_capabilities(definition, bench)

    async def _finish_interrupted_step(
        self,
        run_id: UUID,
        result: WorkflowStepResult | None,
    ) -> None:
        if result is None or result.status is not WorkflowStepStatus.RUNNING:
            return
        requested = run_id in self._cancellation_requests
        updated = result.model_copy(
            update={
                "status": (
                    WorkflowStepStatus.CANCELLED if requested else WorkflowStepStatus.FAILED
                ),
                "completed_at": self._clock(),
                "error_code": "WORKFLOW_CANCELLED" if requested else "AGENT_RESTARTED",
                "error_message": (
                    "Workflow cancelled"
                    if requested
                    else "Agent restarted while the workflow was running"
                ),
            }
        )
        await self._repository.update_step_result(updated)

    async def _mark_cancelled(self, run: WorkflowRun) -> None:
        if run.status is WorkflowRunStatus.CANCELLED:
            return
        cancelled = run.model_copy(
            update={
                "status": WorkflowRunStatus.CANCELLED,
                "completed_at": self._clock(),
                "error_code": "WORKFLOW_CANCELLED",
                "error_message": "Workflow cancelled",
            }
        )
        await self._repository.update_run(cancelled)
        await self._emit("WORKFLOW_CANCELLED", cancelled)

    async def _mark_failed(
        self,
        run: WorkflowRun,
        exc: Exception,
        *,
        error_code: str | None = None,
    ) -> None:
        if run.status not in ACTIVE_WORKFLOW_RUN_STATUSES:
            return
        failed = run.model_copy(
            update={
                "status": WorkflowRunStatus.FAILED,
                "completed_at": self._clock(),
                "error_code": error_code or _error_code(exc),
                "error_message": str(exc),
            }
        )
        await self._repository.update_run(failed)
        await self._emit("WORKFLOW_FAILED", failed)

    async def _require_run(self, run_id: UUID) -> WorkflowRun:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise WorkflowRunNotFoundError(
                f"Workflow run {run_id} does not exist.", workflow_run_id=str(run_id)
            )
        return run

    async def _emit(
        self,
        event_type: str,
        run: WorkflowRun,
        *,
        step_result: WorkflowStepResult | None = None,
    ) -> None:
        if self._events is None:
            return
        payload: dict[str, Any] = {
            "workflow_run_id": str(run.id),
            "workflow_name": run.workflow_name,
            "workflow_version": run.workflow_version,
            "status": run.status.value,
        }
        if step_result is not None:
            payload.update(
                {
                    "workflow_step_result_id": str(step_result.id),
                    "step_index": step_result.step_index,
                    "action": step_result.action.value,
                    "step_status": step_result.status.value,
                }
            )
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type=event_type,
                source="workflow",
                bench_id=run.bench_id,
                actor=run.owner,
                payload=payload,
            )
        )

    def _schedule_cleanup(self, run: WorkflowRun, completed: asyncio.Task[None]) -> None:
        cleanup = asyncio.create_task(self._cleanup_completed_run(run, completed))
        self._cleanup_tasks[run.id] = cleanup
        cleanup.add_done_callback(lambda finished: self._discard_cleanup(run.id, finished))

    async def _cleanup_completed_run(
        self,
        run: WorkflowRun,
        completed: asyncio.Task[None],
    ) -> None:
        try:
            if completed.cancelled():
                latest = await self._require_run(run.id)
                if run.id in self._cancellation_requests:
                    await self._mark_cancelled(latest)
                else:
                    await self._mark_failed(
                        latest,
                        WorkflowStepFailedError(
                            "Agent restarted while the workflow was running.",
                            reason="agent_restart",
                        ),
                        error_code="AGENT_RESTARTED",
                    )
            await self._release_operation_lock(run)
        finally:
            if self._tasks.get(run.id) is completed:
                self._tasks.pop(run.id, None)
            self._cancellation_requests.discard(run.id)
            self._shutdown_requests.discard(run.id)
            self._released_locks.discard(run.id)

    def _discard_cleanup(self, run_id: UUID, finished: asyncio.Task[None]) -> None:
        if self._cleanup_tasks.get(run_id) is finished:
            self._cleanup_tasks.pop(run_id, None)

    async def _release_operation_lock(self, run: WorkflowRun) -> None:
        if self._operation_locks is None or run.id in self._released_locks:
            return
        await self._operation_locks.release(run.bench_id, run.id)
        self._released_locks.add(run.id)


class WorkflowService:
    def __init__(
        self,
        repository: WorkflowRepository,
        backends: WorkflowBackendResolver,
        reservations: WorkflowReservationAuthorizer,
        runner: WorkflowRunner,
        events: WorkflowEventSink | None = None,
        operation_locks: WorkflowOperationLock | None = None,
        *,
        clock: Clock = workflow_clock,
    ) -> None:
        self._repository = repository
        self._backends = backends
        self._reservations = reservations
        self._runner = runner
        self._events = events
        self._operation_locks = operation_locks
        self._clock = clock

    async def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        return await self._repository.save_definition(definition)

    async def register_yaml(
        self,
        source: str,
        *,
        source_name: str = "<workflow>",
        base_directory: Path | None = None,
    ) -> WorkflowDefinition:
        definition = parse_workflow_yaml(
            source,
            source_name=source_name,
            base_directory=base_directory,
        )
        return await self.register(definition)

    async def get_definition(self, name: str, version: int | None = None) -> WorkflowDefinition:
        definition = await self._repository.get_definition(name, version)
        if definition is None:
            qualifier = f" version {version}" if version is not None else ""
            raise WorkflowNotFoundError(
                f"Workflow {name!r}{qualifier} does not exist.",
                workflow_name=name,
                workflow_version=version,
            )
        return definition

    async def list_definitions(self) -> list[WorkflowDefinition]:
        return await self._repository.list_definitions()

    async def start(
        self,
        name: str,
        *,
        bench_id: str,
        owner: str,
        version: int | None = None,
        inputs: Mapping[str, str] | None = None,
    ) -> WorkflowRun:
        stored_definition = await self.get_definition(name, version)
        definition = resolve_workflow_inputs(stored_definition, inputs)
        try:
            reservation_id = await self._reservations.require_active(bench_id, owner)
        except Exception as exc:
            raise WorkflowReservationRequiredError(
                f"Workflow {name!r} requires an active reservation owned by {owner}.",
                bench_id=bench_id,
                owner=owner,
            ) from exc
        backend = await self._backends.get_backend_for_bench(bench_id)
        bench = await backend.get_bench(bench_id)
        if not bench.online:
            raise WorkflowTargetUnavailableError(
                f"Bench {bench_id} is unavailable.", bench_id=bench_id
            )
        validate_workflow_capabilities(definition, bench)
        run = WorkflowRun(
            workflow_name=definition.name,
            workflow_version=definition.version,
            bench_id=bench_id,
            owner=owner,
            reservation_id=reservation_id,
            created_at=self._clock(),
        )
        if self._operation_locks is not None:
            try:
                await self._operation_locks.acquire(bench_id, run.id, owner)
            except Exception:
                await self._operation_locks.release(bench_id, run.id)
                raise
        created = False
        try:
            await self._repository.create_run(run)
            created = True
            self._runner.schedule(definition, run)
        except Exception as exc:
            if created:
                failed = run.model_copy(
                    update={
                        "status": WorkflowRunStatus.FAILED,
                        "completed_at": self._clock(),
                        "error_code": _error_code(exc),
                        "error_message": str(exc),
                    }
                )
                await self._repository.update_run(failed)
            if self._operation_locks is not None:
                await self._operation_locks.release(bench_id, run.id)
            raise
        return run

    async def get_run(self, run_id: UUID) -> WorkflowRun:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise WorkflowRunNotFoundError(
                f"Workflow run {run_id} does not exist.", workflow_run_id=str(run_id)
            )
        return run

    async def list_step_results(self, run_id: UUID) -> list[WorkflowStepResult]:
        await self.get_run(run_id)
        return await self._repository.list_step_results(run_id)

    async def cancel(self, run_id: UUID, owner: str) -> WorkflowRun:
        run = await self.get_run(run_id)
        if run.owner != owner:
            raise WorkflowOwnerMismatchError(
                f"Workflow run {run_id} belongs to another owner.",
                workflow_run_id=str(run_id),
            )
        if run.status is WorkflowRunStatus.CANCELLED:
            return run
        if run.status not in {WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING}:
            raise WorkflowNotCancellableError(
                f"Workflow run {run_id} cannot be cancelled from {run.status.value}.",
                workflow_run_id=str(run_id),
            )
        requested = run.model_copy(update={"status": WorkflowRunStatus.CANCEL_REQUESTED})
        await self._repository.update_run(requested)
        if self._runner.request_cancel(run_id):
            return requested
        cancelled = requested.model_copy(
            update={
                "status": WorkflowRunStatus.CANCELLED,
                "completed_at": self._clock(),
                "error_code": "WORKFLOW_CANCELLED",
                "error_message": "Workflow cancelled",
            }
        )
        await self._repository.update_run(cancelled)
        if self._operation_locks is not None:
            await self._operation_locks.release(run.bench_id, run.id)
        if self._events is not None:
            await self._events.create(
                EventRecord(
                    timestamp=self._clock(),
                    type="WORKFLOW_CANCELLED",
                    source="workflow",
                    bench_id=run.bench_id,
                    actor=run.owner,
                    payload={
                        "workflow_run_id": str(run.id),
                        "workflow_name": run.workflow_name,
                        "status": WorkflowRunStatus.CANCELLED.value,
                    },
                )
            )
        return cancelled

    async def wait(self, run_id: UUID) -> WorkflowRun:
        return await self._runner.wait(run_id)

    async def cancel_for_expiry(self, run_id: UUID) -> bool:
        run = await self._repository.get_run(run_id)
        if run is None:
            return False
        if run.status in {WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING}:
            await self.cancel(run_id, run.owner)
            return True
        if run.status is WorkflowRunStatus.CANCEL_REQUESTED:
            self._runner.request_cancel(run_id)
            return True
        return False

    async def recover_interrupted(self) -> int:
        recovered = await self._repository.recover_interrupted(self._clock())
        if self._events is not None:
            for run in recovered:
                await self._events.create(
                    EventRecord(
                        timestamp=self._clock(),
                        type="WORKFLOW_FAILED",
                        source="recovery",
                        bench_id=run.bench_id,
                        actor=run.owner,
                        payload={
                            "workflow_run_id": str(run.id),
                            "workflow_name": run.workflow_name,
                            "status": run.status.value,
                            "error_code": run.error_code,
                        },
                    )
                )
        return len(recovered)


def _firmware_input(step: FlashWorkflowStep) -> FirmwareInput:
    try:
        size = step.firmware.stat().st_size
    except OSError as exc:
        raise WorkflowStepFailedError(
            f"Firmware file {step.firmware} is not readable.", firmware=str(step.firmware)
        ) from exc
    if size <= 0:
        raise WorkflowStepFailedError(
            f"Firmware file {step.firmware} is empty.", firmware=str(step.firmware)
        )
    digest = hashlib.sha256()
    try:
        with step.firmware.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise WorkflowStepFailedError(
            f"Firmware file {step.firmware} is not readable.", firmware=str(step.firmware)
        ) from exc
    return FirmwareInput(
        filename=step.firmware.name,
        local_path=step.firmware,
        sha256=digest.hexdigest(),
        size_bytes=size,
        version=step.version,
    )


def _require_target_available(health: TargetHealth) -> None:
    if health.status is TargetHealthStatus.OFFLINE:
        raise WorkflowTargetUnavailableError(
            f"Target {health.bench_id} is unavailable.", bench_id=health.bench_id
        )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, PlatformError):
        return exc.code
    return "WORKFLOW_STEP_FAILED"


_INPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INPUT_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
