from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from uuid import UUID

import pytest
from lab_platform.control_plane_core.errors import ReservationLeaseInvalidError
from lab_platform.control_plane_core.inventory import (
    InMemoryInventoryRepository,
    InventoryService,
)
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowCoordinator,
    DistributedWorkflowRequest,
    DistributedWorkflowReservationLifecycle,
    WorkflowArtifactTransferDescriptor,
)
from lab_platform.core.errors import BenchAlreadyReservedError, NoCompatibleBenchError
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    ArtifactReference,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
    WorkflowDefinition,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _run_async_test(
    function: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., None]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


def _agent(
    number: int,
    slug: str,
    *,
    status: AgentStatus = AgentStatus.ONLINE,
    location: str = "Tel Aviv",
    labels: Mapping[str, str] | None = None,
) -> AgentRecord:
    return AgentRecord(
        id=UUID(int=number),
        slug=slug,
        name=slug,
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location=location,
        labels=dict(labels or {"pool": "ci"}),
        registered_at=NOW - timedelta(days=1),
        last_connected_at=NOW - timedelta(minutes=5),
        last_seen_at=NOW,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )


def _bench(
    agent: AgentRecord,
    local_id: str,
    *,
    kind: GlobalBenchKind = GlobalBenchKind.PHYSICAL,
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    capabilities: frozenset[str] = frozenset({"firmware", "reset", "serial"}),
    labels: Mapping[str, str] | None = None,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"{agent.slug}/{local_id}",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=local_id,
        name=local_id,
        backend_id="hardware",
        kind=kind,
        status=status,
        health=HealthStatus.HEALTHY,
        capabilities=capabilities,
        labels=dict(labels or {"board": "esp32", "rack": "one"}),
        last_seen_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "reset-ci",
            "version": 2,
            "requirements": {
                "capabilities": ["reset"],
                "labels": {"board": "esp32"},
            },
            "steps": [{"action": "reset"}],
        }
    )


def _artifact_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "firmware-ci",
            "version": 2,
            "inputs": {
                "firmware": {"type": "artifact", "required": True},
                "version": {"type": "string", "required": True},
                "seconds": {"type": "integer", "default": 7},
                "enabled": {"type": "boolean", "default": True},
            },
            "requirements": {
                "capabilities": ["firmware"],
                "labels": {"board": "esp32"},
            },
            "steps": [
                {
                    "action": "flash",
                    "firmware": "${{ inputs.firmware }}",
                    "version": "${{ inputs.version }}-${{ inputs.enabled }}",
                },
                {"action": "wait", "seconds": "${{ inputs.seconds }}"},
            ],
        }
    )


class FakePresence:
    def __init__(self, agents: list[AgentRecord]) -> None:
        self.agents = agents

    async def list_agents(self) -> list[AgentRecord]:
        return list(self.agents)


