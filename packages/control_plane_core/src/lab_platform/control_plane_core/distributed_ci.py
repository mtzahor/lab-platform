from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowDispatch,
    DistributedWorkflowRequest,
)
from lab_platform.core.errors import CiSessionConflictError, CiSessionNotFoundError
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    ArtifactReference,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    DistributedCiWorkflowBinding,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    WorkflowDefinition,
)

_SESSION_TERMINAL_STATUSES = frozenset(
    {
        CiSessionStatus.SUCCEEDED,
        CiSessionStatus.FAILED,
        CiSessionStatus.CANCELLED,
        CiSessionStatus.TIMED_OUT,
    }
)
_OPERATION_TERMINAL_STATUSES = frozenset(
    {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }
)
_RESERVATION_TERMINAL_STATES = frozenset(
    {
        ReservationLeaseState.RELEASED,
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.REVOKED,
    }
)


@dataclass(frozen=True, slots=True)
class DistributedCiCreateRequest:
    """Agent-agnostic metadata for one durable central CI session."""

    provider: CiProvider
    external_run_id: str
    requested_by: str
    bench_request: BenchRequest = field(default_factory=BenchRequest)
    repository: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    actor: str | None = None
    idempotency_key: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DistributedCiStartRequest:
    """Workflow and optional Agent metadata filters for a queued CI session."""

    workflow_name: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    workflow_version: int | None = None
    location: str | None = None
    agent_labels: Mapping[str, str] = field(default_factory=dict)
    lease_ttl_seconds: int | None = None
    command_timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DistributedCiMaintenanceResult:
    examined: int = 0
    synchronized: int = 0
    timed_out: int = 0
    finalized: int = 0
    cleanup_pending: int = 0


class DistributedCiSessionRepository(Protocol):
    async def create(
        self,
        session: CiSession,
        *,
        idempotency_key: str | None = None,
        errors: Sequence[str] = (),
    ) -> CiSession: ...

    async def get(self, session_id: UUID) -> CiSession | None: ...

    async def get_by_idempotency_key(self, key: str) -> CiSession | None: ...

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

    async def append_error(self, session_id: UUID, error: str) -> bool: ...

    async def attach_distributed_workflow(
        self,
        session_id: UUID,
        remote_command_id: UUID,
        *,
        operation_id: UUID,
        agent_id: UUID,
        bench_id: str,
        reservation_id: UUID,
        workflow_name: str,
        workflow_version: int,
        idempotency_key: str,
        request_fingerprint: str,
        started_at: datetime,
    ) -> CiSession | None: ...

    async def get_distributed_workflow(
        self,
        session_id: UUID,
    ) -> DistributedCiWorkflowBinding | None: ...

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


class DistributedCiWorkflowCatalog(Protocol):
    async def get_definition(
        self,
        name: str,
        version: int | None = None,
    ) -> WorkflowDefinition | None: ...


class DistributedCiWorkflowCoordinator(Protocol):
    async def run(self, request: DistributedWorkflowRequest) -> DistributedWorkflowDispatch: ...


class DistributedCiRemoteWorkRepository(Protocol):
    async def get_command(self, command_id: UUID) -> RemoteCommand | None: ...

    async def get_operation_for_command(self, command_id: UUID) -> DistributedOperation | None: ...


class DistributedCiReservationService(Protocol):
    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease: ...

    async def renew(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
        lease_ttl_seconds: int | None = None,
    ) -> CoordinatedReservationLease: ...

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
    ) -> CoordinatedReservationLease: ...


class DistributedCiCommandService(Protocol):
    async def request_cancel(
        self,
        command_id: UUID,
        *,
        reason: str | None = None,
    ) -> RemoteCommand: ...


class DistributedCiArtifactRepository(Protocol):
    async def list(
        self,
        *,
        agent_id: UUID | None = None,
        command_id: UUID | None = None,
        limit: int = 500,
    ) -> list[RemoteArtifactMetadata]: ...


class DistributedCiArtifactUploadRequester(Protocol):
    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> object: ...


