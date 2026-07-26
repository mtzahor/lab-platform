from __future__ import annotations

import asyncio
import builtins
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from weakref import WeakValueDictionary

from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.errors import (
    CiSessionConflictError,
    CiSessionNotFoundError,
    InvalidArtifactError,
)
from lab_platform.core.results import build_test_results, render_junit_xml
from lab_platform.models import (
    CI_SESSION_TRANSITIONS,
    ArtifactOwnerType,
    ArtifactReference,
    BenchOperationLock,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    EventRecord,
    Reservation,
    ReservationStatus,
)
from lab_platform.models.workflows import (
    TERMINAL_WORKFLOW_RUN_STATUSES,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
)


class CiSessionRepository(Protocol):
    async def create(
        self,
        session: CiSession,
        *,
        idempotency_key: str | None = None,
        errors: Sequence[str] = (),
    ) -> CiSession: ...

    async def get(self, session_id: UUID) -> CiSession | None: ...

    async def get_by_workflow_launch_idempotency_key(self, key: str) -> CiSession | None: ...

    async def get_by_finalize_idempotency_key(self, key: str) -> CiSession | None: ...

    async def update(
        self,
        session: CiSession,
        *,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None: ...

    async def compare_and_set(
        self,
        session: CiSession,
        *,
        expected_statuses: Iterable[CiSessionStatus] | None,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None: ...

    async def list(
        self,
        *,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> builtins.list[CiSession]: ...

    async def list_stale(
        self,
        *,
        heartbeat_before: datetime,
        now: datetime,
        limit: int = 500,
    ) -> builtins.list[CiSession]: ...

    async def errors(self, session_id: UUID) -> builtins.list[str]: ...

    async def assign_compatible_bench(
        self,
        session_id: UUID,
        request: BenchRequest,
        *,
        now: datetime,
        reservation_id: UUID | None = None,
        owner: str | None = None,
        reservation_idempotency_key: str | None = None,
    ) -> tuple[CiSession, Reservation] | None: ...

    async def attach_workflow_run(
        self,
        session_id: UUID,
        workflow_run_id: UUID,
        *,
        idempotency_key: str,
        started_at: datetime,
    ) -> CiSession | None: ...

    async def mark_finalized(
        self,
        session: CiSession,
        *,
        idempotency_key: str,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None: ...

    async def save_cleanup(
        self,
        session_id: UUID,
        result: CleanupResult,
        *,
        recorded_at: datetime | None = None,
    ) -> CleanupResult: ...

    async def get_cleanup(self, session_id: UUID) -> CleanupResult | None: ...


class CiWorkflowService(Protocol):
    async def get_definition(self, name: str, version: int | None = None) -> WorkflowDefinition: ...

    async def start(
        self,
        name: str,
        *,
        bench_id: str,
        owner: str,
        version: int | None = None,
        inputs: Mapping[str, object] | None = None,
        artifact_resolver: Callable[[ArtifactReference], Path] | None = None,
    ) -> WorkflowRun: ...

    async def get_run(self, run_id: UUID) -> WorkflowRun: ...

    async def list_step_results(self, run_id: UUID) -> list[WorkflowStepResult]: ...

    async def cancel(self, run_id: UUID, owner: str) -> WorkflowRun: ...

    async def wait(self, run_id: UUID) -> WorkflowRun: ...


class CiReservationService(Protocol):
    async def get(self, reservation_id: UUID) -> Reservation: ...

    async def release(self, reservation_id: UUID | str, owner: str) -> Reservation | None: ...

    async def extend(
        self, reservation_id: UUID, owner: str, duration_seconds: int
    ) -> Reservation: ...


class CiOperationLocks(Protocol):
    async def get(self, bench_id: str) -> BenchOperationLock | None: ...

    async def release(self, bench_id: str, operation_id: UUID) -> bool: ...


class CiEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...


class CiCatalog(Protocol):
    def get(self, bench_id: str) -> Any: ...


class CiSessionService:
    """Durable coordinator for reservation, workflow, artifacts, and cleanup."""

    def __init__(
        self,
        repository: CiSessionRepository,
        workflows: CiWorkflowService,
        reservations: CiReservationService,
        operation_locks: CiOperationLocks,
        artifacts: ArtifactService,
        events: CiEventRepository,
        catalog: CiCatalog,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat_interval_seconds: int = 30,
        heartbeat_timeout_seconds: int = 120,
        session_timeout_seconds: int = 3600,
        workflow_timeout_seconds: int = 1800,
        maximum_reservation_seconds: int = 7200,
        cleanup_timeout_seconds: int = 60,
        serial_artifact_max_bytes: int = 50 * 1024 * 1024,
        maintenance_concurrency: int = 16,
        backend_types: Mapping[str, str] | None = None,
    ) -> None:
        if maintenance_concurrency <= 0:
            raise ValueError("CI maintenance concurrency must be positive")
        self._repository = repository
        self._workflows = workflows
        self._reservations = reservations
        self._operation_locks = operation_locks
        self._artifacts = artifacts
        self._events = events
        self._catalog = catalog
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._session_timeout_seconds = session_timeout_seconds
        self._workflow_timeout_seconds = workflow_timeout_seconds
        self._maximum_reservation_seconds = maximum_reservation_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._serial_artifact_max_bytes = serial_artifact_max_bytes
        self._maintenance_semaphore = asyncio.Semaphore(maintenance_concurrency)
        self._backend_types = dict(backend_types or {})
        self._finalizations: dict[UUID, asyncio.Task[CiSession]] = {}
        self._session_guards: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    async def create(
        self,
        *,
        provider: CiProvider,
        external_run_id: str,
        requested_by: str,
        bench_request: BenchRequest,
        repository: str | None = None,
        ref: str | None = None,
        commit_sha: str | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> CiSession:
        if bench_request.reservation_duration_seconds > self._maximum_reservation_seconds:
            raise CiSessionConflictError(
                "Requested CI reservation exceeds the configured maximum.",
                maximum_reservation_seconds=self._maximum_reservation_seconds,
            )
        now = self._clock()
        session = CiSession(
            provider=provider,
            external_run_id=external_run_id,
            repository=repository,
            ref=ref,
            commit_sha=commit_sha,
            actor=actor,
            requested_by=requested_by,
            status=CiSessionStatus.CREATED,
            created_at=now,
            heartbeat_at=now,
            timeout_at=now + timedelta(seconds=self._session_timeout_seconds),
            bench_request=bench_request,
        )
        created = await self._repository.create(session, idempotency_key=idempotency_key)
        if created.id != session.id and created.requested_by != session.requested_by:
            raise CiSessionConflictError(
                "CI session idempotency key belongs to another token owner."
            )
        return await self._initialize_created_session(created, observed_at=now)

    async def _initialize_created_session(
        self,
        session: CiSession,
        *,
        observed_at: datetime,
    ) -> CiSession:
        if session.status not in {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
        }:
            return session
        await self._emit("CI_SESSION_CREATED", session)
        current = session
        if current.status is CiSessionStatus.CREATED:
            waiting = self._transition(current, CiSessionStatus.WAITING_FOR_BENCH)
            persisted = await self._repository.compare_and_set(
                waiting,
                expected_statuses={CiSessionStatus.CREATED},
            )
            current = persisted or await self.get(current.id, synchronize=False)
        if current.status is CiSessionStatus.WAITING_FOR_BENCH:
            await self._emit("CI_SESSION_WAITING_FOR_BENCH", current)
        assigned = await self.try_assign(current.id)
        request = assigned.bench_request or BenchRequest()
        if (
            assigned.status is CiSessionStatus.WAITING_FOR_BENCH
            and request.maximum_wait_seconds == 0
        ):
            return await self._timeout(
                assigned,
                "NO_COMPATIBLE_BENCH",
                observed_at=observed_at,
            )
        return assigned

    async def get(self, session_id: UUID, *, synchronize: bool = True) -> CiSession:
        session = await self._repository.get(session_id)
        if session is None:
            raise CiSessionNotFoundError(
                f"CI session {session_id} does not exist.", ci_session_id=str(session_id)
            )
        if synchronize and session.status is CiSessionStatus.RUNNING:
            return await self._synchronize_workflow(session)
        return session

    async def details(self, session_id: UUID) -> dict[str, object]:
        session = await self.get(session_id)
        cleanup = await self._repository.get_cleanup(session_id)
        payload: dict[str, object] = {
            **session.model_dump(mode="json"),
            "cleanup": cleanup.model_dump(mode="json") if cleanup is not None else None,
            "errors": await self._repository.errors(session_id),
            "heartbeat_interval_seconds": self._heartbeat_interval_seconds,
            "cleanup_timeout_seconds": self._cleanup_timeout_seconds,
        }
        if session.bench_id is not None:
            backend_id = str(getattr(self._catalog.get(session.bench_id), "backend_id", ""))
            backend_type = self._backend_types.get(backend_id)
            if backend_type is not None:
                payload["backend"] = backend_type
        return payload

    async def try_assign(self, session_id: UUID) -> CiSession:
        session = await self.get(session_id, synchronize=False)
        if session.status is CiSessionStatus.RESERVED:
            return session
        if session.status not in {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
        }:
            return session
        request = session.bench_request or BenchRequest()
        assignment = await self._repository.assign_compatible_bench(
            session.id,
            request,
            now=self._clock(),
            owner=session.requested_by,
            reservation_idempotency_key=f"ci-session:{session.id}",
        )
        if assignment is None:
            return session
        assigned, reservation = assignment
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type="CI_BENCH_ASSIGNED",
                source="ci",
                bench_id=assigned.bench_id,
                reservation_id=reservation.id,
                actor=assigned.requested_by,
                payload={"ci_session_id": str(assigned.id)},
                deduplication_key=f"ci-session:{assigned.id}:bench-assigned",
            )
        )
        return assigned

    async def heartbeat(self, session_id: UUID) -> CiSession:
        async with self._session_guard(session_id):
            session = await self.get(session_id, synchronize=False)
            if session.status in {
                CiSessionStatus.SUCCEEDED,
                CiSessionStatus.FAILED,
                CiSessionStatus.CANCELLED,
                CiSessionStatus.TIMED_OUT,
                CiSessionStatus.CLEANUP_PENDING,
                CiSessionStatus.COMPLETED,
            }:
                return session
            now = self._clock()
            updated = session.model_copy(update={"heartbeat_at": now})
            persisted = await self._repository.compare_and_set(
                updated,
                expected_statuses={session.status},
            )
            current = persisted or await self.get(session_id, synchronize=False)
        if persisted is not None:
            await self._renew_reservation(current, now)
        return current

    async def start_workflow(
        self,
        session_id: UUID,
        *,
        workflow_name: str,
        inputs: Mapping[str, object] | None = None,
        version: int | None = None,
        idempotency_key: str | None = None,
    ) -> CiSession:
        session = await self.get(session_id, synchronize=False)
        launch_key = idempotency_key or f"ci-session:{session.id}:workflow"
        replay = await self._repository.get_by_workflow_launch_idempotency_key(launch_key)
        if replay is not None:
            if replay.id != session.id:
                raise CiSessionConflictError("Workflow idempotency key belongs to another session.")
            return replay
        if session.status is not CiSessionStatus.RESERVED or session.bench_id is None:
            raise CiSessionConflictError(
                "CI session must have a reserved bench before launching a workflow.",
                ci_session_id=str(session.id),
                status=session.status.value,
            )
        definition = await self._workflows.get_definition(workflow_name, version)
        self._validate_workflow_labels(session.bench_id, definition)
        artifact_paths = await self._resolve_artifact_inputs(session, inputs or {})

        def artifact_resolver(reference: ArtifactReference) -> Path:
            try:
                return artifact_paths[reference.artifact_id]
            except KeyError as exc:
                raise InvalidArtifactError(
                    "Workflow artifact does not belong to this CI session.",
                    artifact_id=str(reference.artifact_id),
                ) from exc

        run = await self._workflows.start(
            workflow_name,
            bench_id=session.bench_id,
            owner=session.requested_by,
            version=version,
            inputs=inputs,
            artifact_resolver=artifact_resolver,
        )
        try:
            attached = await self._repository.attach_workflow_run(
                session.id,
                run.id,
                idempotency_key=launch_key,
                started_at=self._clock(),
            )
        except BaseException:
            await self._stop_unattached_run(run, session.requested_by)
            raise
        if attached is None:
            await self._cancel_run_if_active(run, session.requested_by)
            raise CiSessionConflictError("CI workflow launch raced with another request.")
        if attached.id != session.id:
            await self._cancel_run_if_active(run, session.requested_by)
            raise CiSessionConflictError("Workflow idempotency key belongs to another CI session.")
        if attached.workflow_run_id != run.id:
            await self._cancel_run_if_active(run, session.requested_by)
            return attached
        await self._emit("CI_SESSION_STARTED", attached)
        return attached

    async def cancel(self, session_id: UUID) -> CiSession:
        session = await self.get(session_id)
        if session.status is CiSessionStatus.COMPLETED:
            return session
        if session.status in {
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }:
            return await self.finalize(session.id)
        if session.status is not CiSessionStatus.CANCEL_REQUESTED:
            requested = session.model_copy(
                update={
                    "status": CiSessionStatus.CANCEL_REQUESTED,
                    "outcome": CiOutcome.CANCELLED,
                }
            )
            persisted = await self._repository.compare_and_set(
                requested,
                expected_statuses={session.status},
            )
            session = persisted or await self.get(session_id, synchronize=False)
        if session.workflow_run_id is not None:
            run = await self._workflows.get_run(session.workflow_run_id)
            await self._cancel_run_if_active(run, session.requested_by)
        await self._emit("CI_SESSION_CANCELLED", session)
        return await self.finalize(session.id)

    async def finalize(
        self,
        session_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> CiSession:
        key = idempotency_key or f"ci-session:{session_id}:finalize"
        replay = await self._repository.get_by_finalize_idempotency_key(key)
        if replay is not None:
            if replay.id != session_id:
                raise CiSessionConflictError(
                    "Finalization idempotency key belongs to another CI session."
                )
            return replay
        task = self._finalizations.get(session_id)
        if task is None:
            task = asyncio.create_task(
                self._finalize_once(session_id, key),
                name=f"ci-finalize-{session_id}",
            )
            self._finalizations[session_id] = task

            def finalization_finished(completed: asyncio.Task[CiSession]) -> None:
                self._finalization_finished(session_id, completed)

            task.add_done_callback(finalization_finished)
        return await asyncio.shield(task)

    async def _finalize_once(self, session_id: UUID, key: str) -> CiSession:
        session = await self.get(session_id)
        if session.status is CiSessionStatus.COMPLETED:
            return session
        prior_errors = await self._repository.errors(session.id)
        session = await self._prepare_cleanup(session)
        errors: list[str] = []
        result = await self._cleanup(session, errors)
        await self._repository.save_cleanup(session.id, result, recorded_at=self._clock())
        cleanup_succeeded = (
            all(
                (
                    result.reservation_released,
                    result.workflow_stopped,
                    result.locks_released,
                    result.serial_closed,
                    result.artifacts_finalized,
                )
            )
            and not result.errors
        )
        outcome = session.outcome
        if not cleanup_succeeded:
            outcome = CiOutcome.INFRASTRUCTURE_ERROR
        completed = session.model_copy(
            update={
                "status": CiSessionStatus.COMPLETED,
                "outcome": outcome,
                "cleanup_status": (
                    CleanupStatus.SUCCEEDED if cleanup_succeeded else CleanupStatus.FAILED
                ),
                "completed_at": self._clock(),
            }
        )
        final_errors = [*prior_errors, *result.errors]
        finalized = await self._repository.mark_finalized(
            completed,
            idempotency_key=key,
            errors=final_errors,
        )
        if finalized is None:
            finalized = await self.get(session.id, synchronize=False)
        await self._emit(
            "CI_CLEANUP_COMPLETED" if cleanup_succeeded else "CI_CLEANUP_FAILED",
            finalized,
            payload={"cleanup": result.model_dump(mode="json")},
        )
        await self._emit("CI_SESSION_FINALIZED", finalized)
        return finalized

    async def process_maintenance(self) -> int:
        now = self._clock()
        heartbeat_before = now - timedelta(seconds=self._heartbeat_timeout_seconds)
        active_or_terminal_statuses = (
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        )
        groups = await asyncio.gather(
            *(
                self._repository.list(status=status, limit=500)
                for status in active_or_terminal_statuses
            ),
            self._repository.list_stale(
                heartbeat_before=heartbeat_before,
                now=now,
                limit=500,
            ),
        )
        sessions = _unique_sessions(tuple(session for group in groups for session in group))

        async def maintain(session: CiSession) -> None:
            await self._maintain_session(session.id, now, heartbeat_before)

        await self._run_bounded(sessions, maintain)
        return len(sessions)

    async def recover_incomplete(self) -> int:
        live_statuses = (
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
        )
        terminal_statuses = (
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        )
        groups = await asyncio.gather(
            *(
                self._repository.list(status=status, limit=500)
                for status in (*live_statuses, *terminal_statuses)
            )
        )
        sessions = _unique_sessions(tuple(session for group in groups for session in group))
        live = set(live_statuses)
        terminal = set(terminal_statuses)

        async def recover(session: CiSession) -> None:
            current = await self.get(session.id, synchronize=False)
            if current.status in live:
                await self._timeout(current, "AGENT_RESTARTED")
            elif current.status in terminal:
                await self.finalize(session.id)

        await self._run_bounded(sessions, recover)
        return len(sessions)

    async def _maintain_session(
        self,
        session_id: UUID,
        now: datetime,
        heartbeat_before: datetime,
    ) -> None:
        session = await self.get(session_id, synchronize=False)
        if session.status is CiSessionStatus.CREATED:
            session = await self._initialize_created_session(session, observed_at=now)
        if session.status is CiSessionStatus.COMPLETED:
            return
        if session.status in {
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }:
            await self.finalize(session.id)
            return
        if session.status is CiSessionStatus.WAITING_FOR_BENCH:
            request = session.bench_request or BenchRequest()
            if session.created_at + timedelta(seconds=request.maximum_wait_seconds) <= now:
                await self._timeout(
                    session,
                    "BENCH_WAIT_TIMEOUT",
                    observed_at=now,
                )
                return
        elif session.status is CiSessionStatus.RUNNING:
            session = await self._synchronize_workflow(session)
            if session.status is not CiSessionStatus.RUNNING:
                await self.finalize(session.id)
                return
            if (
                session.started_at is not None
                and session.started_at + timedelta(seconds=self._workflow_timeout_seconds) <= now
            ):
                await self._timeout(
                    session,
                    "WORKFLOW_TIMEOUT",
                    observed_at=now,
                )
                return
        if session.timeout_at is not None and session.timeout_at <= now:
            await self._timeout(
                session,
                "SESSION_TIMEOUT",
                observed_at=now,
            )
            return
        if (session.heartbeat_at or session.created_at) <= heartbeat_before:
            await self._timeout(
                session,
                "CI_HEARTBEAT_MISSED",
                observed_at=now,
                heartbeat_before=heartbeat_before,
            )
            return
        if session.status is CiSessionStatus.WAITING_FOR_BENCH:
            await self.try_assign(session.id)

    async def _run_bounded(
        self,
        sessions: Sequence[CiSession],
        operation: Callable[[CiSession], Awaitable[None]],
    ) -> None:
        async def run(session: CiSession) -> None:
            async with self._maintenance_semaphore:
                await operation(session)

        await asyncio.gather(*(run(session) for session in sessions))

    async def _synchronize_workflow(self, session: CiSession) -> CiSession:
        if session.workflow_run_id is None:
            return session
        run = await self._workflows.get_run(session.workflow_run_id)
        if run.status not in TERMINAL_WORKFLOW_RUN_STATUSES:
            return session
        status, outcome, event_type = {
            WorkflowRunStatus.SUCCEEDED: (
                CiSessionStatus.SUCCEEDED,
                CiOutcome.SUCCEEDED,
                "CI_SESSION_SUCCEEDED",
            ),
            WorkflowRunStatus.FAILED: (
                CiSessionStatus.FAILED,
                CiOutcome.FAILED,
                "CI_SESSION_FAILED",
            ),
            WorkflowRunStatus.CANCELLED: (
                CiSessionStatus.CANCELLED,
                CiOutcome.CANCELLED,
                "CI_SESSION_CANCELLED",
            ),
        }[run.status]
        updated = session.model_copy(update={"status": status, "outcome": outcome})
        persisted = await self._repository.compare_and_set(
            updated,
            expected_statuses={CiSessionStatus.RUNNING, CiSessionStatus.CANCEL_REQUESTED},
        )
        if persisted is not None:
            await self._emit(event_type, persisted)
            return persisted
        return await self.get(session.id, synchronize=False)

    async def _prepare_cleanup(self, session: CiSession) -> CiSession:
        if session.status is CiSessionStatus.RUNNING:
            session = await self._synchronize_workflow(session)
        if session.outcome is CiOutcome.PENDING:
            session = session.model_copy(update={"outcome": CiOutcome.FAILED})
        pending = session.model_copy(
            update={
                "status": CiSessionStatus.CLEANUP_PENDING,
                "cleanup_status": CleanupStatus.RUNNING,
            }
        )
        persisted = await self._repository.compare_and_set(
            pending,
            expected_statuses={session.status, CiSessionStatus.CLEANUP_PENDING},
        )
        current = persisted or await self.get(session.id, synchronize=False)
        await self._emit("CI_CLEANUP_STARTED", current)
        return current

    async def _cleanup(self, session: CiSession, errors: list[str]) -> CleanupResult:
        workflow_stopped = await self._cleanup_stage(
            lambda: self._stop_workflow(session, errors),
            timeout_seconds=self._cleanup_timeout_seconds * 0.4,
            label="workflow stop",
            errors=errors,
        )
        serial_closed = await self._cleanup_stage(
            lambda: self._serial_resources_closed(session, workflow_stopped, errors),
            timeout_seconds=self._cleanup_timeout_seconds * 0.1,
            label="serial close check",
            errors=errors,
        )
        locks_released = await self._cleanup_stage(
            lambda: self._locks_released(session, workflow_stopped, errors),
            timeout_seconds=self._cleanup_timeout_seconds * 0.1,
            label="operation lock check",
            errors=errors,
        )
        reservation_released = await self._cleanup_stage(
            lambda: self._release_reservation(session, errors),
            timeout_seconds=self._cleanup_timeout_seconds * 0.2,
            label="reservation release",
            errors=errors,
        )
        artifacts_finalized = await self._cleanup_stage(
            lambda: self._finalize_artifacts(session, errors),
            timeout_seconds=self._cleanup_timeout_seconds * 0.2,
            label="artifact finalization",
            errors=errors,
        )
        return CleanupResult(
            reservation_released=reservation_released,
            workflow_stopped=workflow_stopped,
            locks_released=locks_released,
            serial_closed=serial_closed,
            artifacts_finalized=artifacts_finalized,
            errors=errors,
        )

    @staticmethod
    async def _cleanup_stage(
        operation: Callable[[], Awaitable[bool]],
        *,
        timeout_seconds: float,
        label: str,
        errors: list[str],
    ) -> bool:
        try:
            async with asyncio.timeout(max(0.001, timeout_seconds)):
                return await operation()
        except TimeoutError:
            errors.append(f"{label} timed out")
            return False

    async def _stop_workflow(self, session: CiSession, errors: list[str]) -> bool:
        if session.workflow_run_id is None:
            return True
        try:
            run = await self._workflows.get_run(session.workflow_run_id)
            await self._cancel_run_if_active(run, session.requested_by)
            stopped = await self._workflows.wait(run.id)
            return stopped.status in TERMINAL_WORKFLOW_RUN_STATUSES
        except Exception as exc:
            errors.append(f"workflow: {exc}")
            return False

    async def _serial_resources_closed(
        self,
        session: CiSession,
        workflow_stopped: bool,
        errors: list[str],
    ) -> bool:
        if not workflow_stopped:
            return False
        if session.workflow_run_id is None:
            return True
        try:
            steps = await self._workflows.list_step_results(session.workflow_run_id)
            if any(step.error_code == "SERIAL_CLOSE_FAILED" for step in steps):
                errors.append("serial: a serial handle failed to close")
                return False
            return True
        except Exception as exc:
            errors.append(f"serial close check: {exc}")
            return False

    async def _locks_released(
        self,
        session: CiSession,
        workflow_stopped: bool,
        errors: list[str],
    ) -> bool:
        if session.bench_id is None:
            return True
        try:
            lock = await self._operation_locks.get(session.bench_id)
            if (
                workflow_stopped
                and lock is not None
                and session.workflow_run_id is not None
                and lock.operation_id == session.workflow_run_id
            ):
                await self._operation_locks.release(session.bench_id, lock.operation_id)
                lock = await self._operation_locks.get(session.bench_id)
            if lock is not None:
                errors.append(f"operation lock remains on {session.bench_id}")
                return False
            return True
        except Exception as exc:
            errors.append(f"operation lock: {exc}")
            return False

    async def _release_reservation(self, session: CiSession, errors: list[str]) -> bool:
        if session.reservation_id is None:
            return True
        try:
            reservation = await self._reservations.get(session.reservation_id)
            if reservation.status in {
                ReservationStatus.RELEASED,
                ReservationStatus.EXPIRED,
                ReservationStatus.CANCELLED,
            }:
                return True
            await self._reservations.release(reservation.id, session.requested_by)
            return True
        except Exception as exc:
            errors.append(f"reservation: {exc}")
            return False

    async def _finalize_artifacts(self, session: CiSession, errors: list[str]) -> bool:
        try:
            steps: list[WorkflowStepResult] = []
            workflow_name = "hardware-ci"
            workflow_status = "not_run"
            if session.workflow_run_id is not None:
                run = await self._workflows.get_run(session.workflow_run_id)
                workflow_name = run.workflow_name
                workflow_status = run.status.value
                steps = await self._workflows.list_step_results(run.id)
            tests = build_test_results(steps)
            serial_content = await self._workflow_log(session, steps, {"serial_log"})
            if serial_content:
                await self._artifacts.store_bytes(
                    serial_content,
                    owner_type=ArtifactOwnerType.CI_SESSION,
                    owner_id=session.id,
                    name="serial.log",
                    artifact_type="serial_log",
                    content_type="text/plain; charset=utf-8",
                    metadata={"workflow_run_id": str(session.workflow_run_id or "")},
                    idempotency_key=f"ci-session:{session.id}:serial-log",
                )
            flash_content = await self._workflow_log(session, steps, {"flash_log"})
            if flash_content:
                await self._artifacts.store_bytes(
                    flash_content,
                    owner_type=ArtifactOwnerType.CI_SESSION,
                    owner_id=session.id,
                    name="flash.log",
                    artifact_type="flash_log",
                    content_type="text/plain; charset=utf-8",
                    metadata={"workflow_run_id": str(session.workflow_run_id or "")},
                    idempotency_key=f"ci-session:{session.id}:flash-log",
                )
            junit = render_junit_xml(workflow_name, tests)
            await self._artifacts.store_bytes(
                junit.encode("utf-8"),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=session.id,
                name="hardware-results.xml",
                artifact_type="junit",
                content_type="application/xml",
                metadata={"workflow_run_id": str(session.workflow_run_id or "")},
                idempotency_key=f"ci-session:{session.id}:junit",
            )
            summary = json.dumps(
                {
                    "ci_session_id": str(session.id),
                    "workflow_run_id": str(session.workflow_run_id or ""),
                    "workflow_status": workflow_status,
                    "tests": [item.model_dump(mode="json") for item in tests],
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            await self._artifacts.store_bytes(
                summary,
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=session.id,
                name="workflow-summary.json",
                artifact_type="workflow_summary",
                content_type="application/json",
                metadata={"workflow_run_id": str(session.workflow_run_id or "")},
                idempotency_key=f"ci-session:{session.id}:summary",
            )
            return True
        except Exception as exc:
            errors.append(f"artifacts: {exc}")
            return False

    async def _workflow_log(
        self,
        session: CiSession,
        steps: Sequence[WorkflowStepResult],
        artifact_types: set[str],
    ) -> bytes:
        content = bytearray()

        def ensure_capacity(size: int) -> None:
            if len(content) + size > self._serial_artifact_max_bytes:
                raise InvalidArtifactError(
                    "Combined workflow log exceeds the configured artifact size limit.",
                    artifact_types=sorted(artifact_types),
                    maximum_size_bytes=self._serial_artifact_max_bytes,
                )

        def append(chunk: bytes) -> None:
            ensure_capacity(len(chunk))
            content.extend(chunk)

        for step in sorted(steps, key=lambda item: item.step_index):
            found_artifact = False
            for artifact_id in step.artifact_ids:
                record = await self._artifacts.get(artifact_id)
                if (
                    record.owner_type is ArtifactOwnerType.WORKFLOW_RUN
                    and record.owner_id == session.workflow_run_id
                    and record.artifact_type in artifact_types
                ):
                    path = await self._artifacts.content_path(record.id)
                    remaining = self._serial_artifact_max_bytes - len(content)
                    append(await asyncio.to_thread(_read_bounded_log, path, remaining))
                    found_artifact = True
            expected_legacy_type = (
                "flash_log" if step.action is WorkflowAction.FLASH else "serial_log"
            )
            if not found_artifact and expected_legacy_type in artifact_types:
                legacy = _serial_log((step,))
                if legacy:
                    append(legacy.encode("utf-8", errors="replace"))
        return bytes(content)

    async def _resolve_artifact_inputs(
        self,
        session: CiSession,
        inputs: Mapping[str, object],
    ) -> dict[UUID, Path]:
        identifiers = _artifact_ids(inputs.values())
        records: dict[UUID, Path] = {}
        for identifier in identifiers:
            record = await self._artifacts.get(identifier)
            if (
                record.owner_type is not ArtifactOwnerType.CI_SESSION
                or record.owner_id != session.id
            ):
                raise InvalidArtifactError(
                    "Workflow artifact does not belong to this CI session.",
                    artifact_id=str(identifier),
                )
            records[identifier] = await self._artifacts.content_path(identifier)
        return records

    def _validate_workflow_labels(self, bench_id: str, definition: WorkflowDefinition) -> None:
        record = self._catalog.get(bench_id)
        missing = {
            key: value
            for key, value in definition.requirements.labels.items()
            if record.labels.get(key) != value
        }
        if missing:
            raise CiSessionConflictError(
                "Selected bench does not satisfy workflow label requirements.",
                bench_id=bench_id,
                missing_labels=missing,
            )

    async def _renew_reservation(self, session: CiSession, now: datetime) -> None:
        if session.reservation_id is None:
            return
        try:
            reservation = await self._reservations.get(session.reservation_id)
            if reservation.ends_at is None:
                return
            threshold = self._heartbeat_timeout_seconds * 2
            if reservation.ends_at > now + timedelta(seconds=threshold):
                return
            starts_at = reservation.starts_at or reservation.created_at
            maximum_end = starts_at + timedelta(seconds=self._maximum_reservation_seconds)
            remaining = int((maximum_end - reservation.ends_at).total_seconds())
            extension = min(threshold, max(0, remaining))
            if extension > 0:
                await self._reservations.extend(
                    reservation.id,
                    session.requested_by,
                    extension,
                )
        except Exception:
            # A heartbeat should remain observable even if a conflicting future
            # reservation prevents renewal; the timeout worker will clean up.
            return

    async def _timeout(
        self,
        session: CiSession,
        reason: str,
        *,
        observed_at: datetime | None = None,
        heartbeat_before: datetime | None = None,
    ) -> CiSession:
        observed_at = observed_at or self._clock()
        eligible = {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
        }
        terminal = {
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING,
        }
        persisted: CiSession | None = None
        finalization_target: CiSession | None = None
        current = session
        async with self._session_guard(session.id):
            for _attempt in range(3):
                current = await self.get(session.id, synchronize=False)
                if current.status is CiSessionStatus.COMPLETED:
                    return current
                if current.status in terminal:
                    finalization_target = current
                    break
                if current.status not in eligible or not self._timeout_applies(
                    current,
                    reason,
                    observed_at=observed_at,
                    heartbeat_before=heartbeat_before,
                ):
                    return current
                timed_out = current.model_copy(
                    update={
                        "status": CiSessionStatus.TIMED_OUT,
                        "outcome": CiOutcome.TIMED_OUT,
                    }
                )
                persisted = await self._repository.compare_and_set(
                    timed_out,
                    expected_statuses={current.status},
                    errors=[reason],
                )
                if persisted is not None:
                    break
            if persisted is None and finalization_target is None:
                current = await self.get(session.id, synchronize=False)
                if current.status in terminal:
                    finalization_target = current
                else:
                    return current
        if finalization_target is not None:
            return await self.finalize(finalization_target.id)
        assert persisted is not None
        if reason == "CI_HEARTBEAT_MISSED":
            await self._emit(
                "CI_HEARTBEAT_MISSED",
                persisted,
                payload={
                    "heartbeat_at": (
                        persisted.heartbeat_at.isoformat()
                        if persisted.heartbeat_at is not None
                        else None
                    )
                },
            )
        await self._emit(
            "CI_SESSION_TIMED_OUT",
            persisted,
            payload={"reason": reason},
        )
        return await self.finalize(persisted.id)

    def _timeout_applies(
        self,
        session: CiSession,
        reason: str,
        *,
        observed_at: datetime,
        heartbeat_before: datetime | None,
    ) -> bool:
        if reason == "AGENT_RESTARTED":
            return True
        if reason == "NO_COMPATIBLE_BENCH":
            request = session.bench_request or BenchRequest()
            return (
                session.status
                in {
                    CiSessionStatus.CREATED,
                    CiSessionStatus.WAITING_FOR_BENCH,
                }
                and request.maximum_wait_seconds == 0
            )
        if reason == "BENCH_WAIT_TIMEOUT":
            request = session.bench_request or BenchRequest()
            return (
                session.status is CiSessionStatus.WAITING_FOR_BENCH
                and session.created_at + timedelta(seconds=request.maximum_wait_seconds)
                <= observed_at
            )
        if reason == "WORKFLOW_TIMEOUT":
            return (
                session.status is CiSessionStatus.RUNNING
                and session.started_at is not None
                and session.started_at + timedelta(seconds=self._workflow_timeout_seconds)
                <= observed_at
            )
        if reason == "SESSION_TIMEOUT":
            return session.timeout_at is not None and session.timeout_at <= observed_at
        if reason == "CI_HEARTBEAT_MISSED":
            return (
                heartbeat_before is not None
                and (session.heartbeat_at or session.created_at) <= heartbeat_before
            )
        return False

    def _session_guard(self, session_id: UUID) -> asyncio.Lock:
        guard = self._session_guards.get(session_id)
        if guard is None:
            guard = asyncio.Lock()
            self._session_guards[session_id] = guard
        return guard

    def _finalization_finished(
        self,
        session_id: UUID,
        task: asyncio.Task[CiSession],
    ) -> None:
        if self._finalizations.get(session_id) is task:
            self._finalizations.pop(session_id, None)
        if not task.cancelled():
            task.exception()

    async def _cancel_run_if_active(self, run: WorkflowRun, owner: str) -> None:
        if run.status in {
            WorkflowRunStatus.PENDING,
            WorkflowRunStatus.RUNNING,
        }:
            await self._workflows.cancel(run.id, owner)

    async def _stop_unattached_run(self, run: WorkflowRun, owner: str) -> None:
        async def stop() -> None:
            try:
                await self._cancel_run_if_active(run, owner)
                await self._workflows.wait(run.id)
            except Exception:
                # Preserve the attachment failure. Server maintenance still
                # reconciles the workflow's persistent lock and reservation.
                return

        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    def _transition(self, session: CiSession, status: CiSessionStatus) -> CiSession:
        if status not in CI_SESSION_TRANSITIONS.get(session.status, frozenset()):
            raise CiSessionConflictError(
                f"CI session cannot transition from {session.status.value} to {status.value}.",
                ci_session_id=str(session.id),
            )
        return session.model_copy(update={"status": status})

    async def _emit(
        self,
        event_type: str,
        session: CiSession,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        event_payload: dict[str, object] = {
            "ci_session_id": str(session.id),
            "status": session.status.value,
            "outcome": session.outcome.value,
        }
        event_payload.update(payload or {})
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type=event_type,
                source="ci",
                bench_id=session.bench_id,
                reservation_id=session.reservation_id,
                actor=session.requested_by,
                payload=event_payload,
                deduplication_key=f"ci-session:{session.id}:{event_type.lower()}",
            )
        )


def _unique_sessions(sessions: Sequence[CiSession]) -> list[CiSession]:
    unique: dict[UUID, CiSession] = {}
    for session in sessions:
        unique.setdefault(session.id, session)
    return list(unique.values())


def _artifact_ids(values: Iterable[object]) -> set[UUID]:
    identifiers: set[UUID] = set()
    for value in values:
        if isinstance(value, ArtifactReference):
            identifiers.add(value.artifact_id)
        elif isinstance(value, Mapping) and set(value) == {"artifact_id"}:
            try:
                identifiers.add(UUID(str(value["artifact_id"])))
            except (TypeError, ValueError) as exc:
                raise InvalidArtifactError("Workflow artifact ID is invalid.") from exc
    return identifiers


def _serial_log(steps: Iterable[WorkflowStepResult]) -> str:
    lines: list[str] = []
    for step in sorted(steps, key=lambda item: item.step_index):
        raw_lines = step.output.get("lines")
        if not isinstance(raw_lines, list):
            continue
        for raw in raw_lines:
            if isinstance(raw, Mapping) and "text" in raw:
                lines.append(str(raw["text"]))
            elif isinstance(raw, str):
                lines.append(raw)
    return "\n".join(lines) + ("\n" if lines else "")


def _read_bounded_log(path: Path, maximum_size_bytes: int) -> bytes:
    content = bytearray()
    with path.open("rb") as stream:
        while chunk := stream.read(min(1024 * 1024, maximum_size_bytes - len(content) + 1)):
            content.extend(chunk)
            if len(content) > maximum_size_bytes:
                raise InvalidArtifactError(
                    "Combined workflow log exceeds the configured artifact size limit.",
                    maximum_size_bytes=maximum_size_bytes,
                )
    return bytes(content)