class FakeReservations:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_once: set[str] = set()
        self.reserved: set[str] = set()
        self.records: dict[str, CoordinatedReservationLease] = {}
        self.lock = asyncio.Lock()
        self.entered: asyncio.Event | None = None
        self.resume: asyncio.Event | None = None
        self.return_state = ReservationLeaseState.ACTIVE
        self.invalid_once: set[str] = set()
        self.non_race_error = False
        self.lease_valid_until = NOW + timedelta(hours=1)
        self.release_calls: list[dict[str, object]] = []

    async def grant(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        idempotency_key: str,
        reservation_duration_seconds: int | None = None,
        lease_ttl_seconds: int | None = None,
        source: ReservationSource = ReservationSource.API,
        metadata: Mapping[str, str] | None = None,
    ) -> CoordinatedReservationLease:
        self.calls.append(
            {
                "agent_id": agent_id,
                "bench_id": bench_id,
                "owner": owner,
                "idempotency_key": idempotency_key,
                "metadata": dict(metadata or {}),
            }
        )
        if self.entered is not None:
            self.entered.set()
        if self.resume is not None:
            await self.resume.wait()
        if bench_id in self.fail_once:
            self.fail_once.remove(bench_id)
            raise BenchAlreadyReservedError("lost reservation race", bench_id=bench_id)
        if bench_id in self.invalid_once:
            self.invalid_once.remove(bench_id)
            raise ReservationLeaseInvalidError(
                "atomic eligibility race",
                agent_id=str(agent_id),
                bench_id=bench_id,
            )
        if self.non_race_error:
            raise ReservationLeaseInvalidError(
                "idempotency mismatch",
                idempotency_key=idempotency_key,
            )
        await asyncio.sleep(0)
        async with self.lock:
            replay = self.records.get(idempotency_key)
            if replay is not None:
                return replay
            if bench_id in self.reserved:
                raise BenchAlreadyReservedError("already reserved", bench_id=bench_id)
            self.reserved.add(bench_id)
            number = 10_000 + len(self.records)
            reservation_id = UUID(int=number)
            reservation = Reservation(
                id=reservation_id,
                bench_id=bench_id,
                owner=owner,
                created_at=NOW,
                requested_at=NOW,
                starts_at=NOW,
                ends_at=NOW + timedelta(hours=1),
                activated_at=NOW,
                status=ReservationStatus.ACTIVE,
                source=source,
                metadata=dict(metadata or {}),
                idempotency_key=idempotency_key,
            )
            lease = ReservationLease(
                reservation_id=reservation_id,
                agent_id=agent_id,
                bench_id=bench_id,
                owner=owner,
                valid_from=min(NOW, self.lease_valid_until - timedelta(seconds=1)),
                valid_until=self.lease_valid_until,
                lease_version=1,
            )
            unknown = self.return_state is ReservationLeaseState.UNKNOWN
            record = CoordinatedReservationLease(
                reservation=reservation,
                lease=lease,
                state=self.return_state,
                revision=2,
                unknown_since=NOW if unknown else None,
                reconciliation_deadline=NOW + timedelta(minutes=5) if unknown else None,
            )
            self.records[idempotency_key] = record
            return record

    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease:
        for record in self.records.values():
            if record.reservation.id == reservation_id:
                return record
        raise AssertionError(f"Unknown reservation {reservation_id}")

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
    ) -> CoordinatedReservationLease:
        current = await self.get(reservation_id)
        self.release_calls.append(
            {
                "reservation_id": reservation_id,
                "owner": owner,
                "expected_lease_version": expected_lease_version,
                "idempotency_key": idempotency_key,
            }
        )
        if current.state is ReservationLeaseState.RELEASED:
            return current
        assert current.reservation.owner == owner
        assert current.lease.lease_version == expected_lease_version
        released = CoordinatedReservationLease(
            reservation=current.reservation.model_copy(
                update={"status": ReservationStatus.RELEASED, "released_at": NOW}
            ),
            lease=current.lease.model_copy(update={"released_at": NOW}),
            state=ReservationLeaseState.RELEASED,
            revision=current.revision + 1,
        )
        for key, record in tuple(self.records.items()):
            if record.reservation.id == reservation_id:
                self.records[key] = released
                break
        self.reserved.discard(current.reservation.bench_id)
        return released


