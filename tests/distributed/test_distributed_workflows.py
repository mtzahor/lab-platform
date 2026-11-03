from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from uuid import UUID

import pytest
from lab_platform.control_plane_core.errors import (
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
)
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
    WorkflowAuthorisationService,
)
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    BenchAlreadyReservedError,
    NoCompatibleBenchError,
    PermissionDeniedError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    ActorContext,
    AgentRecord,
    AgentStatus,
    ArtifactReference,
    AuthenticationContext,
    AuthorisationResource,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Principal,
    PrincipalType,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationOwner,
    ReservationSource,
    ReservationStatus,
    ResourceType,
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
        organisation_id=agent.organisation_id,
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
                # Stable workflows use ``flash`` while the legacy Agent fixture below
                # still reports ``firmware``; selection must bridge that 1.0 alias.
                "capabilities": ["flash"],
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

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[AgentRecord]:
        return [
            agent
            for agent in self.agents
            if organisation_id is None or agent.organisation_id == organisation_id
        ]


class FakeWorkflowDefinitionCatalog:
    def __init__(self, definitions: Iterable[WorkflowDefinition]) -> None:
        self.definitions = {
            (definition.organisation_id, definition.name, definition.version): definition
            for definition in definitions
        }
        self.calls: list[tuple[UUID, str, int | None]] = []

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None:
        if organisation_id is None:
            return None
        self.calls.append((organisation_id, name, version))
        if version is not None:
            return self.definitions.get((organisation_id, name, version))
        matches = [
            definition
            for (scope, candidate_name, _), definition in self.definitions.items()
            if scope == organisation_id and candidate_name == name
        ]
        return max(matches, key=lambda item: item.version) if matches else None