class DistributedCiSessionService:
    """Durable CI orchestration over centrally selected remote workflows.

    A CI session never names an Agent.  ``BenchRequest`` constraints are mapped
    into a ``DistributedWorkflowRequest`` and the workflow coordinator chooses
    and fences the route.  A durable binding table links each session to the
    remote command and operation without overloading Phase 4's local-workflow
    foreign key.
    """

    def __init__(
        self,
        repository: DistributedCiSessionRepository,
        workflow_catalog: DistributedCiWorkflowCatalog,
        workflows: DistributedCiWorkflowCoordinator,
        remote_work: DistributedCiRemoteWorkRepository,
        reservations: DistributedCiReservationService,
        commands: DistributedCiCommandService,
        artifacts: DistributedCiArtifactRepository,
        artifact_uploads: DistributedCiArtifactUploadRequester,
        *,
        clock: Callable[[], datetime] | None = None,
        session_timeout_seconds: int = 3_600,
        maximum_session_timeout_seconds: int = 86_400,
        heartbeat_timeout_seconds: int = 300,
        lease_renewal_threshold_seconds: int = 120,
        artifact_finalization_timeout_seconds: int = 300,
        maintenance_concurrency: int = 32,
        maintenance_batch_size: int = 1_000,
        lock_shards: int = 64,
    ) -> None:
        for value, name in (
            (session_timeout_seconds, "session_timeout_seconds"),
            (maximum_session_timeout_seconds, "maximum_session_timeout_seconds"),
            (heartbeat_timeout_seconds, "heartbeat_timeout_seconds"),
            (lease_renewal_threshold_seconds, "lease_renewal_threshold_seconds"),
            (
                artifact_finalization_timeout_seconds,
                "artifact_finalization_timeout_seconds",
            ),
            (maintenance_concurrency, "maintenance_concurrency"),
            (maintenance_batch_size, "maintenance_batch_size"),
            (lock_shards, "lock_shards"),
        ):
            _require_positive_integer(value, field=name)
        if session_timeout_seconds > maximum_session_timeout_seconds:
            raise ValueError("Default CI timeout cannot exceed the configured maximum")
        self._repository = repository
        self._workflow_catalog = workflow_catalog
        self._workflows = workflows
        self._remote_work = remote_work
        self._reservations = reservations
        self._commands = commands
        self._artifacts = artifacts
        self._artifact_uploads = artifact_uploads
        self._clock = clock or _utc_now
        self._session_timeout_seconds = session_timeout_seconds
        self._maximum_session_timeout_seconds = maximum_session_timeout_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._lease_renewal_threshold_seconds = lease_renewal_threshold_seconds
        self._artifact_finalization_timeout = timedelta(
            seconds=artifact_finalization_timeout_seconds
        )
        self._maintenance_concurrency = maintenance_concurrency
        self._maintenance_batch_size = maintenance_batch_size
        self._locks = tuple(asyncio.Lock() for _ in range(lock_shards))

    async def create(self, request: DistributedCiCreateRequest) -> CiSession:
        timeout_seconds = (
            self._session_timeout_seconds
            if request.timeout_seconds is None
            else request.timeout_seconds
        )
        _require_positive_integer(timeout_seconds, field="timeout_seconds")
        if timeout_seconds > self._maximum_session_timeout_seconds:
            raise CiSessionConflictError(
                "Requested CI session timeout exceeds the configured maximum.",
                maximum_session_timeout_seconds=self._maximum_session_timeout_seconds,
            )
        now = self._now()
        session = CiSession(
            provider=request.provider,
            external_run_id=request.external_run_id,
            repository=request.repository,
            ref=request.ref,
            commit_sha=request.commit_sha,
            actor=request.actor,
            requested_by=request.requested_by,
            status=CiSessionStatus.WAITING_FOR_BENCH,
            created_at=now,
            heartbeat_at=now,
            timeout_at=now + timedelta(seconds=timeout_seconds),
            bench_request=request.bench_request,
        )
        idempotency_key = _require_text(
            request.idempotency_key or _default_create_key(session),
            field="idempotency_key",
            maximum_length=500,
        )
        created = await self._repository.create(session, idempotency_key=idempotency_key)
        if created.id != session.id:
            _require_same_creation(created, session, idempotency_key=idempotency_key)
        return created

    async def list(
        self,
        *,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> list[CiSession]:
        _require_positive_integer(limit, field="limit")
        return await self._repository.list(status=status, provider=provider, limit=limit)

    async def get(self, session_id: UUID, *, synchronize: bool = True) -> CiSession:
        session = await self._require_session(session_id)
        if synchronize:
            session = await self._synchronize(session)
        return session

    async def details(self, session_id: UUID) -> dict[str, object]:
        session = await self.get(session_id)
        cleanup, errors = await asyncio.gather(
            self._repository.get_cleanup(session_id),
            self._repository.errors(session_id),
        )
        return {
            **session.model_dump(mode="json"),
            "cleanup": cleanup.model_dump(mode="json") if cleanup is not None else None,
            "errors": errors,
            "distributed": True,
        }

    async def start(
        self,
        session_id: UUID,
        request: DistributedCiStartRequest,
    ) -> CiSession:
        async with self._session_lock(session_id):
            session = await self._require_session(session_id)
            existing_binding = await self._repository.get_distributed_workflow(session.id)
            if existing_binding is not None:
                self._validate_started_workflow(existing_binding, request)
                return await self._synchronize(session)
            if session.status not in {
                CiSessionStatus.CREATED,
                CiSessionStatus.WAITING_FOR_BENCH,
            }:
                raise CiSessionConflictError(
                    "CI session cannot start a workflow in its current state.",
                    ci_session_id=str(session.id),
                    status=session.status.value,
                )
            definition = await self._workflow_catalog.get_definition(
                request.workflow_name,
                request.workflow_version,
            )
            if definition is None:
                raise WorkflowInvalidError(
                    f"Workflow {request.workflow_name!r} does not exist.",
                    workflow_name=request.workflow_name,
                    workflow_version=request.workflow_version,
                )
            bench_request = session.bench_request or BenchRequest()
            routed_definition = _with_ci_capabilities(definition, bench_request)
            command_timeout = (
                self._session_timeout_seconds
                if request.command_timeout_seconds is None
                else request.command_timeout_seconds
            )
            _require_positive_integer(
                command_timeout,
                field="command_timeout_seconds",
            )
            dispatch = await self._workflows.run(
                DistributedWorkflowRequest(
                    definition=routed_definition,
                    owner=session.requested_by,
                    idempotency_key=_workflow_launch_key(session.id),
                    inputs=dict(request.inputs),
                    bench_id=bench_request.explicit_bench_id,
                    kind=_requested_kind(bench_request),
                    location=(
                        _require_text(request.location, field="location", maximum_length=200)
                        if request.location is not None
                        else None
                    ),
                    preferred_location=bench_request.preferred_location,
                    bench_labels=bench_request.required_labels,
                    preferred_bench_labels=bench_request.preferred_labels,
                    agent_labels=_merge_required_labels(
                        bench_request.required_agent_labels,
                        request.agent_labels,
                    ),
                    reservation_duration_seconds=(bench_request.reservation_duration_seconds),
                    lease_ttl_seconds=request.lease_ttl_seconds,
                    command_timeout_seconds=command_timeout,
                    manage_reservation_lifecycle=False,
                )
            )
            attached = await self._repository.attach_distributed_workflow(
                session.id,
                dispatch.command.id,
                operation_id=dispatch.operation.id,
                agent_id=dispatch.agent.id,
                bench_id=dispatch.bench.id,
                reservation_id=dispatch.reservation.reservation.id,
                workflow_name=dispatch.definition.name,
                workflow_version=dispatch.definition.version,
                idempotency_key=_workflow_launch_key(session.id),
                request_fingerprint=_dispatch_fingerprint(dispatch),
                started_at=self._now(),
            )
            current = attached or await self._require_session(session.id)
            binding = await self._repository.get_distributed_workflow(session.id)
            if (
                binding is None
                or binding.remote_command_id != dispatch.command.id
                or binding.operation_id != dispatch.operation.id
                or current.bench_id != dispatch.bench.id
                or current.reservation_id != dispatch.reservation.reservation.id
            ):
                raise CiSessionConflictError(
                    "CI workflow launch raced with different distributed work.",
                    ci_session_id=str(session.id),
                )
            return await self._synchronize(current)

    async def heartbeat(self, session_id: UUID) -> CiSession:
        async with self._session_lock(session_id):
            session = await self._synchronize(await self._require_session(session_id))
            if session.status is CiSessionStatus.COMPLETED:
                return session
            now = self._now()
            candidate = session.model_copy(update={"heartbeat_at": now})
            persisted = await self._repository.compare_and_set(
                candidate,
                expected_statuses={session.status},
            )
            current = persisted or await self._require_session(session.id)
            await self._renew_if_needed(current, now)
            return current

    async def cancel(self, session_id: UUID) -> CiSession:
        async with self._session_lock(session_id):
            session = await self._synchronize(await self._require_session(session_id))
            if session.status is CiSessionStatus.COMPLETED:
                return session
            if session.status in _SESSION_TERMINAL_STATUSES or (
                session.status is CiSessionStatus.CLEANUP_PENDING
            ):
                return await self._cleanup_locked(session)
            binding = await self._repository.get_distributed_workflow(session.id)
            if binding is None:
                cancelled = session.model_copy(
                    update={
                        "status": CiSessionStatus.CANCELLED,
                        "outcome": CiOutcome.CANCELLED,
                    }
                )
                session = await self._compare_or_reload(cancelled, {session.status})
                return await self._cleanup_locked(session)
            if session.status is not CiSessionStatus.CANCEL_REQUESTED:
                requested = session.model_copy(
                    update={
                        "status": CiSessionStatus.CANCEL_REQUESTED,
                        "outcome": CiOutcome.CANCELLED,
                    }
                )
                session = await self._compare_or_reload(requested, {session.status})
            await self._try_send_cancel(session)
            return await self._synchronize(session)

    async def cleanup(self, session_id: UUID) -> CiSession:
        async with self._session_lock(session_id):
            session = await self._synchronize(await self._require_session(session_id))
            return await self._cleanup_locked(session)

    async def process_maintenance(self) -> DistributedCiMaintenanceResult:
        now = self._now()
        heartbeat_before = now - timedelta(seconds=self._heartbeat_timeout_seconds)
        maintained_statuses = (
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
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
                self._repository.list(
                    status=status,
                    limit=self._maintenance_batch_size,
                )
                for status in maintained_statuses
            ),
            self._repository.list_stale(
                heartbeat_before=heartbeat_before,
                now=now,
                limit=self._maintenance_batch_size,
            ),
        )
        sessions = _unique_sessions(session for group in groups for session in group)
        stale_ids = {session.id for session in groups[-1]}
        semaphore = asyncio.Semaphore(self._maintenance_concurrency)

        async def maintain(session: CiSession) -> tuple[bool, bool, bool, bool]:
            async with semaphore, self._session_lock(session.id):
                return await self._maintain_locked(
                    session.id,
                    now=now,
                    heartbeat_before=heartbeat_before,
                    listed_stale=session.id in stale_ids,
                )

        changes = await asyncio.gather(*(maintain(session) for session in sessions))
        return DistributedCiMaintenanceResult(
            examined=len(sessions),
            synchronized=sum(item[0] for item in changes),
            timed_out=sum(item[1] for item in changes),
            finalized=sum(item[2] for item in changes),
            cleanup_pending=sum(item[3] for item in changes),
        )

    async def recover_incomplete(self) -> DistributedCiMaintenanceResult:
        """Rebuild CI projections from durable commands, operations, and leases."""

        return await self.process_maintenance()

    async def _maintain_locked(
        self,
        session_id: UUID,
        *,
        now: datetime,
        heartbeat_before: datetime,
        listed_stale: bool,
    ) -> tuple[bool, bool, bool, bool]:
        original = await self._require_session(session_id)
        session = await self._synchronize(original)
        synchronized = session != original
        if session.status is CiSessionStatus.CREATED:
            waiting = session.model_copy(update={"status": CiSessionStatus.WAITING_FOR_BENCH})
            session = await self._compare_or_reload(waiting, {CiSessionStatus.CREATED})
        if session.status is CiSessionStatus.COMPLETED:
            return synchronized, False, False, False

        timed_out = False
        if session.status in {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
            CiSessionStatus.RUNNING,
        } and self._session_is_due(
            session,
            now=now,
            heartbeat_before=heartbeat_before,
            listed_stale=listed_stale,
        ):
            session = await self._timeout_locked(session)
            timed_out = True

        if session.status is CiSessionStatus.CANCEL_REQUESTED:
            session = await self._synchronize(session)
            if session.status is CiSessionStatus.CANCEL_REQUESTED:
                await self._try_send_cancel(session)
                session = await self._fail_if_ownership_ended(session)

        if session.status in _SESSION_TERMINAL_STATUSES or (
            session.status is CiSessionStatus.CLEANUP_PENDING
        ):
            before = session.status
            session = await self._cleanup_locked(session)
            return (
                synchronized,
                timed_out,
                session.status is CiSessionStatus.COMPLETED,
                before is not CiSessionStatus.COMPLETED
                and session.status is CiSessionStatus.CLEANUP_PENDING,
            )
        return synchronized, timed_out, False, False

    async def _synchronize(self, session: CiSession) -> CiSession:
        if session.status not in {
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
        }:
            return session
        binding = await self._repository.get_distributed_workflow(session.id)
        if binding is None:
            return session
        command_id = binding.remote_command_id
        command, operation = await asyncio.gather(
            self._remote_work.get_command(command_id),
            self._remote_work.get_operation_for_command(command_id),
        )
        now = self._now()
        if operation is not None and operation.status in {
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.RECONCILING,
        }:
            if (
                operation.reconciliation_deadline is not None
                and operation.reconciliation_deadline <= now
            ):
                failed = session.model_copy(
                    update={
                        "status": CiSessionStatus.FAILED,
                        "outcome": CiOutcome.INFRASTRUCTURE_ERROR,
                    }
                )
                current = await self._compare_or_reload(
                    failed,
                    {CiSessionStatus.RUNNING, CiSessionStatus.CANCEL_REQUESTED},
                )
                await self._record_error_once(
                    current.id,
                    "REMOTE_OPERATION_RECONCILIATION_TIMEOUT",
                )
                return current
            return session
        target = _operation_session_result(session, operation, command)
        if target is None:
            return session
        status, outcome = target
        candidate = session.model_copy(update={"status": status, "outcome": outcome})
        return await self._compare_or_reload(
            candidate,
            {CiSessionStatus.RUNNING, CiSessionStatus.CANCEL_REQUESTED},
        )

    async def _timeout_locked(self, session: CiSession) -> CiSession:
        binding = await self._repository.get_distributed_workflow(session.id)
        if binding is None:
            candidate = session.model_copy(
                update={
                    "status": CiSessionStatus.TIMED_OUT,
                    "outcome": CiOutcome.TIMED_OUT,
                }
            )
            return await self._compare_or_reload(candidate, {session.status})
        candidate = session.model_copy(
            update={
                "status": CiSessionStatus.CANCEL_REQUESTED,
                "outcome": CiOutcome.TIMED_OUT,
            }
        )
        current = await self._compare_or_reload(candidate, {session.status})
        await self._record_error_once(current.id, "CI_SESSION_TIMED_OUT")
        await self._try_send_cancel(current)
        return current

    async def _cleanup_locked(self, session: CiSession) -> CiSession:
        if session.status is CiSessionStatus.COMPLETED:
            return session
        if session.status is CiSessionStatus.CANCEL_REQUESTED:
            session = await self._fail_if_ownership_ended(session)
        if session.status not in _SESSION_TERMINAL_STATUSES and (
            session.status is not CiSessionStatus.CLEANUP_PENDING
        ):
            raise CiSessionConflictError(
                "CI session cannot be cleaned up before remote work is terminal.",
                ci_session_id=str(session.id),
                status=session.status.value,
            )
        if session.status is not CiSessionStatus.CLEANUP_PENDING:
            pending = session.model_copy(
                update={
                    "status": CiSessionStatus.CLEANUP_PENDING,
                    "cleanup_status": CleanupStatus.RUNNING,
                }
            )
            session = await self._compare_or_reload(pending, {session.status})
        elif session.cleanup_status is not CleanupStatus.RUNNING:
            running = session.model_copy(update={"cleanup_status": CleanupStatus.RUNNING})
            session = await self._compare_or_reload(
                running,
                {CiSessionStatus.CLEANUP_PENDING},
            )

        release_error: str | None = None
        reservation_terminal = session.reservation_id is None
        if session.reservation_id is not None:
            try:
                reservation = await self._reservations.get(session.reservation_id)
                reservation_terminal = reservation.state in _RESERVATION_TERMINAL_STATES
                if not reservation_terminal:
                    released = await self._reservations.release(
                        session.reservation_id,
                        owner=session.requested_by,
                        expected_lease_version=reservation.lease.lease_version,
                        idempotency_key=_reservation_release_key(session.id),
                    )
                    reservation_terminal = released.state in _RESERVATION_TERMINAL_STATES
            except Exception as exc:  # cleanup is deliberately retryable
                release_error = f"reservation release failed: {type(exc).__name__}"

        work_terminal = await self._remote_work_is_terminal(session)
        artifacts_finalized, artifact_timeout_error = await self._finalize_artifacts(session)
        if artifact_timeout_error is not None:
            await self._record_error_once(session.id, artifact_timeout_error)
        cleanup_errors = [
            error for error in (release_error, artifact_timeout_error) if error is not None
        ]
        cleanup = CleanupResult(
            reservation_released=reservation_terminal,
            workflow_stopped=work_terminal or reservation_terminal,
            locks_released=work_terminal or reservation_terminal,
            serial_closed=work_terminal or reservation_terminal,
            artifacts_finalized=artifacts_finalized,
            errors=cleanup_errors,
        )
        await self._repository.save_cleanup(session.id, cleanup, recorded_at=self._now())
        artifact_retry_pending = not artifacts_finalized and artifact_timeout_error is None
        if not reservation_terminal or artifact_retry_pending:
            retry_status = (
                CleanupStatus.FAILED if release_error is not None else CleanupStatus.PENDING
            )
            retryable = session.model_copy(update={"cleanup_status": retry_status})
            current = await self._compare_or_reload(
                retryable,
                {CiSessionStatus.CLEANUP_PENDING},
            )
            if release_error is not None:
                await self._record_error_once(current.id, release_error)
            return current

        outcome = session.outcome
        cleanup_succeeded = artifacts_finalized and not cleanup_errors
        if outcome is CiOutcome.PENDING or not cleanup_succeeded:
            outcome = CiOutcome.INFRASTRUCTURE_ERROR
        completed = session.model_copy(
            update={
                "status": CiSessionStatus.COMPLETED,
                "outcome": outcome,
                "cleanup_status": (
                    CleanupStatus.SUCCEEDED if cleanup_succeeded else CleanupStatus.FAILED
                ),
                "completed_at": self._now(),
            }
        )
        errors = await self._repository.errors(session.id)
        finalized = await self._repository.mark_finalized(
            completed,
            idempotency_key=_finalize_key(session.id),
            errors=errors,
        )
        return finalized or await self._require_session(session.id)

    async def _try_send_cancel(self, session: CiSession) -> None:
        binding = await self._repository.get_distributed_workflow(session.id)
        if binding is None:
            return
        command = await self._remote_work.get_command(binding.remote_command_id)
        if command is None or command.status in {
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.EXPIRED,
        }:
            return
        try:
            await self._commands.request_cancel(
                binding.remote_command_id,
                reason=(
                    "CI session timed out"
                    if session.outcome is CiOutcome.TIMED_OUT
                    else "CI session cancelled"
                ),
            )
        except Exception as exc:  # cancellation remains pending and is retried
            await self._record_error_once(
                session.id,
                f"remote cancellation delivery failed: {type(exc).__name__}",
            )

    async def _finalize_artifacts(
        self,
        session: CiSession,
    ) -> tuple[bool, str | None]:
        binding = await self._repository.get_distributed_workflow(session.id)
        if binding is None:
            return True, None

        lookup_error: Exception | None = None
        try:
            artifacts = await self._artifacts.list(
                command_id=binding.remote_command_id,
                limit=10_000,
            )
        except Exception as exc:  # a transient repository failure is retryable
            artifacts = []
            lookup_error = exc

        pending = [artifact for artifact in artifacts if artifact.uploaded_at is None]
        if lookup_error is None and not pending:
            return True, None

        if pending:
            await asyncio.gather(
                *(
                    self._artifact_uploads.request_artifact_upload(
                        artifact.agent_id,
                        artifact.id,
                    )
                    for artifact in pending
                ),
                return_exceptions=True,
            )

        command, operation = await asyncio.gather(
            self._remote_work.get_command(binding.remote_command_id),
            self._remote_work.get_operation_for_command(binding.remote_command_id),
        )
        completed_at = (
            operation.completed_at
            if operation is not None and operation.completed_at is not None
            else command.completed_at
            if command is not None and command.completed_at is not None
            else session.timeout_at
            if session.timeout_at is not None
            else binding.created_at
        )
        if self._now() < completed_at + self._artifact_finalization_timeout:
            return False, None

        # Re-read at the deadline so an upload that completed while its retry request was
        # being delivered cannot be misclassified as a cleanup failure from a stale snapshot.
        if lookup_error is None:
            try:
                latest = await self._artifacts.list(
                    command_id=binding.remote_command_id,
                    limit=10_000,
                )
            except Exception as exc:
                lookup_error = exc
            else:
                pending = [artifact for artifact in latest if artifact.uploaded_at is None]
                if not pending:
                    return True, None

        if lookup_error is not None:
            detail = f"artifact metadata lookup failed: {type(lookup_error).__name__}"
        else:
            detail = f"{len(pending)} remote artifact upload(s) incomplete"
        return False, f"artifact finalization timed out: {detail}"

    async def _renew_if_needed(self, session: CiSession, now: datetime) -> None:
        if session.reservation_id is None or session.status not in {
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
        }:
            return
        try:
            reservation = await self._reservations.get(session.reservation_id)
            if reservation.state is not ReservationLeaseState.ACTIVE:
                return
            remaining = (reservation.lease.valid_until - now).total_seconds()
            if remaining > self._lease_renewal_threshold_seconds:
                return
            await self._reservations.renew(
                session.reservation_id,
                owner=session.requested_by,
                expected_lease_version=reservation.lease.lease_version,
                idempotency_key=_lease_renewal_key(
                    session.id,
                    reservation.lease.lease_version,
                ),
            )
        except Exception as exc:  # heartbeat persistence must survive a transient Agent outage
            await self._record_error_once(
                session.id,
                f"reservation lease renewal failed: {type(exc).__name__}",
            )

    async def _fail_if_ownership_ended(self, session: CiSession) -> CiSession:
        if session.reservation_id is None:
            target = (
                CiSessionStatus.TIMED_OUT
                if session.outcome is CiOutcome.TIMED_OUT
                else CiSessionStatus.CANCELLED
            )
            return await self._compare_or_reload(
                session.model_copy(update={"status": target}),
                {CiSessionStatus.CANCEL_REQUESTED},
            )
        try:
            reservation = await self._reservations.get(session.reservation_id)
        except Exception:
            return session
        if reservation.state not in _RESERVATION_TERMINAL_STATES:
            return session
        failed = session.model_copy(
            update={
                "status": CiSessionStatus.FAILED,
                "outcome": CiOutcome.INFRASTRUCTURE_ERROR,
            }
        )
        current = await self._compare_or_reload(
            failed,
            {CiSessionStatus.CANCEL_REQUESTED},
        )
        await self._record_error_once(current.id, "REMOTE_OPERATION_STATE_UNKNOWN")
        return current

    async def _remote_work_is_terminal(self, session: CiSession) -> bool:
        binding = await self._repository.get_distributed_workflow(session.id)
        if binding is None:
            return True
        command, operation = await asyncio.gather(
            self._remote_work.get_command(binding.remote_command_id),
            self._remote_work.get_operation_for_command(binding.remote_command_id),
        )
        return (operation is not None and operation.status in _OPERATION_TERMINAL_STATUSES) or (
            command is not None
            and command.status
            in {
                RemoteCommandStatus.SUCCEEDED,
                RemoteCommandStatus.FAILED,
                RemoteCommandStatus.CANCELLED,
                RemoteCommandStatus.EXPIRED,
            }
        )

    def _validate_started_workflow(
        self,
        binding: DistributedCiWorkflowBinding,
        request: DistributedCiStartRequest,
    ) -> None:
        if binding.workflow_name != request.workflow_name:
            raise CiSessionConflictError(
                "CI session is already bound to another workflow.",
                ci_session_id=str(binding.ci_session_id),
            )
        if (
            request.workflow_version is not None
            and binding.workflow_version != request.workflow_version
        ):
            raise CiSessionConflictError(
                "CI session is already bound to another workflow version.",
                ci_session_id=str(binding.ci_session_id),
            )

    async def _compare_or_reload(
        self,
        candidate: CiSession,
        expected_statuses: Iterable[CiSessionStatus],
    ) -> CiSession:
        persisted = await self._repository.compare_and_set(
            candidate,
            expected_statuses=expected_statuses,
        )
        return persisted or await self._require_session(candidate.id)

    async def _record_error_once(self, session_id: UUID, error: str) -> None:
        errors = await self._repository.errors(session_id)
        if error not in errors:
            await self._repository.append_error(session_id, error)

    async def _require_session(self, session_id: UUID) -> CiSession:
        session = await self._repository.get(session_id)
        if session is None:
            raise CiSessionNotFoundError(
                f"CI session {session_id} does not exist.",
                ci_session_id=str(session_id),
            )
        return session

    def _session_is_due(
        self,
        session: CiSession,
        *,
        now: datetime,
        heartbeat_before: datetime,
        listed_stale: bool,
    ) -> bool:
        if session.timeout_at is not None and session.timeout_at <= now:
            return True
        if listed_stale and (session.heartbeat_at or session.created_at) <= heartbeat_before:
            return True
        if session.status in {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
        }:
            request = session.bench_request or BenchRequest()
            return session.created_at + timedelta(seconds=request.maximum_wait_seconds) <= now
        return False

    def _session_lock(self, session_id: UUID) -> asyncio.Lock:
        return self._locks[session_id.int % len(self._locks)]

    def _now(self) -> datetime:
        return _as_utc(self._clock(), field="CI service clock")