class FakeArtifacts:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.descriptors: dict[str, WorkflowArtifactTransferDescriptor] = {}
        self.override_agent_id: UUID | None = None

    async def issue_download(
        self,
        *,
        agent_id: UUID,
        input_name: str,
        artifact_id: UUID,
        target_path: str,
        idempotency_key: str,
    ) -> WorkflowArtifactTransferDescriptor:
        existing = self.descriptors.get(idempotency_key)
        if existing is not None:
            return existing
        self.calls.append(
            {
                "agent_id": agent_id,
                "input_name": input_name,
                "artifact_id": artifact_id,
                "target_path": target_path,
            }
        )
        descriptor = WorkflowArtifactTransferDescriptor(
            input_name=input_name,
            agent_id=self.override_agent_id or agent_id,
            artifact_id=artifact_id,
            sha256="a" * 64,
            size_bytes=1024,
            target_path=target_path,
        )
        self.descriptors[idempotency_key] = descriptor
        return descriptor


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.created: dict[str, tuple[RemoteCommand, DistributedOperation]] = {}
        self.omit_operation = False
        self.create_error: Exception | None = None
        self.dispatch_error: Exception | None = None
        self.dispatch_calls: list[dict[str, object]] = []

    async def create(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        command_type: RemoteCommandType,
        payload: Mapping[str, Any],
        expires_at: datetime,
        idempotency_key: str,
        reservation_lease: ReservationLease | None = None,
        operation_type: str | None = None,
        dispatch: bool = True,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        if self.create_error is not None:
            raise self.create_error
        existing = self.created.get(idempotency_key)
        if existing is not None:
            return existing
        assert reservation_lease is not None
        self.calls.append(
            {
                "agent_id": agent_id,
                "bench_id": bench_id,
                "command_type": command_type,
                "payload": dict(payload),
                "expires_at": expires_at,
                "lease": reservation_lease,
                "dispatch": dispatch,
            }
        )
        command = RemoteCommand(
            id=UUID(int=30_000 + len(self.calls)),
            agent_id=agent_id,
            bench_id=bench_id,
            command_type=command_type,
            payload=dict(payload),
            status=RemoteCommandStatus.CREATED,
            created_at=NOW,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            reservation_id=reservation_lease.reservation_id,
            lease_version=reservation_lease.lease_version,
        )
        operation = DistributedOperation(
            id=UUID(int=40_000 + len(self.calls)),
            remote_command_id=command.id,
            agent_id=agent_id,
            bench_id=bench_id,
            reservation_id=reservation_lease.reservation_id,
            operation_type=operation_type or command_type.value,
            created_at=NOW,
        )
        self.created[idempotency_key] = (command, operation)
        return command, None if self.omit_operation else operation

    async def dispatch(
        self,
        command_id: UUID,
        *,
        reservation_lease: ReservationLease | None = None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        self.dispatch_calls.append(
            {"command_id": command_id, "reservation_lease": reservation_lease}
        )
        if self.dispatch_error is not None:
            raise self.dispatch_error
        for key, (command, operation) in tuple(self.created.items()):
            if command.id != command_id:
                continue
            dispatched_command = command.model_copy(
                update={"status": RemoteCommandStatus.DISPATCHED, "dispatched_at": NOW}
            )
            dispatched_operation = operation.model_copy(
                update={
                    "status": DistributedOperationStatus.DISPATCHED,
                    "dispatched_at": NOW,
                }
            )
            self.created[key] = (dispatched_command, dispatched_operation)
            return dispatched_command, dispatched_operation
        raise AssertionError(f"Unknown command {command_id}")

    async def get_command(self, command_id: UUID) -> RemoteCommand | None:
        return next(
            (command for command, _ in self.created.values() if command.id == command_id),
            None,
        )

    async def list_commands(
        self,
        *,
        agent_id: UUID | None = None,
        statuses: Iterable[RemoteCommandStatus] | None = None,
        limit: int = 500,
    ) -> list[RemoteCommand]:
        selected = set(statuses) if statuses is not None else None
        return [
            command
            for command, _ in self.created.values()
            if (agent_id is None or command.agent_id == agent_id)
            and (selected is None or command.status in selected)
        ][:limit]

    async def list_operations(
        self,
        *,
        agent_id: UUID | None,
        statuses: Iterable[DistributedOperationStatus],
        limit: int,
    ) -> list[DistributedOperation]:
        selected = set(statuses)
        return [
            operation
            for _, operation in self.created.values()
            if (agent_id is None or operation.agent_id == agent_id) and operation.status in selected
        ][:limit]

    async def list_terminal_workflow_commands(
        self,
        *,
        limit: int,
    ) -> list[RemoteCommand]:
        terminal_commands = {
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.EXPIRED,
        }
        terminal_operations = {
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }
        return [
            command
            for command, operation in self.created.values()
            if command.status in terminal_commands or operation.status in terminal_operations
        ][:limit]


def _coordinator(
    agents: list[AgentRecord],
    benches: list[GlobalBenchRecord],
    *,
    reservations: FakeReservations | None = None,
    artifacts: FakeArtifacts | None = None,
    commands: FakeCommands | None = None,
) -> tuple[DistributedWorkflowCoordinator, FakeReservations, FakeArtifacts, FakeCommands]:
    lease_service = reservations or FakeReservations()
    artifact_service = artifacts or FakeArtifacts()
    command_service = commands or FakeCommands()
    coordinator = DistributedWorkflowCoordinator(
        InventoryService(InMemoryInventoryRepository(benches)),
        FakePresence(agents),
        lease_service,
        artifact_service,
        command_service,
        clock=lambda: NOW,
    )
    return coordinator, lease_service, artifact_service, command_service


@_run_async_test
async def test_selects_deterministically_without_accepting_an_agent_id() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    draining = _agent(3, "agent-c", status=AgentStatus.DRAINING)
    missing_capability = _agent(4, "agent-0")
    wrong_location = _agent(5, "agent-1", location="Haifa")
    wrong_agent_label = _agent(6, "agent-2", labels={"pool": "development"})
    simulated_owner = _agent(7, "agent-3")
    benches = [
        _bench(second_agent, "bench-b"),
        _bench(first_agent, "bench-z"),
        _bench(draining, "bench-a"),
        _bench(missing_capability, "bench-a", capabilities=frozenset({"serial"})),
        _bench(wrong_location, "bench-a"),
        _bench(wrong_agent_label, "bench-a"),
        _bench(simulated_owner, "bench-a", kind=GlobalBenchKind.SIMULATED),
        _bench(first_agent, "wrong-label", labels={"board": "nrf52", "rack": "one"}),
    ]
    coordinator, reservations, _, commands = _coordinator(
        [
            second_agent,
            draining,
            missing_capability,
            wrong_location,
            wrong_agent_label,
            simulated_owner,
            first_agent,
        ],
        benches,
    )

    request = DistributedWorkflowRequest(
        definition=_workflow(),
        owner="ci/build-42",
        idempotency_key="run-42",
        kind=GlobalBenchKind.PHYSICAL,
        location="TEL AVIV",
        bench_labels={"rack": "one"},
        agent_labels={"pool": "ci"},
    )
    result = await coordinator.run(request)

    assert "agent_id" not in DistributedWorkflowRequest.__dataclass_fields__
    assert result.bench.id == "agent-a/bench-z"
    assert result.agent.id == first_agent.id
    assert reservations.calls[0]["agent_id"] == first_agent.id
    assert commands.calls[0]["command_type"] is RemoteCommandType.RUN_WORKFLOW
    assert commands.calls[0]["lease"] == result.reservation.lease
    assert commands.calls[0]["payload"]["definition"] == result.definition.model_dump(mode="json")
    assert commands.calls[0]["payload"]["owner"] == "ci/build-42"


@_run_async_test
async def test_preferred_location_and_bench_labels_rank_without_excluding_fallbacks() -> None:
    fallback_agent = _agent(70, "agent-a", location="Haifa")
    preferred_agent = _agent(71, "agent-z", location="Jerusalem")
    benches = [
        _bench(fallback_agent, "bench-a", labels={"board": "esp32", "rack": "cold"}),
        _bench(preferred_agent, "bench-z", labels={"board": "esp32", "rack": "warm"}),
    ]
    coordinator, _, _, _ = _coordinator([fallback_agent, preferred_agent], benches)

    dispatch = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="ci/preference",
            idempotency_key="preferred-route",
            preferred_location="jerusalem",
            preferred_bench_labels={"rack": "warm"},
        )
    )

    assert dispatch.agent.id == preferred_agent.id
    assert dispatch.bench.id == "agent-z/bench-z"