class FakeWorkflowAuthorisation:
    def __init__(
        self,
        *,
        workflow_allowed: bool = True,
        allowed_benches: set[str] | None = None,
    ) -> None:
        self.workflow_allowed = workflow_allowed
        self.allowed_benches = allowed_benches or set()
        self.evaluated: list[tuple[str, AuthorisationResource]] = []
        self.required: list[tuple[str, AuthorisationResource]] = []
        self.audited: list[tuple[str, str, str | None, dict[str, object]]] = []

    async def is_allowed(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Iterable[str] | None = None,
    ) -> bool:
        del principal, credential_restrictions
        self.evaluated.append((permission, resource))
        return resource.id in self.allowed_benches

    async def require(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Iterable[str] | None = None,
    ) -> None:
        del principal, credential_restrictions
        self.required.append((permission, resource))
        allowed = self.workflow_allowed if permission == "workflows:run" else False
        if not allowed:
            raise PermissionDeniedError(
                "permission denied",
                required_permission=permission,
                resource_id=resource.id,
            )

    async def audit_success(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> object:
        del principal
        self.audited.append((action, resource_type, resource_id, metadata or {}))
        return None


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
        self.require_calls: list[dict[str, object]] = []
        self.advance_lease_before_require = False

    async def grant(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        owner_principal: ReservationOwner | None = None,
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
                owner_principal_id=(
                    owner_principal.principal_id if owner_principal is not None else None
                ),
                owner_principal_type=(
                    owner_principal.principal_type.value if owner_principal is not None else None
                ),
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

    async def get(
        self,
        reservation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> CoordinatedReservationLease:
        for record in self.records.values():
            if record.reservation.id == reservation_id:
                if (
                    organisation_id is not None
                    and record.reservation.organisation_id != organisation_id
                ):
                    break
                return record
        raise ReservationNotFoundError(
            f"Reservation {reservation_id} was not found.",
            reservation_id=str(reservation_id),
        )

    async def require_for_new_work(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        lease_version: int,
        agent_observed_at: datetime | None = None,
    ) -> ReservationLease:
        del agent_observed_at
        current = await self.get(reservation_id)
        self.require_calls.append(
            {
                "reservation_id": reservation_id,
                "agent_id": agent_id,
                "bench_id": bench_id,
                "owner": owner,
                "lease_version": lease_version,
            }
        )
        if self.advance_lease_before_require:
            self.advance_lease_before_require = False
            advanced = CoordinatedReservationLease(
                reservation=current.reservation,
                lease=current.lease.model_copy(
                    update={"lease_version": current.lease.lease_version + 1}
                ),
                state=current.state,
                revision=current.revision + 1,
            )
            for key, record in tuple(self.records.items()):
                if record.reservation.id == reservation_id:
                    self.records[key] = advanced
                    break
            current = advanced
        if current.reservation.owner != owner:
            raise ReservationOwnerMismatchError(
                "Reservation owner does not match.",
                reservation_id=str(reservation_id),
            )
        if current.lease.lease_version != lease_version:
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease version is stale.",
                reservation_id=str(reservation_id),
                expected_lease_version=lease_version,
                current_lease_version=current.lease.lease_version,
            )
        if current.lease.agent_id != agent_id or current.lease.bench_id != bench_id:
            raise ReservationLeaseInvalidError(
                "Reservation route does not match.",
                reservation_id=str(reservation_id),
            )
        if current.state is not ReservationLeaseState.ACTIVE:
            raise ReservationNotActiveError(
                "Reservation is not active.",
                reservation_id=str(reservation_id),
            )
        if not current.lease.is_valid_at(NOW):
            raise ReservationLeaseInvalidError(
                "Reservation lease is not current.",
                reservation_id=str(reservation_id),
            )
        return current.lease

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


class StrictGlobalReplayFakeReservations(FakeReservations):
    def __init__(self) -> None:
        super().__init__()
        self.replay_agents: dict[str, UUID] = {}

    async def grant(self, **kwargs: Any) -> CoordinatedReservationLease:
        key = str(kwargs["idempotency_key"])
        agent_id = kwargs["agent_id"]
        assert isinstance(agent_id, UUID)
        existing_agent = self.replay_agents.get(key)
        if existing_agent is not None and existing_agent != agent_id:
            raise AssertionError("reservation replay key crossed tenant routes")
        self.replay_agents[key] = agent_id
        return await super().grant(**kwargs)


class FakeArtifacts:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.descriptors: dict[str, WorkflowArtifactTransferDescriptor] = {}
        self.override_agent_id: UUID | None = None

    async def require_access(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
    ) -> None:
        del artifact_id, organisation_id, authentication_context, allow_legacy_authorisation

    async def issue_download(
        self,
        *,
        agent_id: UUID,
        input_name: str,
        artifact_id: UUID,
        target_path: str,
        idempotency_key: str,
        organisation_id: UUID | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
    ) -> WorkflowArtifactTransferDescriptor:
        del organisation_id, authentication_context, allow_legacy_authorisation
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
        actor_context: ActorContext | None = None,
        authorisation_snapshot_id: UUID | None = None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        del actor_context, authorisation_snapshot_id
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
    authorisation: WorkflowAuthorisationService | None = None,
    definition_catalog: FakeWorkflowDefinitionCatalog | None = None,
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
        authorisation=authorisation,
        definition_catalog=(
            definition_catalog or FakeWorkflowDefinitionCatalog((_workflow(), _artifact_workflow()))
        ),
        clock=lambda: NOW,
    )
    return coordinator, lease_service, artifact_service, command_service


async def _seed_existing_reservation(
    reservations: FakeReservations,
    agent: AgentRecord,
    bench: GlobalBenchRecord,
    *,
    owner: str = "api/alice",
    owner_principal: ReservationOwner | None = None,
    idempotency_key: str = "existing-reservation",
) -> CoordinatedReservationLease:
    record = await reservations.grant(
        agent_id=agent.id,
        bench_id=bench.id,
        owner=owner,
        owner_principal=owner_principal,
        idempotency_key=idempotency_key,
        metadata={"reservation_lifecycle": "caller"},
    )
    reservations.calls.clear()
    return record


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


@pytest.mark.parametrize(("release_after", "released"), [(False, False), (True, True)])
@_run_async_test
async def test_existing_reservation_is_reused_without_a_second_grant_and_honours_release_intent(
    release_after: bool,
    released: bool,
) -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(reservations, agent, bench)
    coordinator, _, _, commands = _coordinator(
        [agent],
        [bench],
        reservations=reservations,
    )

    dispatch = await coordinator.run(
        DistributedWorkflowRequest(
            definition=_workflow(),
            owner=existing.reservation.owner,
            idempotency_key=f"reuse-{release_after}",
            reservation_id=existing.reservation.id,
            release_reservation_after=release_after,
        )
    )

    assert reservations.calls == []
    assert reservations.require_calls == [
        {
            "reservation_id": existing.reservation.id,
            "agent_id": agent.id,
            "bench_id": bench.id,
            "owner": existing.reservation.owner,
            "lease_version": existing.lease.lease_version,
        }
    ]
    assert dispatch.reservation == existing
    assert commands.calls[0]["lease"] == existing.lease
    assert commands.dispatch_calls[0]["reservation_lease"] == existing.lease
    assert commands.calls[0]["payload"]["reservation_lifecycle"] == {
        "management": "caller",
        "release_after": release_after,
    }
    with pytest.raises(WorkflowInvalidError, match="idempotency key"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=existing.reservation.owner,
                idempotency_key=f"reuse-{release_after}",
                reservation_id=existing.reservation.id,
                release_reservation_after=not release_after,
            )
        )

    command_key, (command, operation) = next(iter(commands.created.items()))
    commands.created[command_key] = (
        command.model_copy(
            update={
                "status": RemoteCommandStatus.SUCCEEDED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        ),
        operation,
    )
    lifecycle = DistributedWorkflowReservationLifecycle(commands, reservations)
    assert await lifecycle.release_terminal() == int(released)
    assert len(reservations.release_calls) == int(released)


@pytest.mark.parametrize("release_after", [False, True])
@_run_async_test
async def test_existing_reservation_pre_dispatch_failure_never_releases_callers_lease(
    release_after: bool,
) -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(
        reservations,
        agent,
        bench,
        idempotency_key=f"existing-failure-{release_after}",
    )
    commands = FakeCommands()
    commands.create_error = RuntimeError("create failed")
    coordinator, _, _, _ = _coordinator(
        [agent],
        [bench],
        reservations=reservations,
        commands=commands,
    )

    with pytest.raises(RuntimeError, match="create failed"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=existing.reservation.owner,
                idempotency_key=f"reuse-failure-{release_after}",
                reservation_id=existing.reservation.id,
                release_reservation_after=release_after,
            )
        )

    assert reservations.calls == []
    assert reservations.release_calls == []
    current = await reservations.get(existing.reservation.id)
    assert current.state is ReservationLeaseState.ACTIVE


