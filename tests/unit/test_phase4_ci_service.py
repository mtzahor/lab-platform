from __future__ import annotations

import asyncio
import builtins
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.ci import CiSessionService
from lab_platform.core.errors import CiSessionConflictError, InvalidArtifactError
from lab_platform.core.workflows import parse_workflow_yaml
from lab_platform.models import (
    ArtifactOwnerType,
    ArtifactRecord,
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
    ReservationSource,
    ReservationStatus,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class Candidate:
    id: str
    capabilities: set[str] = field(default_factory=lambda: {"firmware", "serial"})
    labels: dict[str, str] = field(default_factory=lambda: {"board": "esp32"})
    online: bool = True
    available: bool = True
    simulated: bool = True
    last_used_at: datetime | None = None


@dataclass(frozen=True)
class CatalogRecord:
    labels: dict[str, str]
    backend_id: str = "simlab"


class FakeCatalog:
    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self._records = {
            candidate.id: CatalogRecord(labels=dict(candidate.labels)) for candidate in candidates
        }

    def get(self, bench_id: str) -> CatalogRecord:
        return self._records[bench_id]


class FakeReservations:
    def __init__(self) -> None:
        self.items: dict[UUID, Reservation] = {}
        self.release_calls: builtins.list[tuple[UUID, str]] = []
        self.extend_calls: builtins.list[tuple[UUID, str, int]] = []
        self.release_error: Exception | None = None

    async def get(self, reservation_id: UUID) -> Reservation:
        return self.items[reservation_id]

    async def release(self, reservation_id: UUID | str, owner: str) -> Reservation | None:
        identifier = UUID(str(reservation_id))
        self.release_calls.append((identifier, owner))
        if self.release_error is not None:
            raise self.release_error
        reservation = self.items[identifier].model_copy(
            update={
                "status": ReservationStatus.RELEASED,
                "released_at": NOW,
            }
        )
        self.items[identifier] = reservation
        return reservation

    async def extend(self, reservation_id: UUID, owner: str, duration_seconds: int) -> Reservation:
        self.extend_calls.append((reservation_id, owner, duration_seconds))
        reservation = self.items[reservation_id]
        assert reservation.ends_at is not None
        extended = reservation.model_copy(
            update={"ends_at": reservation.ends_at + timedelta(seconds=duration_seconds)}
        )
        self.items[reservation_id] = extended
        return extended


class FakeCiRepository:
    def __init__(
        self,
        candidates: Sequence[Candidate],
        reservations: FakeReservations,
    ) -> None:
        self.sessions: dict[UUID, CiSession] = {}
        self.creation_keys: dict[str, UUID] = {}
        self.launch_keys: dict[str, UUID] = {}
        self.finalize_keys: dict[str, UUID] = {}
        self.cleanups: dict[UUID, CleanupResult] = {}
        self.session_errors: dict[UUID, builtins.list[str]] = {}
        self.candidates = builtins.list(candidates)
        self.reservations = reservations
        self.assignment_enabled = True
        self.attach_override: CiSession | None = None
        self.attach_error: Exception | None = None
        self.before_compare_and_set: Callable[[CiSession], Awaitable[None]] | None = None
        self.assignment_delay_seconds = 0.0
        self.active_assignments = 0
        self.maximum_active_assignments = 0
        self.cleanup_delay_seconds = 0.0
        self.active_cleanups = 0
        self.maximum_active_cleanups = 0
        self.save_cleanup_calls = 0
        self.mark_finalized_calls = 0

    async def create(
        self,
        session: CiSession,
        *,
        idempotency_key: str | None = None,
        errors: Sequence[str] = (),
    ) -> CiSession:
        if idempotency_key is not None and idempotency_key in self.creation_keys:
            return self.sessions[self.creation_keys[idempotency_key]]
        self.sessions[session.id] = session
        self.session_errors[session.id] = builtins.list(errors)
        if idempotency_key is not None:
            self.creation_keys[idempotency_key] = session.id
        return session

    async def get(self, session_id: UUID) -> CiSession | None:
        return self.sessions.get(session_id)

    async def get_by_workflow_launch_idempotency_key(self, key: str) -> CiSession | None:
        session_id = self.launch_keys.get(key)
        return self.sessions.get(session_id) if session_id is not None else None

    async def get_by_finalize_idempotency_key(self, key: str) -> CiSession | None:
        session_id = self.finalize_keys.get(key)
        return self.sessions.get(session_id) if session_id is not None else None

    async def update(
        self,
        session: CiSession,
        *,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        if session.id not in self.sessions:
            return None
        self.sessions[session.id] = session
        if errors is not None:
            self.session_errors[session.id] = builtins.list(errors)
        return session

    async def compare_and_set(
        self,
        session: CiSession,
        *,
        expected_statuses: Iterable[CiSessionStatus] | None,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        if self.before_compare_and_set is not None:
            await self.before_compare_and_set(session)
        current = self.sessions.get(session.id)
        if current is None:
            return None
        expected = set(expected_statuses or ())
        if expected and current.status not in expected:
            return None
        self.sessions[session.id] = session
        if errors is not None:
            self.session_errors[session.id] = builtins.list(errors)
        return session

    async def list(
        self,
        *,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> builtins.list[CiSession]:
        matches = [
            session
            for session in self.sessions.values()
            if (status is None or session.status is status)
            and (provider is None or session.provider is provider)
        ]
        return sorted(matches, key=lambda item: (item.created_at, str(item.id)))[:limit]

    async def list_stale(
        self,
        *,
        heartbeat_before: datetime,
        now: datetime,
        limit: int = 500,
    ) -> builtins.list[CiSession]:
        live = {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
            CiSessionStatus.RUNNING,
            CiSessionStatus.CANCEL_REQUESTED,
            CiSessionStatus.CLEANUP_PENDING,
        }
        stale = [
            session
            for session in self.sessions.values()
            if session.status in live
            and (
                (session.timeout_at is not None and session.timeout_at <= now)
                or (session.heartbeat_at or session.created_at) <= heartbeat_before
            )
        ]
        return sorted(stale, key=lambda item: (item.created_at, str(item.id)))[:limit]

    async def errors(self, session_id: UUID) -> builtins.list[str]:
        return builtins.list(self.session_errors.get(session_id, []))

    async def assign_compatible_bench(
        self,
        session_id: UUID,
        request: BenchRequest,
        *,
        now: datetime,
        reservation_id: UUID | None = None,
        owner: str | None = None,
        reservation_idempotency_key: str | None = None,
    ) -> tuple[CiSession, Reservation] | None:
        del reservation_idempotency_key
        if self.assignment_delay_seconds:
            self.active_assignments += 1
            self.maximum_active_assignments = max(
                self.maximum_active_assignments,
                self.active_assignments,
            )
            try:
                await asyncio.sleep(self.assignment_delay_seconds)
            finally:
                self.active_assignments -= 1
        if not self.assignment_enabled:
            return None
        session = self.sessions[session_id]
        if session.reservation_id is not None:
            return session, self.reservations.items[session.reservation_id]
        candidates = [candidate for candidate in self.candidates if candidate.online]
        if request.explicit_bench_id is not None:
            candidates = [
                candidate for candidate in candidates if candidate.id == request.explicit_bench_id
            ]
        candidates = [
            candidate
            for candidate in candidates
            if candidate.available
            and request.required_capabilities.issubset(candidate.capabilities)
            and all(
                candidate.labels.get(key) == value for key, value in request.required_labels.items()
            )
            and (request.allow_simulated or not candidate.simulated)
            and (request.allow_physical or candidate.simulated)
        ]
        candidates.sort(
            key=lambda candidate: (
                -sum(
                    candidate.labels.get(key) == value
                    for key, value in request.preferred_labels.items()
                ),
                candidate.last_used_at or datetime.min.replace(tzinfo=UTC),
                candidate.id,
            )
        )
        if not candidates:
            return None
        selected = candidates[0]
        selected.available = False
        selected.last_used_at = now
        identifier = reservation_id or uuid4()
        requested_by = owner or session.requested_by
        reservation = Reservation(
            id=identifier,
            bench_id=selected.id,
            owner=requested_by,
            created_at=now,
            requested_at=now,
            starts_at=now,
            ends_at=now + timedelta(seconds=request.reservation_duration_seconds),
            activated_at=now,
            status=ReservationStatus.ACTIVE,
            source=ReservationSource.CI,
            metadata={"ci_session_id": str(session.id)},
        )
        self.reservations.items[identifier] = reservation
        assigned = session.model_copy(
            update={
                "bench_id": selected.id,
                "reservation_id": identifier,
                "status": CiSessionStatus.RESERVED,
            }
        )
        self.sessions[session.id] = assigned
        return assigned, reservation

    async def attach_workflow_run(
        self,
        session_id: UUID,
        workflow_run_id: UUID,
        *,
        idempotency_key: str,
        started_at: datetime,
    ) -> CiSession | None:
        if self.attach_error is not None:
            raise self.attach_error
        if self.attach_override is not None:
            attached = self.attach_override
            self.sessions[attached.id] = attached
            self.launch_keys[idempotency_key] = attached.id
            return attached
        if idempotency_key in self.launch_keys:
            existing = self.launch_keys[idempotency_key]
            return self.sessions[existing]
        session = self.sessions[session_id]
        if session.status is not CiSessionStatus.RESERVED:
            return None
        attached = session.model_copy(
            update={
                "workflow_run_id": workflow_run_id,
                "status": CiSessionStatus.RUNNING,
                "started_at": started_at,
            }
        )
        self.sessions[session_id] = attached
        self.launch_keys[idempotency_key] = session_id
        return attached

    async def mark_finalized(
        self,
        session: CiSession,
        *,
        idempotency_key: str,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        self.mark_finalized_calls += 1
        if idempotency_key in self.finalize_keys:
            return self.sessions[self.finalize_keys[idempotency_key]]
        self.sessions[session.id] = session
        self.finalize_keys[idempotency_key] = session.id
        if errors is not None:
            self.session_errors[session.id] = builtins.list(errors)
        return session

    async def save_cleanup(
        self,
        session_id: UUID,
        result: CleanupResult,
        *,
        recorded_at: datetime | None = None,
    ) -> CleanupResult:
        del recorded_at
        self.save_cleanup_calls += 1
        if self.cleanup_delay_seconds:
            self.active_cleanups += 1
            self.maximum_active_cleanups = max(
                self.maximum_active_cleanups,
                self.active_cleanups,
            )
            try:
                await asyncio.sleep(self.cleanup_delay_seconds)
            finally:
                self.active_cleanups -= 1
        self.cleanups[session_id] = result
        return result

    async def get_cleanup(self, session_id: UUID) -> CleanupResult | None:
        return self.cleanups.get(session_id)


class FakeWorkflows:
    def __init__(self, reservations: FakeReservations, clock: MutableClock) -> None:
        self.reservations = reservations
        self.clock = clock
        self.definitions: dict[str, WorkflowDefinition] = {}
        self.runs: dict[UUID, WorkflowRun] = {}
        self.step_results: dict[UUID, builtins.list[WorkflowStepResult]] = {}
        self.start_calls = 0
        self.cancel_calls: builtins.list[tuple[UUID, str]] = []
        self.wait_calls: builtins.list[UUID] = []
        self.resolved_artifacts: dict[UUID, Path] = {}
        self.wait_error: Exception | None = None

    async def get_definition(self, name: str, version: int | None = None) -> WorkflowDefinition:
        definition = self.definitions[name]
        if version is not None:
            assert definition.version == version
        return definition

    async def start(
        self,
        name: str,
        *,
        bench_id: str,
        owner: str,
        version: int | None = None,
        inputs: Mapping[str, object] | None = None,
        artifact_resolver: Callable[[ArtifactReference], Path] | None = None,
    ) -> WorkflowRun:
        self.start_calls += 1
        await self.get_definition(name, version)
        for value in (inputs or {}).values():
            if isinstance(value, Mapping) and set(value) == {"artifact_id"}:
                reference = ArtifactReference(artifact_id=UUID(str(value["artifact_id"])))
                assert artifact_resolver is not None
                self.resolved_artifacts[reference.artifact_id] = artifact_resolver(reference)
        reservation = next(
            item
            for item in self.reservations.items.values()
            if item.bench_id == bench_id
            and item.owner == owner
            and item.status is ReservationStatus.ACTIVE
        )
        run = WorkflowRun(
            workflow_name=name,
            workflow_version=self.definitions[name].version,
            bench_id=bench_id,
            owner=owner,
            reservation_id=reservation.id,
            status=WorkflowRunStatus.RUNNING,
            created_at=self.clock(),
            started_at=self.clock(),
        )
        self.runs[run.id] = run
        self.step_results[run.id] = []
        return run

    async def get_run(self, run_id: UUID) -> WorkflowRun:
        return self.runs[run_id]

    async def list_step_results(self, run_id: UUID) -> builtins.list[WorkflowStepResult]:
        return builtins.list(self.step_results.get(run_id, []))

    async def cancel(self, run_id: UUID, owner: str) -> WorkflowRun:
        self.cancel_calls.append((run_id, owner))
        run = self.runs[run_id].model_copy(
            update={
                "status": WorkflowRunStatus.CANCELLED,
                "completed_at": self.clock(),
            }
        )
        self.runs[run_id] = run
        return run

    async def wait(self, run_id: UUID) -> WorkflowRun:
        self.wait_calls.append(run_id)
        if self.wait_error is not None:
            raise self.wait_error
        return self.runs[run_id]


class FakeLocks:
    def __init__(self) -> None:
        self.items: dict[str, BenchOperationLock] = {}
        self.error: Exception | None = None

    async def get(self, bench_id: str) -> BenchOperationLock | None:
        if self.error is not None:
            raise self.error
        return self.items.get(bench_id)

    async def release(self, bench_id: str, operation_id: UUID) -> bool:
        lock = self.items.get(bench_id)
        if lock is None or lock.operation_id != operation_id:
            return False
        del self.items[bench_id]
        return True


class FakeEvents:
    def __init__(self) -> None:
        self.items: builtins.list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event


class FakeArtifactRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, ArtifactRecord] = {}
        self.keys: dict[tuple[UUID, str], UUID] = {}

    async def save(
        self,
        record: ArtifactRecord,
        *,
        idempotency_key: str | None = None,
    ) -> ArtifactRecord:
        if idempotency_key is not None:
            existing_id = self.keys.get((record.owner_id, idempotency_key))
            if existing_id is not None:
                return self.items[existing_id]
            self.keys[(record.owner_id, idempotency_key)] = record.id
        self.items[record.id] = record
        return record

    async def get(self, artifact_id: UUID) -> ArtifactRecord | None:
        return self.items.get(artifact_id)

    async def get_by_idempotency_key(
        self, owner_id: UUID, idempotency_key: str
    ) -> ArtifactRecord | None:
        artifact_id = self.keys.get((owner_id, idempotency_key))
        return self.items.get(artifact_id) if artifact_id is not None else None

    async def list_for_owner(
        self, owner_type: ArtifactOwnerType, owner_id: UUID
    ) -> builtins.list[ArtifactRecord]:
        return [
            record
            for record in self.items.values()
            if record.owner_type is owner_type and record.owner_id == owner_id
        ]


@dataclass
class Stack:
    service: CiSessionService
    repository: FakeCiRepository
    workflows: FakeWorkflows
    reservations: FakeReservations
    locks: FakeLocks
    artifacts: ArtifactService
    artifact_repository: FakeArtifactRepository
    events: FakeEvents
    clock: MutableClock


def build_stack(
    tmp_path: Path,
    *,
    candidates: Sequence[Candidate] | None = None,
    assignment_enabled: bool = True,
    maintenance_concurrency: int = 16,
) -> Stack:
    clock = MutableClock()
    reservations = FakeReservations()
    available = builtins.list(candidates or [Candidate("sim-esp32-01")])
    repository = FakeCiRepository(available, reservations)
    repository.assignment_enabled = assignment_enabled
    workflows = FakeWorkflows(reservations, clock)
    locks = FakeLocks()
    events = FakeEvents()
    artifact_repository = FakeArtifactRepository()
    artifacts = ArtifactService(
        artifact_repository,
        tmp_path / "artifacts",
        maximum_size_bytes=1024 * 1024,
        events=events,
        clock=clock,
    )
    service = CiSessionService(
        repository,
        workflows,
        reservations,
        locks,
        artifacts,
        events,
        FakeCatalog(available),
        clock=clock,
        heartbeat_timeout_seconds=10,
        session_timeout_seconds=3600,
        workflow_timeout_seconds=20,
        maximum_reservation_seconds=7200,
        cleanup_timeout_seconds=1,
        maintenance_concurrency=maintenance_concurrency,
        backend_types={"simlab": "simlab"},
    )
    return Stack(
        service=service,
        repository=repository,
        workflows=workflows,
        reservations=reservations,
        locks=locks,
        artifacts=artifacts,
        artifact_repository=artifact_repository,
        events=events,
        clock=clock,
    )


def request(**updates: object) -> BenchRequest:
    values: dict[str, object] = {
        "required_capabilities": {"firmware", "serial"},
        "required_labels": {"board": "esp32"},
        "allow_simulated": True,
        "allow_physical": True,
        "maximum_wait_seconds": 60,
        "reservation_duration_seconds": 30,
    }
    values.update(updates)
    return BenchRequest.model_validate(values)


def artifact_workflow(*, labels: Mapping[str, str] | None = None) -> WorkflowDefinition:
    board_labels = dict(labels or {"board": "esp32"})
    return parse_workflow_yaml(
        f"""
name: esp32-ci-test
version: 2
inputs:
  firmware: {{type: artifact, required: true}}
requirements:
  capabilities: [firmware]
  labels: {board_labels!r}
steps:
  - name: Flash firmware
    action: flash
    firmware: "${{{{ inputs.firmware }}}}"
"""
    )


async def create_assigned(stack: Stack, *, key: str = "create-1") -> CiSession:
    return await stack.service.create(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="12345",
        requested_by="github-actions",
        repository="org/project",
        bench_request=request(),
        idempotency_key=key,
    )


async def upload_firmware(
    stack: Stack,
    session_id: UUID,
    *,
    owner_id: UUID | None = None,
) -> ArtifactRecord:
    return await stack.artifacts.store_bytes(
        b"firmware",
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=owner_id or session_id,
        name="firmware.bin",
        artifact_type="firmware",
        idempotency_key=f"firmware:{owner_id or session_id}",
    )


def test_create_assign_filters_candidates_and_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        candidates = [
            Candidate("offline", online=False),
            Candidate("wrong-board", labels={"board": "stm32"}),
            Candidate("missing-serial", capabilities={"firmware"}),
            Candidate("sim-esp32-02", labels={"board": "esp32", "rack": "b"}),
            Candidate("sim-esp32-01", labels={"board": "esp32", "rack": "a"}),
        ]
        stack = build_stack(tmp_path, candidates=candidates)
        created = await stack.service.create(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="100",
            requested_by="github-actions",
            bench_request=request(preferred_labels={"rack": "a"}),
            idempotency_key="github:org/repo:100:1",
        )
        replayed = await stack.service.create(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="different-on-retry",
            requested_by="github-actions",
            bench_request=request(),
            idempotency_key="github:org/repo:100:1",
        )
        with pytest.raises(CiSessionConflictError, match="another token owner"):
            await stack.service.create(
                provider=CiProvider.GITHUB_ACTIONS,
                external_run_id="stolen-retry",
                requested_by="different-owner",
                bench_request=request(),
                idempotency_key="github:org/repo:100:1",
            )

        assert created.status is CiSessionStatus.RESERVED
        assert created.bench_id == "sim-esp32-01"
        assert replayed == created
        assert len(stack.repository.sessions) == 1
        assert len(stack.reservations.items) == 1
        reservation = next(iter(stack.reservations.items.values()))
        assert reservation.source is ReservationSource.CI
        assert reservation.owner == "github-actions"
        assert [event.type for event in stack.events.items] == [
            "CI_SESSION_CREATED",
            "CI_SESSION_WAITING_FOR_BENCH",
            "CI_BENCH_ASSIGNED",
        ]

    asyncio.run(scenario())


def test_create_without_candidate_fails_immediately_when_wait_is_zero(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path, assignment_enabled=False)

        session = await stack.service.create(
            provider=CiProvider.LOCAL,
            external_run_id="no-compatible-bench",
            requested_by="local-ci",
            bench_request=request(maximum_wait_seconds=0),
        )

        assert session.status is CiSessionStatus.COMPLETED
        assert session.outcome is CiOutcome.TIMED_OUT
        assert session.cleanup_status is CleanupStatus.SUCCEEDED
        assert await stack.repository.errors(session.id) == ["NO_COMPATIBLE_BENCH"]
        event_types = [event.type for event in stack.events.items]
        assert "CI_SESSION_TIMED_OUT" in event_types
        assert "CI_HEARTBEAT_MISSED" not in event_types

    asyncio.run(scenario())


def test_create_replay_resumes_a_persisted_created_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stuck = CiSession(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="interrupted-create",
            requested_by="github-actions",
            status=CiSessionStatus.CREATED,
            created_at=stack.clock(),
            heartbeat_at=stack.clock(),
            timeout_at=stack.clock() + timedelta(hours=1),
            bench_request=request(),
        )
        await stack.repository.create(stuck, idempotency_key="interrupted-create")

        resumed = await stack.service.create(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id="retry",
            requested_by="github-actions",
            bench_request=request(),
            idempotency_key="interrupted-create",
        )

        assert resumed.status is CiSessionStatus.RESERVED
        assert resumed.bench_id == "sim-esp32-01"
        assert len(stack.reservations.items) == 1

    asyncio.run(scenario())


def test_artifact_workflow_launch_and_launch_idempotency(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)

        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
            idempotency_key="launch-1",
        )
        replayed = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
            idempotency_key="launch-1",
        )

        assert running.status is CiSessionStatus.RUNNING
        assert replayed == running
        assert stack.workflows.start_calls == 1
        assert stack.workflows.resolved_artifacts[firmware.id] == Path(firmware.path)
        assert [event.type for event in stack.events.items].count("CI_SESSION_STARTED") == 1

    asyncio.run(scenario())


def test_racing_workflow_launch_cancels_the_duplicate(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        existing_run_id = uuid4()
        stack.repository.attach_override = session.model_copy(
            update={
                "status": CiSessionStatus.RUNNING,
                "workflow_run_id": existing_run_id,
                "started_at": stack.clock(),
            }
        )

        attached = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
            idempotency_key="racing-launch",
        )

        assert attached.workflow_run_id == existing_run_id
        assert len(stack.workflows.cancel_calls) == 1
        assert stack.workflows.cancel_calls[0][0] != existing_run_id
        assert stack.workflows.start_calls == 1

    asyncio.run(scenario())


def test_workflow_attachment_failure_cancels_and_waits_for_started_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        stack.repository.attach_error = RuntimeError("database unavailable")

        with pytest.raises(RuntimeError, match="database unavailable"):
            await stack.service.start_workflow(
                session.id,
                workflow_name="esp32-ci-test",
                inputs={"firmware": {"artifact_id": str(firmware.id)}},
            )

        assert stack.workflows.start_calls == 1
        assert len(stack.workflows.cancel_calls) == 1
        run_id, owner = stack.workflows.cancel_calls[0]
        assert owner == "github-actions"
        assert stack.workflows.wait_calls == [run_id]
        assert stack.workflows.runs[run_id].status is WorkflowRunStatus.CANCELLED
        assert (await stack.service.get(session.id)).status is CiSessionStatus.RESERVED

    asyncio.run(scenario())


def test_workflow_success_and_failure_synchronize_and_preserve_outcomes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        for index, (run_status, session_status, outcome) in enumerate(
            (
                (
                    WorkflowRunStatus.SUCCEEDED,
                    CiSessionStatus.SUCCEEDED,
                    CiOutcome.SUCCEEDED,
                ),
                (
                    WorkflowRunStatus.FAILED,
                    CiSessionStatus.FAILED,
                    CiOutcome.FAILED,
                ),
            )
        ):
            stack = build_stack(tmp_path / str(index))
            stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
            session = await create_assigned(stack, key=f"create-{index}")
            firmware = await upload_firmware(stack, session.id)
            running = await stack.service.start_workflow(
                session.id,
                workflow_name="esp32-ci-test",
                inputs={"firmware": {"artifact_id": str(firmware.id)}},
            )
            assert running.workflow_run_id is not None
            run = stack.workflows.runs[running.workflow_run_id]
            stack.workflows.runs[run.id] = run.model_copy(
                update={"status": run_status, "completed_at": stack.clock()}
            )

            synchronized = await stack.service.get(session.id)
            assert synchronized.status is session_status
            assert synchronized.outcome is outcome

            finalized = await stack.service.finalize(session.id)
            assert finalized.status is CiSessionStatus.COMPLETED
            assert finalized.outcome is outcome
            assert finalized.cleanup_status is CleanupStatus.SUCCEEDED

    asyncio.run(scenario())


def test_maintenance_finalizes_a_terminal_session_after_the_client_disappears(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None
        run = stack.workflows.runs[running.workflow_run_id]
        stack.workflows.runs[run.id] = run.model_copy(
            update={"status": WorkflowRunStatus.SUCCEEDED, "completed_at": stack.clock()}
        )

        terminal = await stack.service.get(session.id)
        assert terminal.status is CiSessionStatus.SUCCEEDED
        assert stack.reservations.release_calls == []

        assert await stack.service.process_maintenance() == 1
        completed = await stack.service.get(session.id, synchronize=False)
        assert completed.status is CiSessionStatus.COMPLETED
        assert completed.outcome is CiOutcome.SUCCEEDED
        assert stack.reservations.release_calls == [
            (cast(UUID, session.reservation_id), "github-actions")
        ]

    asyncio.run(scenario())


def test_heartbeat_updates_session_and_renews_near_expiry_reservation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        session = await create_assigned(stack)
        assert session.reservation_id is not None
        original = stack.reservations.items[session.reservation_id]
        assert original.ends_at == NOW + timedelta(seconds=30)
        stack.clock.advance(15)

        heartbeat = await stack.service.heartbeat(session.id)

        assert heartbeat.heartbeat_at == NOW + timedelta(seconds=15)
        assert stack.reservations.extend_calls == [(session.reservation_id, "github-actions", 20)]
        renewed = stack.reservations.items[session.reservation_id]
        assert renewed.ends_at == NOW + timedelta(seconds=50)

    asyncio.run(scenario())


def test_cancellation_stops_workflow_cleans_resources_and_is_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None

        cancelled = await stack.service.cancel(session.id)
        replayed = await stack.service.cancel(session.id)

        assert cancelled.status is CiSessionStatus.COMPLETED
        assert cancelled.outcome is CiOutcome.CANCELLED
        assert cancelled.cleanup_status is CleanupStatus.SUCCEEDED
        assert replayed == cancelled
        assert stack.workflows.cancel_calls == [(running.workflow_run_id, "github-actions")]
        assert stack.reservations.release_calls == [
            (cast(UUID, running.reservation_id), "github-actions")
        ]
        assert stack.repository.cleanups[session.id].reservation_released

    asyncio.run(scenario())


def test_cleanup_reports_a_serial_handle_close_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None
        run = stack.workflows.runs[running.workflow_run_id]
        stack.workflows.runs[run.id] = run.model_copy(
            update={
                "status": WorkflowRunStatus.FAILED,
                "completed_at": stack.clock(),
                "error_code": "SERIAL_CLOSE_FAILED",
            }
        )
        stack.workflows.step_results[run.id] = [
            WorkflowStepResult(
                workflow_run_id=run.id,
                step_index=0,
                name="Read serial",
                action=WorkflowAction.READ_SERIAL,
                status=WorkflowStepStatus.FAILED,
                started_at=stack.clock(),
                completed_at=stack.clock(),
                error_code="SERIAL_CLOSE_FAILED",
                error_message="Could not close serial port",
            )
        ]

        finalized = await stack.service.finalize(session.id)

        assert finalized.status is CiSessionStatus.COMPLETED
        assert finalized.outcome is CiOutcome.INFRASTRUCTURE_ERROR
        assert finalized.cleanup_status is CleanupStatus.FAILED
        cleanup = stack.repository.cleanups[session.id]
        assert cleanup.serial_closed is False
        assert cleanup.reservation_released is True
        assert cleanup.errors == ["serial: a serial handle failed to close"]

    asyncio.run(scenario())


def test_cleanup_releases_a_stranded_lock_owned_by_the_completed_workflow(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None
        assert running.bench_id is not None
        run = stack.workflows.runs[running.workflow_run_id]
        stack.workflows.runs[run.id] = run.model_copy(
            update={"status": WorkflowRunStatus.SUCCEEDED, "completed_at": stack.clock()}
        )
        stack.locks.items[running.bench_id] = BenchOperationLock(
            bench_id=running.bench_id,
            operation_id=run.id,
            acquired_at=stack.clock(),
        )

        finalized = await stack.service.finalize(session.id)

        assert finalized.cleanup_status is CleanupStatus.SUCCEEDED
        assert stack.repository.cleanups[session.id].locks_released is True
        assert running.bench_id not in stack.locks.items

    asyncio.run(scenario())


def test_concurrent_finalization_is_single_flight_per_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        session = await create_assigned(stack)
        stack.repository.cleanup_delay_seconds = 0.02

        finalized = await asyncio.gather(*(stack.service.finalize(session.id) for _ in range(20)))

        assert all(item == finalized[0] for item in finalized)
        assert finalized[0].status is CiSessionStatus.COMPLETED
        assert stack.repository.save_cleanup_calls == 1
        assert stack.repository.mark_finalized_calls == 1
        assert stack.reservations.release_calls == [
            (cast(UUID, session.reservation_id), "github-actions")
        ]
        event_types = [event.type for event in stack.events.items]
        assert event_types.count("CI_CLEANUP_STARTED") == 1
        assert event_types.count("CI_CLEANUP_COMPLETED") == 1
        assert event_types.count("CI_SESSION_FINALIZED") == 1

    asyncio.run(scenario())


def test_maintenance_times_out_bench_wait_and_stale_heartbeat(tmp_path: Path) -> None:
    async def scenario() -> None:
        waiting_stack = build_stack(tmp_path / "waiting", assignment_enabled=False)
        waiting = await waiting_stack.service.create(
            provider=CiProvider.LOCAL,
            external_run_id="waiting",
            requested_by="local-ci",
            bench_request=request(maximum_wait_seconds=5),
        )
        assert waiting.status is CiSessionStatus.WAITING_FOR_BENCH
        waiting_stack.clock.advance(6)
        assert await waiting_stack.service.process_maintenance() == 1
        timed_out = await waiting_stack.service.get(waiting.id, synchronize=False)
        assert timed_out.status is CiSessionStatus.COMPLETED
        assert timed_out.outcome is CiOutcome.TIMED_OUT
        assert await waiting_stack.repository.errors(waiting.id) == ["BENCH_WAIT_TIMEOUT"]

        stale_stack = build_stack(tmp_path / "stale")
        stale = await create_assigned(stale_stack)
        stale_stack.clock.advance(11)
        assert await stale_stack.service.process_maintenance() == 1
        abandoned = await stale_stack.service.get(stale.id, synchronize=False)
        assert abandoned.status is CiSessionStatus.COMPLETED
        assert abandoned.outcome is CiOutcome.TIMED_OUT
        assert await stale_stack.repository.errors(stale.id) == ["CI_HEARTBEAT_MISSED"]
        stale_events = [event.type for event in stale_stack.events.items]
        assert stale_events.count("CI_HEARTBEAT_MISSED") == 1
        assert stale_events.count("CI_SESSION_TIMED_OUT") == 1

        absolute_stack = build_stack(tmp_path / "session-timeout")
        absolute = await create_assigned(absolute_stack)
        absolute_stack.repository.sessions[absolute.id] = absolute.model_copy(
            update={"timeout_at": NOW + timedelta(seconds=5)}
        )
        absolute_stack.clock.advance(6)
        assert await absolute_stack.service.process_maintenance() == 1
        session_timed_out = await absolute_stack.service.get(
            absolute.id,
            synchronize=False,
        )
        assert session_timed_out.status is CiSessionStatus.COMPLETED
        assert await absolute_stack.repository.errors(absolute.id) == ["SESSION_TIMEOUT"]
        absolute_events = [event.type for event in absolute_stack.events.items]
        assert absolute_events.count("CI_SESSION_TIMED_OUT") == 1
        assert "CI_HEARTBEAT_MISSED" not in absolute_events

        workflow_stack = build_stack(tmp_path / "workflow-timeout")
        workflow_stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        assigned = await create_assigned(workflow_stack)
        firmware = await upload_firmware(workflow_stack, assigned.id)
        running = await workflow_stack.service.start_workflow(
            assigned.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        workflow_stack.clock.advance(21)
        assert await workflow_stack.service.process_maintenance() == 1
        workflow_timed_out = await workflow_stack.service.get(
            running.id,
            synchronize=False,
        )
        assert workflow_timed_out.status is CiSessionStatus.COMPLETED
        assert workflow_timed_out.outcome is CiOutcome.TIMED_OUT
        assert await workflow_stack.repository.errors(running.id) == ["WORKFLOW_TIMEOUT"]
        workflow_events = [event.type for event in workflow_stack.events.items]
        assert workflow_events.count("CI_SESSION_TIMED_OUT") == 1
        assert "CI_HEARTBEAT_MISSED" not in workflow_events

    asyncio.run(scenario())


def test_heartbeat_refresh_wins_race_with_stale_maintenance_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        session = await create_assigned(stack)
        stack.clock.advance(11)
        heartbeat_cas_started = asyncio.Event()
        allow_heartbeat_cas = asyncio.Event()

        async def pause_heartbeat_cas(candidate: CiSession) -> None:
            if (
                candidate.id == session.id
                and candidate.status is CiSessionStatus.RESERVED
                and candidate.heartbeat_at == stack.clock()
            ):
                heartbeat_cas_started.set()
                await allow_heartbeat_cas.wait()

        stack.repository.before_compare_and_set = pause_heartbeat_cas
        heartbeat_task = asyncio.create_task(stack.service.heartbeat(session.id))
        await heartbeat_cas_started.wait()
        maintenance_task = asyncio.create_task(stack.service.process_maintenance())
        await asyncio.sleep(0)
        allow_heartbeat_cas.set()

        heartbeat, processed = await asyncio.gather(
            heartbeat_task,
            maintenance_task,
        )

        current = await stack.service.get(session.id, synchronize=False)
        assert processed == 1
        assert heartbeat.heartbeat_at == stack.clock()
        assert current.status is CiSessionStatus.RESERVED
        assert current.heartbeat_at == stack.clock()
        assert await stack.repository.errors(session.id) == []
        assert "CI_SESSION_TIMED_OUT" not in {event.type for event in stack.events.items}

    asyncio.run(scenario())


def test_workflow_success_wins_race_with_timeout_compare_and_set(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        session = await create_assigned(stack)
        firmware = await upload_firmware(stack, session.id)
        running = await stack.service.start_workflow(
            session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None
        stack.clock.advance(21)
        timeout_cas_started = asyncio.Event()
        allow_timeout_cas = asyncio.Event()

        async def pause_timeout_cas(candidate: CiSession) -> None:
            if candidate.id == session.id and candidate.status is CiSessionStatus.TIMED_OUT:
                timeout_cas_started.set()
                await allow_timeout_cas.wait()

        stack.repository.before_compare_and_set = pause_timeout_cas
        maintenance_task = asyncio.create_task(stack.service.process_maintenance())
        await timeout_cas_started.wait()
        run = stack.workflows.runs[running.workflow_run_id]
        stack.workflows.runs[run.id] = run.model_copy(
            update={
                "status": WorkflowRunStatus.SUCCEEDED,
                "completed_at": stack.clock(),
            }
        )
        synchronized = await stack.service.get(session.id)
        assert synchronized.status is CiSessionStatus.SUCCEEDED
        allow_timeout_cas.set()
        assert await maintenance_task == 1

        completed = await stack.service.get(session.id, synchronize=False)
        assert completed.status is CiSessionStatus.COMPLETED
        assert completed.outcome is CiOutcome.SUCCEEDED
        assert await stack.repository.errors(session.id) == []
        assert "CI_SESSION_TIMED_OUT" not in {event.type for event in stack.events.items}

    asyncio.run(scenario())


def test_maintenance_and_recovery_have_a_shared_concurrency_bound(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        maintenance = build_stack(
            tmp_path / "maintenance",
            assignment_enabled=False,
            maintenance_concurrency=3,
        )
        for index in range(8):
            await maintenance.service.create(
                provider=CiProvider.LOCAL,
                external_run_id=f"waiting-{index}",
                requested_by="maintenance",
                bench_request=request(),
                idempotency_key=f"waiting-{index}",
            )
        maintenance.repository.assignment_delay_seconds = 0.02

        assert await maintenance.service.process_maintenance() == 8
        assert maintenance.repository.maximum_active_assignments == 3

        recovery = build_stack(
            tmp_path / "recovery",
            assignment_enabled=False,
            maintenance_concurrency=2,
        )
        for index in range(7):
            await recovery.repository.create(
                CiSession(
                    provider=CiProvider.LOCAL,
                    external_run_id=f"failed-{index}",
                    requested_by="recovery",
                    status=CiSessionStatus.FAILED,
                    outcome=CiOutcome.FAILED,
                    created_at=recovery.clock(),
                    heartbeat_at=recovery.clock(),
                )
            )
        recovery.repository.cleanup_delay_seconds = 0.02

        assert await recovery.service.recover_incomplete() == 7
        assert recovery.repository.maximum_active_cleanups == 2
        assert all(
            session.status is CiSessionStatus.COMPLETED
            for session in recovery.repository.sessions.values()
        )

    asyncio.run(scenario())


def test_cleanup_success_and_failure_precedence(tmp_path: Path) -> None:
    async def scenario() -> None:
        success = build_stack(tmp_path / "success")
        session = await create_assigned(success)
        finalized = await success.service.finalize(session.id)
        assert finalized.outcome is CiOutcome.FAILED
        assert finalized.cleanup_status is CleanupStatus.SUCCEEDED
        output_types = {
            record.artifact_type
            for record in success.artifact_repository.items.values()
            if record.owner_id == session.id
        }
        assert output_types == {"junit", "workflow_summary"}

        failed = build_stack(tmp_path / "failed")
        failed.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        failed_session = await create_assigned(failed)
        firmware = await upload_firmware(failed, failed_session.id)
        running = await failed.service.start_workflow(
            failed_session.id,
            workflow_name="esp32-ci-test",
            inputs={"firmware": {"artifact_id": str(firmware.id)}},
        )
        assert running.workflow_run_id is not None
        run = failed.workflows.runs[running.workflow_run_id]
        failed.workflows.runs[run.id] = run.model_copy(
            update={
                "status": WorkflowRunStatus.SUCCEEDED,
                "completed_at": failed.clock(),
            }
        )
        synchronized = await failed.service.get(failed_session.id)
        assert synchronized.outcome is CiOutcome.SUCCEEDED
        failed.reservations.release_error = RuntimeError("release unavailable")
        completed = await failed.service.finalize(failed_session.id)
        cleanup = failed.repository.cleanups[failed_session.id]

        assert completed.outcome is CiOutcome.INFRASTRUCTURE_ERROR
        assert completed.cleanup_status is CleanupStatus.FAILED
        assert not cleanup.reservation_released
        assert cleanup.errors == ["reservation: release unavailable"]
        details = await failed.service.details(failed_session.id)
        assert details["errors"] == ["reservation: release unavailable"]
        assert details["backend"] == "simlab"
        assert details["heartbeat_interval_seconds"] == 30
        assert details["cleanup_timeout_seconds"] == 1

    asyncio.run(scenario())


def test_recovery_times_out_live_sessions_and_finalizes_terminal_states(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path, assignment_enabled=False)
        statuses = (
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
        outcomes = {
            CiSessionStatus.SUCCEEDED: CiOutcome.SUCCEEDED,
            CiSessionStatus.FAILED: CiOutcome.FAILED,
            CiSessionStatus.CANCELLED: CiOutcome.CANCELLED,
            CiSessionStatus.TIMED_OUT: CiOutcome.TIMED_OUT,
            CiSessionStatus.CLEANUP_PENDING: CiOutcome.SUCCEEDED,
        }
        for index, status in enumerate(statuses):
            session = CiSession(
                provider=CiProvider.LOCAL,
                external_run_id=f"recovery-{index}",
                requested_by="recovery",
                status=status,
                outcome=outcomes.get(status, CiOutcome.PENDING),
                created_at=stack.clock(),
                heartbeat_at=stack.clock(),
            )
            await stack.repository.create(session)

        assert await stack.service.recover_incomplete() == len(statuses)
        completed = sorted(
            stack.repository.sessions.values(), key=lambda item: item.external_run_id
        )
        assert all(session.status is CiSessionStatus.COMPLETED for session in completed)
        assert [session.outcome for session in completed] == [
            CiOutcome.TIMED_OUT,
            CiOutcome.TIMED_OUT,
            CiOutcome.TIMED_OUT,
            CiOutcome.TIMED_OUT,
            CiOutcome.TIMED_OUT,
            CiOutcome.SUCCEEDED,
            CiOutcome.FAILED,
            CiOutcome.CANCELLED,
            CiOutcome.TIMED_OUT,
            CiOutcome.SUCCEEDED,
        ]
        assert [await stack.repository.errors(session.id) for session in completed] == [
            ["AGENT_RESTARTED"],
            ["AGENT_RESTARTED"],
            ["AGENT_RESTARTED"],
            ["AGENT_RESTARTED"],
            ["AGENT_RESTARTED"],
            [],
            [],
            [],
            [],
            [],
        ]
        assert all(cleanup.artifacts_finalized for cleanup in stack.repository.cleanups.values())

    asyncio.run(scenario())


def test_workflow_labels_and_artifact_ownership_are_enforced(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = build_stack(tmp_path)
        session = await create_assigned(stack)
        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow(labels={"board": "stm32"})
        owned = await upload_firmware(stack, session.id)
        with pytest.raises(CiSessionConflictError, match="label requirements"):
            await stack.service.start_workflow(
                session.id,
                workflow_name="esp32-ci-test",
                inputs={"firmware": {"artifact_id": str(owned.id)}},
            )

        stack.workflows.definitions["esp32-ci-test"] = artifact_workflow()
        foreign = await upload_firmware(stack, session.id, owner_id=uuid4())
        with pytest.raises(InvalidArtifactError, match="does not belong"):
            await stack.service.start_workflow(
                session.id,
                workflow_name="esp32-ci-test",
                inputs={"firmware": {"artifact_id": str(foreign.id)}},
            )
        assert stack.workflows.start_calls == 0

    asyncio.run(scenario())