@_run_async_test
async def test_explicit_global_bench_routes_via_its_owner_and_respects_filters() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first = _bench(first_agent, "bench-a")
    second = _bench(second_agent, "bench-b")
    coordinator, _, _, _ = _coordinator([first_agent, second_agent], [first, second])

    result = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="alice",
            idempotency_key="explicit-b",
            bench_id=second.id,
        )
    )
    assert result.bench == second
    assert result.agent == second_agent

    with pytest.raises(NoCompatibleBenchError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="alice",
                idempotency_key="explicit-kind-mismatch",
                bench_id=first.id,
                kind=GlobalBenchKind.SIMULATED,
            )
        )


@_run_async_test
async def test_reservation_race_falls_through_to_the_next_stable_candidate() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first = _bench(first_agent, "bench-a")
    second = _bench(second_agent, "bench-b")
    reservations = FakeReservations()
    reservations.fail_once.add(first.id)
    coordinator, _, _, _ = _coordinator(
        [first_agent, second_agent],
        [second, first],
        reservations=reservations,
    )

    result = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="ci",
            idempotency_key="race-fallback",
        )
    )

    assert [call["bench_id"] for call in reservations.calls] == [first.id, second.id]
    assert result.bench == second


@_run_async_test
@pytest.mark.parametrize(
    "agent_status,bench_status",
    [
        (AgentStatus.OFFLINE, GlobalBenchStatus.ONLINE),
        (AgentStatus.DRAINING, GlobalBenchStatus.ONLINE),
        (AgentStatus.DRAINED, GlobalBenchStatus.ONLINE),
        (AgentStatus.DEGRADED, GlobalBenchStatus.ONLINE),
        (AgentStatus.ONLINE, GlobalBenchStatus.DEGRADED),
        (AgentStatus.ONLINE, GlobalBenchStatus.OFFLINE),
    ],
)
async def test_offline_draining_degraded_routes_never_receive_work(
    agent_status: AgentStatus,
    bench_status: GlobalBenchStatus,
) -> None:
    agent = _agent(1, "agent-a", status=agent_status)
    bench = _bench(agent, "bench-a", status=bench_status)
    coordinator, reservations, artifacts, commands = _coordinator([agent], [bench])

    with pytest.raises(NoCompatibleBenchError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="ci",
                idempotency_key=f"ineligible-{agent_status}-{bench_status}",
            )
        )
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []


@_run_async_test
async def test_typed_inputs_are_resolved_and_artifacts_are_agent_scoped() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    coordinator, _, artifacts, commands = _coordinator([agent], [bench])
    artifact_id = UUID(int=55)

    result = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_artifact_workflow(),
            owner="ci",
            idempotency_key="artifact-run",
            inputs={
                "firmware": {"artifact_id": str(artifact_id)},
                "version": "1.2.3",
                "seconds": "9",
                "enabled": "false",
            },
        )
    )

    assert result.inputs == {
        "firmware": result.inputs["firmware"],
        "version": "1.2.3",
        "seconds": 9,
        "enabled": False,
    }
    assert isinstance(result.inputs["firmware"], ArtifactReference)
    assert result.inputs["firmware"].artifact_id == artifact_id
    assert str(result.definition.steps[0].firmware) == f"artifacts/{artifact_id}"  # type: ignore[union-attr]
    assert result.definition.steps[0].version == "1.2.3-false"  # type: ignore[union-attr]
    assert result.definition.steps[1].seconds == 9  # type: ignore[union-attr]
    assert artifacts.calls == [
        {
            "agent_id": agent.id,
            "input_name": "firmware",
            "artifact_id": artifact_id,
            "target_path": f"artifacts/{artifact_id}",
        }
    ]
    transfer_payload = commands.calls[0]["payload"]["artifact_transfers"][0]
    assert transfer_payload["agent_id"] == str(agent.id)
    assert transfer_payload["artifact_id"] == str(artifact_id)
    assert commands.calls[0]["payload"]["inputs"] == {
        "firmware": {"artifact_id": str(artifact_id)},
        "version": "1.2.3",
        "seconds": 9,
        "enabled": False,
    }
    dispatched_definition = commands.calls[0]["payload"]["definition"]
    assert dispatched_definition == _artifact_workflow().model_dump(mode="json")
    assert dispatched_definition["steps"][0]["firmware"] == "${{ inputs.firmware }}"
    assert commands.calls[0]["payload"]["reservation_lease"] == (
        result.reservation.lease.model_dump(mode="json")
    )
    assert commands.calls[0]["expires_at"] == NOW + timedelta(hours=1)