def _operation_session_result(
    session: CiSession,
    operation: DistributedOperation | None,
    command: RemoteCommand | None,
) -> tuple[CiSessionStatus, CiOutcome] | None:
    operation_status = operation.status if operation is not None else None
    command_status = command.status if command is not None else None
    if (
        operation_status is DistributedOperationStatus.SUCCEEDED
        or command_status is RemoteCommandStatus.SUCCEEDED
    ):
        if session.outcome is CiOutcome.TIMED_OUT:
            return CiSessionStatus.TIMED_OUT, CiOutcome.TIMED_OUT
        return CiSessionStatus.SUCCEEDED, CiOutcome.SUCCEEDED
    if (
        operation_status is DistributedOperationStatus.CANCELLED
        or command_status is RemoteCommandStatus.CANCELLED
    ):
        if session.outcome is CiOutcome.TIMED_OUT:
            return CiSessionStatus.TIMED_OUT, CiOutcome.TIMED_OUT
        return CiSessionStatus.CANCELLED, CiOutcome.CANCELLED
    if operation_status is DistributedOperationStatus.FAILED or command_status in {
        RemoteCommandStatus.FAILED,
        RemoteCommandStatus.EXPIRED,
    }:
        if session.outcome is CiOutcome.TIMED_OUT:
            return CiSessionStatus.TIMED_OUT, CiOutcome.TIMED_OUT
        return CiSessionStatus.FAILED, CiOutcome.FAILED
    return None


