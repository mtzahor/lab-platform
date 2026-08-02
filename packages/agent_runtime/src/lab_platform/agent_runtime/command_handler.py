from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid5

from lab_platform.agent_protocol import (
    CommandAcceptedPayload,
    CommandCancelPayload,
    CommandRejectedPayload,
    CommandRequestPayload,
    OperationEventPayload,
)
from lab_platform.agent_runtime.command_journal import (
    AgentCommandJournalRepository,
    StoredCommandJournalEntry,
)
from lab_platform.agent_runtime.event_buffer import AgentEventBufferRepository
from lab_platform.agent_runtime.leases import ReservationLeaseStore
from lab_platform.control_plane_core.errors import (
    AgentDrainingError,
    BenchAgentMismatchError,
    RemoteCommandExpiredError,
    RemoteCommandNotFoundError,
    RemoteCommandRejectedError,
    ReservationLeaseInvalidError,
)
from lab_platform.core.errors import PlatformError
from lab_platform.models import (
    TERMINAL_REMOTE_COMMAND_STATUSES,
    BufferedEventPriority,
    CommandJournalEntry,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
)

COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
COMMAND_REJECTED = "COMMAND_REJECTED"
OPERATION_STARTED = "OPERATION_STARTED"
OPERATION_PROGRESS = "OPERATION_PROGRESS"
OPERATION_SUCCEEDED = "OPERATION_SUCCEEDED"
OPERATION_FAILED = "OPERATION_FAILED"
OPERATION_CANCELLED = "OPERATION_CANCELLED"

MUTATING_COMMAND_TYPES = frozenset(
    {
        RemoteCommandType.FLASH,
        RemoteCommandType.RESET,
        RemoteCommandType.RUN_WORKFLOW,
        RemoteCommandType.CANCEL_OPERATION,
    }
)

# Workflows declare and validate their own step capabilities. Cancellation and inventory refresh
# do not need a bench capability, but still pass through the local safety port.
REQUIRED_COMMAND_CAPABILITY: Mapping[RemoteCommandType, str | None] = {
    RemoteCommandType.PROBE: "probe",
    RemoteCommandType.FLASH: "firmware",
    RemoteCommandType.RESET: "reset",
    RemoteCommandType.READ_SERIAL: "serial",
    RemoteCommandType.RUN_WORKFLOW: None,
    RemoteCommandType.CANCEL_OPERATION: None,
    RemoteCommandType.REFRESH_INVENTORY: None,
}

Clock: TypeAlias = Callable[[], datetime]
DrainState: TypeAlias = Callable[[], bool]
OperationIdFactory: TypeAlias = Callable[[UUID], UUID]