@_run_async_test
async def test_existing_reservation_requires_current_active_compatible_owned_lease() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")

    stale_reservations = FakeReservations()
    stale = await _seed_existing_reservation(stale_reservations, agent, bench)
    stale_reservations.advance_lease_before_require = True
    stale_coordinator, _, _, stale_commands = _coordinator(
        [agent], [bench], reservations=stale_reservations
    )
    with pytest.raises(ReservationLeaseVersionMismatchError):
        await stale_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=stale.reservation.owner,
                idempotency_key="reuse-stale",
                reservation_id=stale.reservation.id,
                release_reservation_after=False,
            )
        )
    assert stale_reservations.calls == []
    assert stale_commands.calls == []

    inactive_reservations = FakeReservations()
    inactive_reservations.return_state = ReservationLeaseState.UNKNOWN
    inactive = await _seed_existing_reservation(
        inactive_reservations,
        agent,
        bench,
        idempotency_key="existing-inactive",
    )
    inactive_coordinator, _, _, inactive_commands = _coordinator(
        [agent], [bench], reservations=inactive_reservations
    )
    with pytest.raises(ReservationNotActiveError):
        await inactive_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=inactive.reservation.owner,
                idempotency_key="reuse-inactive",
                reservation_id=inactive.reservation.id,
                release_reservation_after=False,
            )
        )
    assert inactive_reservations.calls == []
    assert inactive_commands.calls == []

    incompatible_reservations = FakeReservations()
    incompatible = await _seed_existing_reservation(
        incompatible_reservations,
        agent,
        bench,
        idempotency_key="existing-incompatible",
    )
    incompatible_coordinator, _, _, incompatible_commands = _coordinator(
        [agent], [bench], reservations=incompatible_reservations
    )
    with pytest.raises(NoCompatibleBenchError):
        await incompatible_coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=incompatible.reservation.owner,
                idempotency_key="reuse-incompatible",
                reservation_id=incompatible.reservation.id,
                release_reservation_after=False,
                kind=GlobalBenchKind.SIMULATED,
            )
        )
    assert incompatible_reservations.calls == []
    assert incompatible_reservations.require_calls == []
    assert incompatible_commands.calls == []