@_run_async_test
async def test_input_validation_happens_before_reservation_or_staging() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    coordinator, reservations, artifacts, commands = _coordinator([agent], [bench])

    with pytest.raises(WorkflowInvalidError, match="firmware.*required"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_artifact_workflow(),
                owner="ci",
                idempotency_key="missing-artifact",
                inputs={"version": "1.0", "seconds": True},
            )
        )
    with pytest.raises(WorkflowInvalidError, match="must be an integer"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_artifact_workflow(),
                owner="ci",
                idempotency_key="bad-integer",
                inputs={
                    "firmware": {"artifact_id": str(UUID(int=8))},
                    "version": "1.0",
                    "seconds": True,
                },
            )
        )
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []


@_run_async_test
async def test_concurrent_retries_are_coalesced_and_success_is_replayed() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    reservations.entered = asyncio.Event()
    reservations.resume = asyncio.Event()
    coordinator, _, artifacts, commands = _coordinator([agent], [bench], reservations=reservations)
    request = DistributedWorkflowRequest(
        definition=_workflow(),
        owner="ci",
        idempotency_key="same-run",
    )

    first = asyncio.create_task(coordinator.run(request))
    await reservations.entered.wait()
    second = asyncio.create_task(coordinator.run(request))
    await asyncio.sleep(0)
    reservations.resume.set()
    first_result, second_result = await asyncio.gather(first, second)
    replay = await coordinator.run(request)

    assert first_result is second_result
    assert replay is first_result
    assert len(reservations.calls) == 1
    assert len(artifacts.calls) == 0
    assert len(commands.calls) == 1

    with pytest.raises(WorkflowInvalidError, match="idempotency key"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="another-owner",
                idempotency_key="same-run",
            )
        )


@_run_async_test
async def test_concurrent_distinct_runs_claim_different_benches_under_race() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first = _bench(first_agent, "bench-a")
    second = _bench(second_agent, "bench-b")
    coordinator, reservations, _, commands = _coordinator(
        [first_agent, second_agent], [first, second]
    )

    results = await asyncio.gather(
        coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci/one", idempotency_key="parallel-one"
            )
        ),
        coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci/two", idempotency_key="parallel-two"
            )
        ),
    )

    assert {result.bench.id for result in results} == {first.id, second.id}
    assert len(reservations.calls) == 3
    assert len(commands.calls) == 2


@_run_async_test
async def test_unknown_unconfirmed_lease_is_never_dispatched() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    reservations.return_state = ReservationLeaseState.UNKNOWN
    coordinator, _, artifacts, commands = _coordinator([agent], [bench], reservations=reservations)

    with pytest.raises(ReservationLeaseInvalidError, match="not semantically confirmed"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="ci",
                idempotency_key="unknown-lease",
            )
        )
    assert artifacts.calls == []
    assert commands.calls == []
    assert len(reservations.release_calls) == 1


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"input_name": ""}, "input_name"),
        ({"sha256": "not-a-digest"}, "sha256"),
        ({"size_bytes": -1}, "size_bytes"),
        ({"target_path": "../escape"}, "safe relative path"),
    ],
)
def test_artifact_transfer_descriptor_rejects_unsafe_capabilities(
    updates: dict[str, Any],
    message: str,
) -> None:
    values: dict[str, Any] = {
        "input_name": "firmware",
        "agent_id": UUID(int=1),
        "artifact_id": UUID(int=2),
        "sha256": "a" * 64,
        "size_bytes": 10,
        "target_path": "artifacts/firmware",
    }
    values.update(updates)
    with pytest.raises(ValueError, match=message):
        WorkflowArtifactTransferDescriptor(**values)