def _with_ci_capabilities(
    definition: WorkflowDefinition,
    request: BenchRequest,
) -> WorkflowDefinition:
    requirements = definition.requirements
    capabilities = sorted(
        {
            *(capability.casefold() for capability in requirements.capabilities),
            *(capability.casefold() for capability in request.required_capabilities),
        }
    )
    if capabilities == requirements.capabilities:
        return definition
    return definition.model_copy(
        update={"requirements": requirements.model_copy(update={"capabilities": capabilities})}
    )


def _requested_kind(request: BenchRequest) -> GlobalBenchKind | None:
    if request.allow_simulated and request.allow_physical:
        return None
    if request.allow_simulated:
        return GlobalBenchKind.SIMULATED
    return GlobalBenchKind.PHYSICAL


def _require_same_creation(
    existing: CiSession,
    requested: CiSession,
    *,
    idempotency_key: str,
) -> None:
    fields = (
        "provider",
        "external_run_id",
        "repository",
        "ref",
        "commit_sha",
        "actor",
        "requested_by",
        "bench_request",
    )
    if any(getattr(existing, name) != getattr(requested, name) for name in fields):
        raise CiSessionConflictError(
            "CI session idempotency key was reused with different content.",
            idempotency_key=idempotency_key,
        )


def _default_create_key(session: CiSession) -> str:
    payload = {
        "provider": session.provider.value,
        "external_run_id": session.external_run_id,
        "requested_by": session.requested_by,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return f"distributed-ci-create:{digest}"


def _dispatch_fingerprint(dispatch: DistributedWorkflowDispatch) -> str:
    inputs: dict[str, object] = {}
    for name, value in dispatch.inputs.items():
        inputs[name] = (
            value.model_dump(mode="json") if isinstance(value, ArtifactReference) else value
        )
    payload = {
        "definition": dispatch.definition.model_dump(mode="json"),
        "inputs": inputs,
        "agent_id": str(dispatch.agent.id),
        "bench_id": dispatch.bench.id,
        "reservation_id": str(dispatch.reservation.reservation.id),
        "remote_command_id": str(dispatch.command.id),
        "operation_id": str(dispatch.operation.id),
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _workflow_launch_key(session_id: UUID) -> str:
    return f"distributed-ci:{session_id}:workflow"


def _reservation_release_key(session_id: UUID) -> str:
    return f"distributed-ci:{session_id}:release"


def _lease_renewal_key(session_id: UUID, lease_version: int) -> str:
    return f"distributed-ci:{session_id}:renew:{lease_version}"


def _finalize_key(session_id: UUID) -> str:
    return f"distributed-ci:{session_id}:finalize"


def _normalized_labels(labels: Mapping[str, str], *, field: str) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in labels.items():
        key = _require_text(raw_key, field=f"{field} key", maximum_length=200)
        value = _require_text(raw_value, field=f"{field} value", maximum_length=500)
        normalized[key] = value
    return normalized


def _merge_required_labels(
    persisted: Mapping[str, str],
    requested: Mapping[str, str],
) -> dict[str, str]:
    merged = _normalized_labels(persisted, field="required_agent_labels")
    for key, value in _normalized_labels(requested, field="agent_labels").items():
        if key in merged and merged[key] != value:
            raise CiSessionConflictError(
                "CI start request conflicts with a persisted required Agent label.",
                label=key,
            )
        merged[key] = value
    return merged


def _unique_sessions(sessions: Iterable[CiSession]) -> list[CiSession]:
    unique: dict[UUID, CiSession] = {}
    for session in sessions:
        unique[session.id] = session
    return sorted(unique.values(), key=lambda item: (item.created_at, str(item.id)))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _require_text(value: str, *, field: str, maximum_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field} exceeds its maximum length")
    return normalized


def _require_positive_integer(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DistributedCiCommandService",
    "DistributedCiCreateRequest",
    "DistributedCiMaintenanceResult",
    "DistributedCiRemoteWorkRepository",
    "DistributedCiReservationService",
    "DistributedCiSessionRepository",
    "DistributedCiSessionService",
    "DistributedCiStartRequest",
    "DistributedCiWorkflowCatalog",
    "DistributedCiWorkflowCoordinator",
]