@_run_async_test
async def test_existing_reservation_cannot_cross_requested_bench_or_organisation() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(reservations, agent, bench)
    coordinator, _, _, commands = _coordinator([agent], [bench], reservations=reservations)

    with pytest.raises(ReservationLeaseInvalidError, match="requested bench"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=existing.reservation.owner,
                idempotency_key="reuse-other-bench",
                bench_id="agent-a/other-bench",
                reservation_id=existing.reservation.id,
                release_reservation_after=False,
            )
        )

    record_key = next(iter(reservations.records))
    reservations.records[record_key] = CoordinatedReservationLease(
        reservation=existing.reservation.model_copy(update={"organisation_id": UUID(int=999)}),
        lease=existing.lease,
        state=existing.state,
        revision=existing.revision,
    )
    with pytest.raises(ReservationNotFoundError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=existing.reservation.owner,
                idempotency_key="reuse-other-organisation",
                reservation_id=existing.reservation.id,
                release_reservation_after=False,
            )
        )

    assert reservations.calls == []
    assert reservations.require_calls == []
    assert commands.calls == []


@_run_async_test
async def test_existing_reservation_requires_the_same_durable_owner_principal() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    owner = ReservationOwner(
        principal_id=UUID(int=101),
        principal_type=PrincipalType.USER,
        display_name="Alice",
    )
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(
        reservations,
        agent,
        bench,
        owner=owner.display_name,
        owner_principal=owner,
    )
    coordinator, _, _, commands = _coordinator([agent], [bench], reservations=reservations)

    with pytest.raises(ReservationOwnerMismatchError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=owner.display_name,
                owner_principal=owner.model_copy(update={"principal_id": UUID(int=102)}),
                idempotency_key="reuse-wrong-principal",
                reservation_id=existing.reservation.id,
                release_reservation_after=False,
            )
        )

    assert reservations.calls == []
    assert reservations.require_calls == []
    assert commands.calls == []