def test_coordinator_rejects_an_unbounded_idempotency_cache() -> None:
    agent = _agent(1, "agent-a")
    with pytest.raises(ValueError, match="maximum_idempotency_entries"):
        DistributedWorkflowCoordinator(
            InventoryService(InMemoryInventoryRepository([_bench(agent, "bench-a")])),
            FakePresence([agent]),
            FakeReservations(),
            FakeArtifacts(),
            FakeCommands(),
            maximum_idempotency_entries=0,
        )


@pytest.mark.parametrize(
    ("request_updates", "message"),
    [
        ({"owner": ""}, "owner must not be empty"),
        ({"owner": "x" * 201}, "owner cannot exceed"),
        ({"idempotency_key": ""}, "idempotency_key must not be empty"),
        ({"idempotency_key": "x" * 501}, "idempotency_key cannot exceed"),
        ({"reservation_duration_seconds": 0}, "reservation_duration_seconds"),
        ({"lease_ttl_seconds": False}, "lease_ttl_seconds"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"manage_reservation_lifecycle": 1}, "manage_reservation_lifecycle"),
    ],
)
@_run_async_test
async def test_request_bounds_are_checked_before_selection(
    request_updates: dict[str, Any],
    message: str,
) -> None:
    agent = _agent(1, "agent-a")
    coordinator, reservations, _, _ = _coordinator([agent], [_bench(agent, "bench-a")])
    values: dict[str, Any] = {
        "definition": _workflow(),
        "owner": "ci",
        "idempotency_key": "bounded",
    }
    values.update(request_updates)
    with pytest.raises(ValueError, match=message):
        await coordinator.run(DistributedWorkflowRequest(**values))
    assert reservations.calls == []


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (
            {
                "firmware": {"artifact_id": str(UUID(int=8))},
                "version": "1.0",
                "extra": "no",
            },
            "Unknown workflow inputs",
        ),
        (
            {"firmware": {"artifact_id": str(UUID(int=8))}, "version": 1},
            "version.*must be a string",
        ),
        (
            {
                "firmware": {"artifact_id": str(UUID(int=8))},
                "version": "1.0",
                "seconds": "nine",
            },
            "seconds.*must be an integer",
        ),
        (
            {
                "firmware": {"artifact_id": str(UUID(int=8))},
                "version": "1.0",
                "enabled": "maybe",
            },
            "enabled.*must be a boolean",
        ),
        ({"firmware": "not-an-artifact", "version": "1.0"}, "artifact reference"),
        (
            {
                1: "invalid-name",
                "firmware": {"artifact_id": str(UUID(int=8))},
                "version": "1.0",
            },
            "input names must be strings",
        ),
    ],
)
@_run_async_test
async def test_all_typed_input_failures_precede_reservation(
    inputs: Any,
    message: str,
) -> None:
    agent = _agent(1, "agent-a")
    coordinator, reservations, _, _ = _coordinator([agent], [_bench(agent, "bench-a")])
    with pytest.raises(WorkflowInvalidError, match=message):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_artifact_workflow(),
                owner="ci",
                idempotency_key="invalid-input",
                inputs=inputs,
            )
        )
    assert reservations.calls == []


@_run_async_test
async def test_optional_typed_and_legacy_workflows_are_both_supported() -> None:
    agent = _agent(1, "agent-a")
    first = _bench(agent, "bench-a")
    coordinator, _, _, _ = _coordinator([agent], [first])
    typed = WorkflowDefinition.model_validate(
        {
            "name": "optional-input",
            "version": 2,
            "inputs": {"note": {"type": "string"}},
            "requirements": {"capabilities": ["reset"]},
            "steps": [{"action": "reset"}],
        }
    )
    typed_result = await coordinator.run(
        DistributedWorkflowRequest(
            definition=typed,
            owner="ci",
            idempotency_key="optional-input",
        )
    )
    assert typed_result.inputs == {}

    second_agent = _agent(2, "agent-b")
    legacy_coordinator, _, _, _ = _coordinator([second_agent], [_bench(second_agent, "bench-b")])
    legacy = WorkflowDefinition.model_validate(
        {
            "name": "legacy-reset",
            "version": 1,
            "requirements": {"capabilities": ["reset"]},
            "steps": [{"action": "reset"}],
        }
    )
    legacy_result = await legacy_coordinator.run(
        DistributedWorkflowRequest(
            definition=legacy,
            owner="ci",
            idempotency_key="legacy-inputs",
        )
    )
    assert legacy_result.definition.version == 1


@_run_async_test
async def test_incomplete_constructed_definition_is_revalidated_centrally() -> None:
    agent = _agent(1, "agent-a")
    coordinator, reservations, _, _ = _coordinator([agent], [_bench(agent, "bench-a")])
    incomplete = _workflow().model_copy(update={"name": "INVALID NAME", "version": 0, "steps": []})
    with pytest.raises(WorkflowInvalidError, match="incomplete or invalid"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=incomplete,
                owner="ci",
                idempotency_key="invalid-definition",
            )
        )
    assert reservations.calls == []