class CommandProgressReporter(Protocol):
    async def __call__(
        self,
        *,
        progress: int | None = None,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None: ...


class AgentBenchSafetyPort(Protocol):
    """Agent-owned validation of bench state, capability, payload, locks, and policy."""

    async def validate(
        self,
        request: CommandRequestPayload,
        *,
        required_capability: str | None,
    ) -> None: ...


class AgentCommandExecutor(Protocol):
    """Closed, typed local execution port; it intentionally exposes no shell command API."""

    async def execute(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any] | None: ...

    async def cancel(
        self,
        *,
        command_id: UUID,
        local_operation_id: UUID,
        reason: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandHandlingResult:
    journal_entry: CommandJournalEntry | None
    accepted: CommandAcceptedPayload | None
    rejected: CommandRejectedPayload | None
    replayed: bool
    local_operation_id: UUID | None

    @property
    def is_accepted(self) -> bool:
        return self.accepted is not None


@dataclass(frozen=True, slots=True)
class CommandDispatchResult:
    """A durable dispatch decision plus optional background physical execution."""

    initial_result: CommandHandlingResult
    execution: asyncio.Task[CommandHandlingResult] | None

    async def wait(self) -> CommandHandlingResult:
        if self.execution is None:
            return self.initial_result
        return await self.execution


@dataclass(slots=True)
class _ActiveExecution:
    task: asyncio.Task[CommandHandlingResult]
    local_operation_id: UUID
    cancellation_reason: str | None = None


class AgentCommandHandler:
    """Safety-first, crash-idempotent Agent command handler.

    Validation is Agent-local. A command is journaled before ``COMMAND_ACCEPTED`` is buffered,
    and no executor call happens before that acceptance event. Existing journal state is always
    replayed without another physical execution, including after process restart.
    """

    def __init__(
        self,
        *,
        agent_id: UUID,
        journal: AgentCommandJournalRepository,
        leases: ReservationLeaseStore,
        events: AgentEventBufferRepository,
        safety: AgentBenchSafetyPort,
        executor: AgentCommandExecutor,
        clock: Clock | None = None,
        is_draining: DrainState | None = None,
        maximum_clock_skew_seconds: int = 0,
        operation_id_factory: OperationIdFactory | None = None,
    ) -> None:
        if isinstance(maximum_clock_skew_seconds, bool) or maximum_clock_skew_seconds < 0:
            raise ValueError("maximum_clock_skew_seconds must be a non-negative integer")
        if leases.agent_id != agent_id:
            raise ValueError("Reservation lease store belongs to a different Agent")
        self._agent_id = agent_id
        self._journal = journal
        self._leases = leases
        self._events = events
        self._safety = safety
        self._executor = executor
        self._clock = clock or _utc_now
        self._drain_state = is_draining
        self._locally_draining = False
        self._maximum_clock_skew_seconds = maximum_clock_skew_seconds
        self._operation_id_factory = operation_id_factory or _stable_operation_id
        self._active: dict[UUID, _ActiveExecution] = {}
        self._active_lock = asyncio.Lock()

    @property
    def agent_id(self) -> UUID:
        return self._agent_id

    @property
    def draining(self) -> bool:
        return self._locally_draining or (self._drain_state is not None and self._drain_state())

    def set_draining(self, draining: bool) -> None:
        if not isinstance(draining, bool):
            raise TypeError("draining must be a boolean")
        self._locally_draining = draining

    async def handle(self, request: CommandRequestPayload) -> CommandHandlingResult:
        """Preserve the original await-to-terminal API for direct local callers."""

        dispatch = await self.dispatch(request)
        return await dispatch.wait()

    async def dispatch(self, request: CommandRequestPayload) -> CommandDispatchResult:
        """Durably decide a command before starting its background physical execution.

        Once this method returns, a new accepted command is journaled, its COMMAND_ACCEPTED event
        is buffered, and its execution task is registered for cancellation. A transport may then
        acknowledge the incoming sequence and continue reading without waiting for the operation.
        """

        if not isinstance(request, CommandRequestPayload):
            raise TypeError("request must be a CommandRequestPayload")

        # A durable replay wins over present-time state such as expiry or draining. Retrying a
        # command must resend its stored state, never re-evaluate it into a second execution.
        try:
            replay = await self._journal.find_replay(request)
        except PlatformError as exc:
            rejected = await self._reject(request.command.id, exc)
            return CommandDispatchResult(initial_result=rejected, execution=None)
        if replay is not None:
            replayed = await self._replay(replay)
            return CommandDispatchResult(initial_result=replayed, execution=None)

        try:
            await self._validate_new(request)
        except PlatformError as exc:
            rejected = await self._reject(request.command.id, exc)
            return CommandDispatchResult(initial_result=rejected, execution=None)
        except (TypeError, ValueError) as exc:
            rejected = await self._reject(
                request.command.id,
                RemoteCommandRejectedError(
                    "Command payload or local safety validation failed.",
                    validation_error=type(exc).__name__,
                ),
            )
            return CommandDispatchResult(initial_result=rejected, execution=None)

        try:
            creation = await self._journal.create_or_replay(
                request,
                received_at=self._now(),
            )
        except PlatformError as exc:
            rejected = await self._reject(request.command.id, exc)
            return CommandDispatchResult(initial_result=rejected, execution=None)
        if creation.replayed:
            replayed = await self._replay(creation.record)
            return CommandDispatchResult(initial_result=replayed, execution=None)

        entry = creation.record.entry
        local_operation_id = self._operation_id_factory(entry.command_id)
        accepted = await self._emit_accepted(entry, local_operation_id=local_operation_id)
        initial = CommandHandlingResult(
            journal_entry=entry,
            accepted=accepted,
            rejected=None,
            replayed=False,
            local_operation_id=local_operation_id,
        )
        async with self._active_lock:
            execution = asyncio.create_task(
                self._execute(
                    request.command,
                    entry=entry,
                    accepted=accepted,
                    local_operation_id=local_operation_id,
                ),
                name=f"agent-command-execution-{entry.command_id}",
            )
            self._active[entry.command_id] = _ActiveExecution(
                task=execution,
                local_operation_id=local_operation_id,
            )
        return CommandDispatchResult(initial_result=initial, execution=execution)

    async def cancel(self, payload: CommandCancelPayload) -> CommandJournalEntry:
        if not isinstance(payload, CommandCancelPayload):
            raise TypeError("payload must be a CommandCancelPayload")
        record = await self._journal.get(payload.command_id)
        if record is None:
            raise RemoteCommandNotFoundError(
                "Command does not exist in the Agent journal.",
                command_id=str(payload.command_id),
            )
        if record.entry.status in TERMINAL_REMOTE_COMMAND_STATUSES:
            return record.entry

        operation_id = self._operation_id_factory(payload.command_id)
        async with self._active_lock:
            active = self._active.get(payload.command_id)
            if active is not None:
                active.cancellation_reason = payload.reason

        await self._executor.cancel(
            command_id=payload.command_id,
            local_operation_id=operation_id,
            reason=payload.reason,
        )
        if active is not None and not active.task.done():
            active.task.cancel()
            try:
                result = await active.task
            except asyncio.CancelledError:  # pragma: no cover - handler consumes cancellation
                pass
            else:
                if result.journal_entry is not None:
                    return result.journal_entry

        completed = await self._journal.complete(
            payload.command_id,
            status=RemoteCommandStatus.CANCELLED,
            completed_at=self._now_after(record.entry),
            error_message=payload.reason,
        )
        await self._emit_terminal(completed.entry, local_operation_id=operation_id)
        async with self._active_lock:
            if active is not None and self._active.get(payload.command_id) is active:
                self._active.pop(payload.command_id, None)
        return completed.entry

    async def _validate_new(self, request: CommandRequestPayload) -> None:
        command = request.command
        if command.agent_id != self._agent_id:
            raise BenchAgentMismatchError(
                "Command was addressed to a different Agent.",
                expected_agent_id=str(self._agent_id),
                received_agent_id=str(command.agent_id),
                bench_id=command.bench_id,
            )
        if command.status not in {
            RemoteCommandStatus.CREATED,
            RemoteCommandStatus.QUEUED,
            RemoteCommandStatus.DISPATCHED,
        }:
            raise RemoteCommandRejectedError(
                "Command request carries an invalid dispatch status.",
                command_status=command.status.value,
            )
        observed = self._now()
        skew = timedelta(seconds=self._maximum_clock_skew_seconds)
        if observed > command.expires_at + skew:
            raise RemoteCommandExpiredError(
                "Remote command has expired.",
                command_id=str(command.id),
                expires_at=command.expires_at.isoformat(),
            )
        if self.draining and command.command_type is not RemoteCommandType.CANCEL_OPERATION:
            raise AgentDrainingError(
                "Agent is draining and cannot accept new work.",
                agent_id=str(self._agent_id),
            )
        required_capability = REQUIRED_COMMAND_CAPABILITY.get(command.command_type)
        if command.command_type not in REQUIRED_COMMAND_CAPABILITY:
            raise RemoteCommandRejectedError(
                "Remote command type is not supported by this Agent.",
                command_type=str(command.command_type),
            )

        if command.command_type in MUTATING_COMMAND_TYPES and command.reservation_id is None:
            raise ReservationLeaseInvalidError(
                "Mutating remote command requires a reservation lease.",
                command_id=str(command.id),
                bench_id=command.bench_id,
            )
        if command.reservation_id is not None:
            assert command.lease_version is not None
            if request.reservation_lease is None:  # protected by CommandRequestPayload validation
                raise ReservationLeaseInvalidError(
                    "Reserved command is missing its reservation lease.",
                    command_id=str(command.id),
                )
            stored = await self._leases.validate(
                agent_id=command.agent_id,
                reservation_id=command.reservation_id,
                bench_id=command.bench_id,
                lease_version=command.lease_version,
                observed_at=observed,
                maximum_clock_skew_seconds=self._maximum_clock_skew_seconds,
            )
            if stored != request.reservation_lease:
                raise ReservationLeaseInvalidError(
                    "Command reservation lease differs from the Agent's stored lease.",
                    command_id=str(command.id),
                    bench_id=command.bench_id,
                    lease_version=command.lease_version,
                )

        await self._safety.validate(
            request,
            required_capability=required_capability,
        )

    async def _execute(
        self,
        command: RemoteCommand,
        *,
        entry: CommandJournalEntry,
        accepted: CommandAcceptedPayload,
        local_operation_id: UUID,
    ) -> CommandHandlingResult:
        try:
            running = await self._journal.mark_running(
                command.id,
                started_at=self._now_after(entry),
            )
            if running.entry.status in TERMINAL_REMOTE_COMMAND_STATUSES:
                await self._emit_terminal(running.entry, local_operation_id=local_operation_id)
                return CommandHandlingResult(
                    journal_entry=running.entry,
                    accepted=accepted,
                    rejected=None,
                    replayed=True,
                    local_operation_id=local_operation_id,
                )
            await self._emit_operation(
                OPERATION_STARTED,
                OperationEventPayload(
                    command_id=command.id,
                    local_operation_id=local_operation_id,
                    occurred_at=running.entry.started_at or self._now(),
                    progress=0,
                    message="Command execution started.",
                ),
                priority=BufferedEventPriority.STATE,
            )

            async def report_progress(
                *,
                progress: int | None = None,
                message: str | None = None,
                result: Mapping[str, Any] | None = None,
            ) -> None:
                await self._emit_operation(
                    OPERATION_PROGRESS,
                    OperationEventPayload(
                        command_id=command.id,
                        local_operation_id=local_operation_id,
                        occurred_at=self._now(),
                        progress=progress,
                        message=message,
                        result=dict(result) if result is not None else None,
                    ),
                    priority=BufferedEventPriority.PROGRESS,
                    coalesce_key=f"command:{command.id}:progress",
                )

            raw_result = await self._executor.execute(
                command,
                local_operation_id=local_operation_id,
                report_progress=report_progress,
            )
            if raw_result is not None and not isinstance(raw_result, Mapping):
                raise RemoteCommandRejectedError(
                    "Local executor returned an invalid command result.",
                    result_type=type(raw_result).__name__,
                )
            result = dict(raw_result) if raw_result is not None else {}
            completed = await self._journal.complete(
                command.id,
                status=RemoteCommandStatus.SUCCEEDED,
                completed_at=self._now_after(running.entry),
                result=result,
            )
            await self._emit_terminal(completed.entry, local_operation_id=local_operation_id)
            return CommandHandlingResult(
                journal_entry=completed.entry,
                accepted=accepted,
                rejected=None,
                replayed=False,
                local_operation_id=local_operation_id,
            )
        except asyncio.CancelledError:
            async with self._active_lock:
                active = self._active.get(command.id)
                cancellation_reason = active.cancellation_reason if active is not None else None
            latest = await self._journal.get(command.id)
            basis = latest.entry if latest is not None else entry
            completed = await self._journal.complete(
                command.id,
                status=RemoteCommandStatus.CANCELLED,
                completed_at=self._now_after(basis),
                error_message=cancellation_reason,
            )
            await self._emit_terminal(completed.entry, local_operation_id=local_operation_id)
            return CommandHandlingResult(
                journal_entry=completed.entry,
                accepted=accepted,
                rejected=None,
                replayed=False,
                local_operation_id=local_operation_id,
            )
        except PlatformError as exc:
            failed_entry = await self._fail_execution(
                command.id,
                entry=entry,
                error_code=exc.code,
                error_message=exc.message,
                local_operation_id=local_operation_id,
            )
            return CommandHandlingResult(
                journal_entry=failed_entry,
                accepted=accepted,
                rejected=None,
                replayed=False,
                local_operation_id=local_operation_id,
            )
        except Exception:
            # Executor implementation details and exception text are deliberately not exposed to
            # the control plane. Callers receive a stable Phase-5 error code and safe message.
            failed_entry = await self._fail_execution(
                command.id,
                entry=entry,
                error_code=RemoteCommandRejectedError.code,
                error_message="Local command execution failed.",
                local_operation_id=local_operation_id,
            )
            return CommandHandlingResult(
                journal_entry=failed_entry,
                accepted=accepted,
                rejected=None,
                replayed=False,
                local_operation_id=local_operation_id,
            )
        finally:
            current_task = asyncio.current_task()
            async with self._active_lock:
                active = self._active.get(command.id)
                if active is not None and active.task is current_task:
                    self._active.pop(command.id, None)

    async def _fail_execution(
        self,
        command_id: UUID,
        *,
        entry: CommandJournalEntry,
        error_code: str,
        error_message: str,
        local_operation_id: UUID,
    ) -> CommandJournalEntry:
        latest = await self._journal.get(command_id)
        basis = latest.entry if latest is not None else entry
        completed = await self._journal.complete(
            command_id,
            status=RemoteCommandStatus.FAILED,
            completed_at=self._now_after(basis),
            error_code=error_code,
            error_message=error_message,
        )
        await self._emit_terminal(completed.entry, local_operation_id=local_operation_id)
        return completed.entry

    async def _replay(self, record: StoredCommandJournalEntry) -> CommandHandlingResult:
        operation_id = self._operation_id_factory(record.entry.command_id)
        if record.entry.status is RemoteCommandStatus.EXPIRED:
            rejected = CommandRejectedPayload(
                command_id=record.entry.command_id,
                rejected_at=record.entry.completed_at or self._now(),
                error_code=record.entry.error_code or RemoteCommandExpiredError.code,
                error_message=record.entry.error_message or "Remote command has expired.",
            )
            await self._events.append(
                COMMAND_REJECTED,
                rejected.model_dump(mode="json"),
                priority=BufferedEventPriority.FAILURE,
                coalesce_key=None,
            )
            await self._emit_terminal(record.entry, local_operation_id=operation_id)
            return CommandHandlingResult(
                journal_entry=record.entry,
                accepted=None,
                rejected=rejected,
                replayed=True,
                local_operation_id=operation_id,
            )
        accepted = await self._emit_accepted(record.entry, local_operation_id=operation_id)
        if record.entry.status in TERMINAL_REMOTE_COMMAND_STATUSES:
            await self._emit_terminal(record.entry, local_operation_id=operation_id)
        elif record.entry.status is RemoteCommandStatus.RUNNING:
            await self._emit_operation(
                OPERATION_STARTED,
                OperationEventPayload(
                    command_id=record.entry.command_id,
                    local_operation_id=operation_id,
                    occurred_at=record.entry.started_at or record.entry.received_at,
                    progress=0,
                    message="Stored command is running.",
                ),
                priority=BufferedEventPriority.STATE,
            )
        return CommandHandlingResult(
            journal_entry=record.entry,
            accepted=accepted,
            rejected=None,
            replayed=True,
            local_operation_id=operation_id,
        )

    async def _reject(
        self,
        command_id: UUID,
        error: PlatformError,
    ) -> CommandHandlingResult:
        rejected = CommandRejectedPayload(
            command_id=command_id,
            rejected_at=self._now(),
            error_code=error.code,
            error_message=error.message,
        )
        await self._events.append(
            COMMAND_REJECTED,
            rejected.model_dump(mode="json"),
            priority=BufferedEventPriority.FAILURE,
            coalesce_key=None,
        )
        return CommandHandlingResult(
            journal_entry=None,
            accepted=None,
            rejected=rejected,
            replayed=False,
            local_operation_id=None,
        )

    async def _emit_accepted(
        self,
        entry: CommandJournalEntry,
        *,
        local_operation_id: UUID,
    ) -> CommandAcceptedPayload:
        accepted = CommandAcceptedPayload(
            command_id=entry.command_id,
            accepted_at=entry.received_at,
            journal_status=entry.status,
            local_operation_id=local_operation_id,
        )
        await self._events.append(
            COMMAND_ACCEPTED,
            accepted.model_dump(mode="json"),
            priority=BufferedEventPriority.STATE,
            coalesce_key=None,
        )
        return accepted

    async def _emit_terminal(
        self,
        entry: CommandJournalEntry,
        *,
        local_operation_id: UUID,
    ) -> None:
        event_type = {
            RemoteCommandStatus.SUCCEEDED: OPERATION_SUCCEEDED,
            RemoteCommandStatus.FAILED: OPERATION_FAILED,
            RemoteCommandStatus.CANCELLED: OPERATION_CANCELLED,
            RemoteCommandStatus.EXPIRED: OPERATION_FAILED,
        }[entry.status]
        payload = OperationEventPayload(
            command_id=entry.command_id,
            local_operation_id=local_operation_id,
            occurred_at=entry.completed_at or self._now(),
            progress=100 if entry.status is RemoteCommandStatus.SUCCEEDED else None,
            result=entry.result,
            error_code=entry.error_code,
            error_message=entry.error_message,
            message=(
                "Command execution succeeded."
                if entry.status is RemoteCommandStatus.SUCCEEDED
                else entry.error_message
            ),
        )
        await self._emit_operation(
            event_type,
            payload,
            priority=(
                BufferedEventPriority.TERMINAL
                if entry.status in {RemoteCommandStatus.SUCCEEDED, RemoteCommandStatus.CANCELLED}
                else BufferedEventPriority.FAILURE
            ),
        )

    async def _emit_operation(
        self,
        event_type: str,
        payload: OperationEventPayload,
        *,
        priority: BufferedEventPriority,
        coalesce_key: str | None = None,
    ) -> None:
        await self._events.append(
            event_type,
            payload.model_dump(mode="json"),
            priority=priority,
            coalesce_key=coalesce_key,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Agent command clock must return timezone-aware timestamps")
        return value.astimezone(UTC)

    def _now_after(self, entry: CommandJournalEntry) -> datetime:
        lower_bound = entry.started_at or entry.received_at
        return max(self._now(), lower_bound)


def _stable_operation_id(command_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"lab-platform:agent-operation:{command_id}")


def _utc_now() -> datetime:
    return datetime.now(UTC)