@_run_async_test
async def test_existing_reservation_release_after_requires_reservation_permission() -> None:
    definition = _workflow()
    context, actor = _authenticated_workflow_actor(definition)
    owner = ReservationOwner(
        principal_id=context.principal.id,
        principal_type=context.principal.type,
        display_name=context.principal.display_name,
    )
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(
        reservations,
        agent,
        bench,
        owner=owner.display_name,
        owner_principal=owner,
    )
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    coordinator, _, _, commands = _coordinator(
        [agent],
        [bench],
        reservations=reservations,
        authorisation=authorisation,
        definition_catalog=FakeWorkflowDefinitionCatalog((definition,)),
    )

    with pytest.raises(PermissionDeniedError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner=owner.display_name,
                owner_principal=owner,
                actor_context=actor,
                authentication_context=context,
                organisation_id=definition.organisation_id,
                idempotency_key="reuse-release-permission",
                reservation_id=existing.reservation.id,
                release_reservation_after=True,
            )
        )

    assert ("benches:reserve", bench.id) in [
        (permission, resource.id) for permission, resource in authorisation.required
    ]
    assert reservations.calls == []
    assert reservations.require_calls == []
    assert commands.calls == []


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
    assert dispatched_definition == result.definition.model_dump(mode="json")
    assert dispatched_definition["steps"][0]["firmware"] == f"artifacts/{artifact_id}"
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
        ({"reservation_id": "not-a-uuid"}, "reservation_id"),
        ({"release_reservation_after": False}, "requires an existing reservation_id"),
        (
            {"reservation_id": UUID(int=700)},
            "release_reservation_after must be explicit",
        ),
        (
            {
                "reservation_id": UUID(int=701),
                "release_reservation_after": 1,
            },
            "release_reservation_after must be a boolean",
        ),
        (
            {
                "reservation_id": UUID(int=702),
                "release_reservation_after": False,
                "lease_ttl_seconds": 60,
            },
            "cannot be granted or renewed",
        ),
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

    command_key, (command, operation) = next(iter(dispatch_failure.created.items()))
    dispatch_failure.created[command_key] = (
        command.model_copy(
            update={
                "status": RemoteCommandStatus.EXPIRED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        ),
        operation,
    )
    lifecycle = DistributedWorkflowReservationLifecycle(
        dispatch_failure,
        dispatch_reservations,
    )
    assert await lifecycle.release_terminal() == 1


@_run_async_test
async def test_reused_dispatch_failure_releases_only_after_durable_terminal_state() -> None:
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    reservations = FakeReservations()
    existing = await _seed_existing_reservation(reservations, agent, bench)
    commands = FakeCommands()
    commands.dispatch_error = RuntimeError("delivery uncertain")
    coordinator, _, _, _ = _coordinator(
        [agent],
        [bench],
        reservations=reservations,
        commands=commands,
    )

    with pytest.raises(RuntimeError, match="delivery uncertain"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=_workflow(),
                owner=existing.reservation.owner,
                idempotency_key="reused-dispatch-failure",
                reservation_id=existing.reservation.id,
                release_reservation_after=True,
            )
        )

    assert reservations.release_calls == []
    command_key, (command, operation) = next(iter(commands.created.items()))
    assert command.payload["reservation_lifecycle"] == {
        "management": "caller",
        "release_after": True,
    }
    commands.created[command_key] = (
        command.model_copy(
            update={
                "status": RemoteCommandStatus.FAILED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        ),
        operation,
    )
    lifecycle = DistributedWorkflowReservationLifecycle(commands, reservations)
    assert await lifecycle.release_terminal() == 1
    assert len(reservations.release_calls) == 1


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


def _authenticated_workflow_actor(
    definition: WorkflowDefinition,
) -> tuple[AuthenticationContext, ActorContext]:
    principal = Principal(
        id=UUID(int=91),
        type=PrincipalType.USER,
        organisation_id=definition.organisation_id,
        display_name="Phase 6 operator",
    )
    snapshot_id = UUID(int=92)
    return (
        AuthenticationContext(
            principal=principal,
            session_id=UUID(int=93),
            permission_restrictions={"workflows:run", "benches:operate"},
            authorisation_snapshot_id=snapshot_id,
        ),
        ActorContext(
            principal_id=principal.id,
            principal_type=principal.type,
            display_name=principal.display_name,
            organisation_id=principal.organisation_id,
            authorisation_snapshot_id=snapshot_id,
        ),
    )


@_run_async_test
async def test_service_level_workflow_authorisation_precedes_all_side_effects() -> None:
    definition = _artifact_workflow()
    context, actor = _authenticated_workflow_actor(definition)
    agent = _agent(1, "agent-a")
    authorisation = FakeWorkflowAuthorisation(workflow_allowed=False)
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [_bench(agent, "bench-a")],
    )
    coordinator.set_authorisation_service(authorisation)

    with pytest.raises(PermissionDeniedError, match="permission denied"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner="phase6/operator",
                idempotency_key="workflow-authz-denied",
                authentication_context=context,
                actor_context=actor,
                organisation_id=definition.organisation_id,
                inputs={
                    "firmware": {"artifact_id": str(UUID(int=94))},
                    "version": "1.0",
                },
            )
        )

    assert [(permission, resource.id) for permission, resource in authorisation.required] == [
        ("workflows:run", definition.name)
    ]
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []


@_run_async_test
async def test_authenticated_workflow_rejects_forged_catalog_definition_before_side_effects() -> (
    None
):
    trusted = _workflow()
    forged = WorkflowDefinition.model_validate(
        {
            **trusted.model_dump(mode="python"),
            "requirements": {"capabilities": [], "labels": {}},
            "steps": [{"action": "wait", "seconds": 1}],
        }
    )
    context, actor = _authenticated_workflow_actor(trusted)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    catalog = FakeWorkflowDefinitionCatalog((trusted,))
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
        definition_catalog=catalog,
    )

    with pytest.raises(WorkflowInvalidError, match="trusted catalog"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=forged,
                owner="phase6/operator",
                idempotency_key="forged-definition",
                authentication_context=context,
                actor_context=actor,
                organisation_id=trusted.organisation_id,
            )
        )

    assert authorisation.required == []
    assert authorisation.evaluated == []
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []
    assert catalog.calls == [(context.principal.organisation_id, trusted.name, trusted.version)]


@_run_async_test
async def test_forged_definition_cannot_replay_legitimate_idempotent_dispatch() -> None:
    trusted = _workflow()
    forged = WorkflowDefinition.model_validate(
        {
            **trusted.model_dump(mode="python"),
            "requirements": {"capabilities": [], "labels": {}},
            "steps": [{"action": "wait", "seconds": 1}],
        }
    )
    context, actor = _authenticated_workflow_actor(trusted)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    catalog = FakeWorkflowDefinitionCatalog((trusted,))
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
        definition_catalog=catalog,
    )
    request = DistributedWorkflowRequest(
        definition=trusted,
        owner="phase6/operator",
        idempotency_key="trusted-then-forged",
        authentication_context=context,
        actor_context=actor,
        organisation_id=trusted.organisation_id,
    )
    dispatched = await coordinator.run(request)
    baseline = (
        len(reservations.calls),
        len(artifacts.calls),
        len(commands.calls),
        len(authorisation.required),
    )

    with pytest.raises(WorkflowInvalidError, match="trusted catalog"):
        await coordinator.run(replace(request, definition=forged))

    assert (
        len(reservations.calls),
        len(artifacts.calls),
        len(commands.calls),
        len(authorisation.required),
    ) == baseline
    payload = commands.calls[0]["payload"]["definition"]
    assert payload == dispatched.definition.model_dump(mode="json")
    assert payload["steps"][0]["action"] == "reset"
    assert catalog.calls == [
        (context.principal.organisation_id, trusted.name, trusted.version),
        (context.principal.organisation_id, trusted.name, trusted.version),
    ]


@_run_async_test
async def test_authenticated_workflow_fails_closed_without_catalog_or_valid_model() -> None:
    trusted = _workflow()
    context, actor = _authenticated_workflow_actor(trusted)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    reservations = FakeReservations()
    artifacts = FakeArtifacts()
    commands = FakeCommands()
    coordinator = DistributedWorkflowCoordinator(
        InventoryService(InMemoryInventoryRepository((bench,))),
        FakePresence([agent]),
        reservations,
        artifacts,
        commands,
        authorisation=authorisation,
        clock=lambda: NOW,
    )
    request = DistributedWorkflowRequest(
        definition=trusted,
        owner="phase6/operator",
        idempotency_key="missing-catalog",
        authentication_context=context,
        actor_context=actor,
        organisation_id=trusted.organisation_id,
    )
    with pytest.raises(WorkflowInvalidError, match="trusted definition catalog"):
        await coordinator.run(request)

    coordinator.set_definition_catalog(FakeWorkflowDefinitionCatalog((trusted,)))
    malformed = WorkflowDefinition.model_construct(name=trusted.name)
    with pytest.raises(WorkflowInvalidError, match="incomplete or invalid"):
        await coordinator.run(replace(request, definition=malformed))

    assert authorisation.required == []
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []


@_run_async_test
async def test_workflow_idempotency_cache_is_scoped_by_organisation() -> None:
    first_org = UUID(int=501)
    second_org = UUID(int=502)
    first_definition = _workflow().model_copy(update={"organisation_id": first_org})
    second_definition = _workflow().model_copy(update={"organisation_id": second_org})
    first_agent = _agent(501, "tenant-a").model_copy(update={"organisation_id": first_org})
    second_agent = _agent(502, "tenant-b").model_copy(update={"organisation_id": second_org})
    first_bench = _bench(first_agent, "bench")
    second_bench = _bench(second_agent, "bench")
    first_context, first_actor = _authenticated_workflow_actor(first_definition)
    second_context, second_actor = _authenticated_workflow_actor(second_definition)
    authorisation = FakeWorkflowAuthorisation(allowed_benches={first_bench.id, second_bench.id})
    reservations = StrictGlobalReplayFakeReservations()
    commands = FakeCommands()
    coordinator, _, _, _ = _coordinator(
        [first_agent, second_agent],
        [first_bench, second_bench],
        reservations=reservations,
        commands=commands,
        authorisation=authorisation,
        definition_catalog=FakeWorkflowDefinitionCatalog((first_definition, second_definition)),
    )
    shared_key = "same-tenant-scoped-key"
    first_request = DistributedWorkflowRequest(
        definition=first_definition,
        owner="tenant-a",
        idempotency_key=shared_key,
        authentication_context=first_context,
        actor_context=first_actor,
        organisation_id=first_org,
    )
    second_request = DistributedWorkflowRequest(
        definition=second_definition,
        owner="tenant-b",
        idempotency_key=shared_key,
        authentication_context=second_context,
        actor_context=second_actor,
        organisation_id=second_org,
    )

    first_dispatch = await coordinator.run(first_request)
    second_dispatch = await coordinator.run(second_request)

    assert first_dispatch.bench.id == first_bench.id
    assert second_dispatch.bench.id == second_bench.id
    assert len(reservations.calls) == 2
    assert len(reservations.replay_agents) == 2
    assert len(commands.calls) == 2
    with pytest.raises(WorkflowInvalidError, match="idempotency key"):
        await coordinator.run(replace(first_request, owner="changed-owner"))
    assert len(reservations.calls) == 2
    assert len(commands.calls) == 2


@_run_async_test
async def test_workflow_replay_ignores_fresh_snapshot_id_but_binds_authentication() -> None:
    definition = _workflow()
    context, actor = _authenticated_workflow_actor(definition)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
    )
    request = DistributedWorkflowRequest(
        definition=definition,
        owner="phase6/operator",
        idempotency_key="fresh-snapshot-replay",
        authentication_context=context,
        actor_context=actor,
        organisation_id=definition.organisation_id,
    )
    first = await coordinator.run(request)
    new_snapshot_id = UUID(int=9_999)
    replayed = await coordinator.run(
        replace(
            request,
            authentication_context=context.model_copy(
                update={"authorisation_snapshot_id": new_snapshot_id}
            ),
            actor_context=actor.model_copy(update={"authorisation_snapshot_id": new_snapshot_id}),
        )
    )
    assert replayed == first
    assert len(reservations.calls) == 1
    assert artifacts.calls == []
    assert len(commands.calls) == 1

    # Authentication handles may rotate for one principal without turning a retry into
    # new work. Effective credential restrictions remain immutable request content.
    rotated = await coordinator.run(
        replace(
            request,
            authentication_context=context.model_copy(
                update={"session_id": UUID(int=6), "credential_id": UUID(int=7)}
            ),
        )
    )
    assert rotated == first
    with pytest.raises(WorkflowInvalidError, match="idempotency key"):
        await coordinator.run(
            replace(
                request,
                authentication_context=context.model_copy(
                    update={"permission_restrictions": {"workflows:run"}}
                ),
            )
        )
    assert len(reservations.calls) == 1
    assert len(commands.calls) == 1