@_run_async_test
async def test_atomic_eligibility_errors_fall_through_but_other_lease_errors_do_not() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first = _bench(first_agent, "bench-a")
    second = _bench(second_agent, "bench-b")
    reservations = FakeReservations()
    reservations.invalid_once.add(first.id)
    coordinator, _, _, _ = _coordinator(
        [first_agent, second_agent], [first, second], reservations=reservations
    )
    result = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(), owner="ci", idempotency_key="eligibility-race"
        )
    )
    assert result.bench == second

    failing = FakeReservations()
    failing.non_race_error = True
    failing_coordinator, _, _, _ = _coordinator([first_agent], [first], reservations=failing)
    with pytest.raises(ReservationLeaseInvalidError, match="idempotency mismatch"):
        await failing_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci", idempotency_key="bad-lease"
            )
        )


@_run_async_test
async def test_all_atomic_reservation_races_return_no_compatible_bench() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first = _bench(first_agent, "bench-a")
    second = _bench(second_agent, "bench-b")
    reservations = FakeReservations()
    reservations.fail_once.update({first.id, second.id})
    coordinator, _, _, _ = _coordinator(
        [first_agent, second_agent], [first, second], reservations=reservations
    )
    with pytest.raises(NoCompatibleBenchError) as captured:
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci", idempotency_key="all-raced"
            )
        )
    assert captured.value.details["raced_benches"] == [first.id, second.id]


@_run_async_test
async def test_invalid_artifact_port_results_prevent_dispatch() -> None:
    agent = _agent(1, "agent-a")
    artifacts = FakeArtifacts()
    artifacts.override_agent_id = UUID(int=999)
    coordinator, reservations, _, commands = _coordinator(
        [agent], [_bench(agent, "bench-a")], artifacts=artifacts
    )
    with pytest.raises(ValueError, match="descriptor"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_artifact_workflow(),
                owner="ci",
                idempotency_key="descriptor-mismatch",
                inputs={
                    "firmware": {"artifact_id": str(UUID(int=8))},
                    "version": "1.0",
                },
            )
        )
    assert commands.calls == []
    assert len(reservations.release_calls) == 1


@_run_async_test
async def test_expired_lease_and_missing_operation_are_rejected() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    expired = FakeReservations()
    expired.lease_valid_until = NOW
    coordinator, _, _, commands = _coordinator([agent], [bench], reservations=expired)
    with pytest.raises(ReservationLeaseInvalidError, match="expired before command dispatch"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci", idempotency_key="expired-lease"
            )
        )
    assert commands.calls == []
    assert len(expired.release_calls) == 1

    second_agent = _agent(2, "agent-b")
    no_operation = FakeCommands()
    no_operation.omit_operation = True
    second_coordinator, second_reservations, _, _ = _coordinator(
        [second_agent],
        [_bench(second_agent, "bench-b")],
        commands=no_operation,
    )
    with pytest.raises(RuntimeError, match="did not create"):
        await second_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(), owner="ci", idempotency_key="missing-operation"
            )
        )
    assert len(second_reservations.release_calls) == 1


@_run_async_test
async def test_creation_failure_releases_but_dispatch_failure_remains_fenced() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    creation_failure = FakeCommands()
    creation_failure.create_error = RuntimeError("create failed")
    coordinator, reservations, _, _ = _coordinator(
        [agent],
        [bench],
        commands=creation_failure,
    )

    with pytest.raises(RuntimeError, match="create failed"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="ci",
                idempotency_key="create-failure",
            )
        )
    assert len(reservations.release_calls) == 1
    assert creation_failure.dispatch_calls == []

    dispatch_agent = _agent(2, "agent-b")
    dispatch_failure = FakeCommands()
    dispatch_failure.dispatch_error = RuntimeError("delivery uncertain")
    dispatch_coordinator, dispatch_reservations, _, _ = _coordinator(
        [dispatch_agent],
        [_bench(dispatch_agent, "bench-b")],
        commands=dispatch_failure,
    )
    with pytest.raises(RuntimeError, match="delivery uncertain"):
        await dispatch_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner="ci",
                idempotency_key="dispatch-failure",
            )
        )
    assert dispatch_failure.calls[0]["dispatch"] is False
    assert len(dispatch_failure.dispatch_calls) == 1
    assert dispatch_reservations.release_calls == []