@_run_async_test
async def test_unauthorised_candidates_are_silently_skipped_before_reservation() -> None:
    definition = _workflow()
    context, actor = _authenticated_workflow_actor(definition)
    first_agent = _agent(1, "agent-a")
    second_agent = _agent(2, "agent-b")
    first_bench = _bench(first_agent, "bench-a")
    second_bench = _bench(second_agent, "bench-b")
    authorisation = FakeWorkflowAuthorisation(
        allowed_benches={second_bench.id},
    )
    coordinator, reservations, artifacts, commands = _coordinator(
        [first_agent, second_agent],
        [first_bench, second_bench],
        authorisation=authorisation,
    )

    dispatch = await coordinator.run(
        DistributedWorkflowRequest(
            definition=definition,
            owner="phase6/operator",
            idempotency_key="bench-authz-filter",
            authentication_context=context,
            actor_context=actor,
            organisation_id=definition.organisation_id,
        )
    )

    assert dispatch.bench.id == second_bench.id
    assert [call[1].id for call in authorisation.evaluated] == [
        first_bench.id,
        second_bench.id,
    ]
    assert [(permission, resource.id) for permission, resource in authorisation.required] == [
        ("workflows:run", definition.name)
    ]
    assert [call["bench_id"] for call in reservations.calls] == [second_bench.id]
    assert artifacts.calls == []
    assert len(commands.calls) == 1
    assert authorisation.audited == [
        (
            "WORKFLOW_STARTED",
            ResourceType.WORKFLOW.value,
            definition.name,
            {
                "bench_id": second_bench.id,
                "command_id": str(dispatch.command.id),
                "operation_id": str(dispatch.operation.id),
            },
        )
    ]


@_run_async_test
async def test_explicit_unauthorised_bench_uses_audited_require_without_side_effects() -> None:
    definition = _workflow()
    context, actor = _authenticated_workflow_actor(definition)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation()
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
    )

    with pytest.raises(PermissionDeniedError, match="permission denied"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner="phase6/operator",
                idempotency_key="explicit-bench-authz-denied",
                authentication_context=context,
                actor_context=actor,
                organisation_id=definition.organisation_id,
                bench_id=bench.id,
            )
        )

    assert [(permission, resource.id) for permission, resource in authorisation.required] == [
        ("workflows:run", definition.name),
        ("benches:operate", bench.id),
    ]
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []


@_run_async_test
async def test_runtime_authorisation_requires_identity_or_explicit_legacy_escape() -> None:
    definition = _workflow()
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    coordinator, reservations, _, _ = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
    )

    with pytest.raises(AuthenticationRequiredError):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner="legacy",
                idempotency_key="implicit-legacy-rejected",
            )
        )
    escaped = await coordinator.run(
        DistributedWorkflowRequest(
            definition=definition,
            owner="legacy",
            idempotency_key="explicit-legacy-accepted",
            allow_legacy_authorisation=True,
        )
    )

    assert escaped.bench.id == bench.id
    assert len(reservations.calls) == 1
    assert authorisation.audited == []


@_run_async_test
async def test_authenticated_workflow_context_is_bound_to_actor_and_tenant() -> None:
    definition = _workflow()
    context, actor = _authenticated_workflow_actor(definition)
    agent = _agent(1, "agent-a")
    bench = _bench(agent, "bench-a")
    authorisation = FakeWorkflowAuthorisation(allowed_benches={bench.id})
    coordinator, reservations, artifacts, commands = _coordinator(
        [agent],
        [bench],
        authorisation=authorisation,
    )

    with pytest.raises(WorkflowInvalidError, match="actor context"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=definition,
                owner="phase6/operator",
                idempotency_key="mismatched-actor",
                authentication_context=context,
                actor_context=actor.model_copy(update={"principal_id": UUID(int=999)}),
                organisation_id=definition.organisation_id,
            )
        )
    foreign_definition = definition.model_copy(update={"organisation_id": UUID(int=888)})
    with pytest.raises(WorkflowInvalidError, match="trusted catalog"):
        await coordinator.run(
            DistributedWorkflowRequest(
                definition=foreign_definition,
                owner="phase6/operator",
                idempotency_key="mismatched-tenant",
                authentication_context=context,
                actor_context=actor,
                organisation_id=context.principal.organisation_id,
            )
        )

    assert authorisation.required == []
    assert reservations.calls == []
    assert artifacts.calls == []
    assert commands.calls == []