@pytest.mark.parametrize(
    ("command_status", "operation_status"),
    [
        (RemoteCommandStatus.SUCCEEDED, DistributedOperationStatus.DISPATCHED),
        (RemoteCommandStatus.FAILED, DistributedOperationStatus.DISPATCHED),
        (RemoteCommandStatus.CANCELLED, DistributedOperationStatus.DISPATCHED),
        (RemoteCommandStatus.EXPIRED, DistributedOperationStatus.DISPATCHED),
        (RemoteCommandStatus.DISPATCHED, DistributedOperationStatus.SUCCEEDED),
        (RemoteCommandStatus.DISPATCHED, DistributedOperationStatus.FAILED),
        (RemoteCommandStatus.DISPATCHED, DistributedOperationStatus.CANCELLED),
    ],
)
@_run_async_test
async def test_terminal_command_or_operation_releases_managed_reservation_idempotently(
    command_status: RemoteCommandStatus,
    operation_status: DistributedOperationStatus,
) -> None:
    agent = _agent(1, "agent-a")
    coordinator, reservations, _, commands = _coordinator(
        [agent],
        [_bench(agent, "bench-a")],
    )
    dispatch = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="api/alice",
            idempotency_key=f"terminal-{command_status}-{operation_status}",
        )
    )
    key, (command, operation) = next(iter(commands.created.items()))
    command_updates: dict[str, object] = {"status": command_status}
    if command_status in {
        RemoteCommandStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED,
        RemoteCommandStatus.CANCELLED,
        RemoteCommandStatus.EXPIRED,
    }:
        command_updates["completed_at"] = NOW + timedelta(seconds=1)
    operation_updates: dict[str, object] = {"status": operation_status}
    if operation_status in {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }:
        operation_updates["completed_at"] = NOW + timedelta(seconds=1)
    commands.created[key] = (
        command.model_copy(update=command_updates),
        operation.model_copy(update=operation_updates),
    )
    lifecycle = DistributedWorkflowReservationLifecycle(commands, reservations)

    assert dispatch.reservation.reservation.metadata["reservation_lifecycle"] == "workflow"
    assert await lifecycle.release_terminal() == 1
    assert await lifecycle.release_terminal() == 0
    assert len(reservations.release_calls) == 1


@_run_async_test
async def test_terminal_cleanup_skips_caller_managed_and_newer_lease_generations() -> None:
    caller_agent = _agent(1, "agent-a")
    caller_coordinator, caller_reservations, _, caller_commands = _coordinator(
        [caller_agent],
        [_bench(caller_agent, "bench-a")],
    )
    await caller_coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="ci/session",
            idempotency_key="caller-managed",
            manage_reservation_lifecycle=False,
        )
    )
    caller_key, (caller_command, caller_operation) = next(iter(caller_commands.created.items()))
    caller_commands.created[caller_key] = (
        caller_command.model_copy(
            update={
                "status": RemoteCommandStatus.SUCCEEDED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        ),
        caller_operation,
    )
    caller_lifecycle = DistributedWorkflowReservationLifecycle(
        caller_commands,
        caller_reservations,
    )
    assert await caller_lifecycle.release_terminal() == 0
    assert caller_reservations.release_calls == []
    assert (
        next(iter(caller_reservations.records.values())).reservation.metadata[
            "reservation_lifecycle"
        ]
        == "caller"
    )

    fenced_agent = _agent(2, "agent-b")
    fenced_coordinator, fenced_reservations, _, fenced_commands = _coordinator(
        [fenced_agent],
        [_bench(fenced_agent, "bench-b")],
    )
    await fenced_coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner="api/bob",
            idempotency_key="newer-generation",
        )
    )
    fenced_key, (fenced_command, fenced_operation) = next(iter(fenced_commands.created.items()))
    fenced_commands.created[fenced_key] = (
        fenced_command.model_copy(
            update={
                "status": RemoteCommandStatus.FAILED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        ),
        fenced_operation,
    )
    reservation_key, current = next(iter(fenced_reservations.records.items()))
    fenced_reservations.records[reservation_key] = CoordinatedReservationLease(
        reservation=current.reservation,
        lease=current.lease.model_copy(update={"lease_version": 2}),
        state=current.state,
        revision=current.revision + 1,
    )
    fenced_lifecycle = DistributedWorkflowReservationLifecycle(
        fenced_commands,
        fenced_reservations,
    )
    assert await fenced_lifecycle.release_terminal() == 0
    assert fenced_reservations.release_calls == []


@_run_async_test
async def test_completed_idempotency_entries_are_bounded() -> None:
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    reservations = FakeReservations()
    artifacts = FakeArtifacts()
    commands = FakeCommands()
    coordinator = DistributedWorkflowCoordinator(
        InventoryService(
            InMemoryInventoryRepository(
                [_bench(first_agent, "bench-a"), _bench(second_agent, "bench-b")]
            )
        ),
        FakePresence([first_agent, second_agent]),
        reservations,
        artifacts,
        commands,
        clock=lambda: NOW,
        maximum_idempotency_entries=1,
    )
    await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(), owner="ci/one", idempotency_key="cache-one"
        )
    )
    await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(), owner="ci/two", idempotency_key="cache-two"
        )
    )
    assert len(coordinator._requests) == 1
