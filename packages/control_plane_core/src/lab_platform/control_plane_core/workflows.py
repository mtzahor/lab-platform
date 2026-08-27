from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from lab_platform.control_plane_core.commands import RemoteCommandService
from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    BenchAlreadyReservedError,
    NoCompatibleBenchError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
)
from lab_platform.core.workflows import (
    WorkflowInvalidError,
    WorkflowNotFoundError,
    resolve_workflow_inputs,
)
from lab_platform.models import (
    LEGACY_ORGANISATION_ID,
    ActorContext,
    AgentRecord,
    AgentStatus,
    ArtifactReference,
    ArtifactWorkflowInput,
    AuthenticationContext,
    AuthorisationResource,
    BooleanWorkflowInput,
    DistributedOperation,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    IntegerWorkflowInput,
    Principal,
    RemoteCommand,
    RemoteCommandType,
    ReservationLease,
    ReservationOwner,
    ReservationSource,
    ResourceType,
    StringWorkflowInput,
    WorkflowDefinition,
)
from pydantic import ValidationError

_INTEGER_INPUT = re.compile(r"^[+-]?\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_GRANT_ERRORS = (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    BenchAlreadyReservedError,
)
_RESERVATION_LIFECYCLE_METADATA_KEY = "reservation_lifecycle"
_WORKFLOW_MANAGED_RESERVATION = "workflow"
_CALLER_MANAGED_RESERVATION = "caller"
_RESERVATION_LIFECYCLE_RELEASE_AFTER_KEY = "release_after"
_RESERVATION_LIFECYCLE_MANAGEMENT_KEY = "management"
_EXPECTED_RELEASE_RACES = (
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
)


@dataclass(frozen=True, slots=True)
class DistributedWorkflowRequest:
    """Agent-agnostic request for one centrally validated workflow run."""

    definition: WorkflowDefinition
    owner: str
    idempotency_key: str
    owner_principal: ReservationOwner | None = None
    actor_context: ActorContext | None = None
    authentication_context: AuthenticationContext | None = None
    organisation_id: UUID | None = None
    allow_legacy_authorisation: bool = False
    inputs: Mapping[str, object] = field(default_factory=dict)
    bench_id: str | None = None
    kind: GlobalBenchKind | None = None
    location: str | None = None
    preferred_location: str | None = None
    bench_labels: Mapping[str, str] = field(default_factory=dict)
    preferred_bench_labels: Mapping[str, str] = field(default_factory=dict)
    agent_labels: Mapping[str, str] = field(default_factory=dict)
    required_capabilities: Collection[str] = field(default_factory=tuple)
    reservation_duration_seconds: int | None = None
    lease_ttl_seconds: int | None = None
    command_timeout_seconds: int = 3_600
    manage_reservation_lifecycle: bool = True
    reservation_id: UUID | None = None
    release_reservation_after: bool | None = None


@dataclass(frozen=True, slots=True)
class WorkflowArtifactTransferDescriptor:
    """Durable metadata for an immutable workflow input staged by the control plane.

    Transfer identifiers, URLs, bearer capabilities, and their expirations are intentionally
    absent. Those values are delivery-attempt state and are hydrated only in the outbound
    command request.
    """

    input_name: str
    agent_id: UUID
    artifact_id: UUID
    sha256: str
    size_bytes: int
    target_path: str

    def __post_init__(self) -> None:
        if not self.input_name:
            raise ValueError("Artifact transfer input_name must not be empty")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("Artifact transfer sha256 must be lowercase hexadecimal")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("Artifact transfer size_bytes must be non-negative")
        _require_safe_relative_path(self.target_path)

    def as_payload(self) -> dict[str, object]:
        return {
            "input_name": self.input_name,
            "agent_id": str(self.agent_id),
            "artifact_id": str(self.artifact_id),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "target_path": self.target_path,
        }


@dataclass(frozen=True, slots=True)
class DistributedWorkflowDispatch:
    """The selected route and durable distributed work accepted by the control plane."""

    agent: AgentRecord
    bench: GlobalBenchRecord
    definition: WorkflowDefinition
    inputs: Mapping[str, object]
    reservation: CoordinatedReservationLease
    artifact_transfers: tuple[WorkflowArtifactTransferDescriptor, ...]
    command: RemoteCommand
    operation: DistributedOperation


class WorkflowInventory(Protocol):
    async def list_benches(self) -> list[GlobalBenchRecord]: ...


class WorkflowPresence(Protocol):
    async def list_agents(self) -> list[AgentRecord]: ...


class WorkflowDefinitionCatalog(Protocol):
    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None: ...


class WorkflowAuthorisationService(Protocol):
    """The narrow central-authorisation surface required during routing."""

    async def is_allowed(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool: ...

    async def require(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None: ...

    async def audit_success(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> object: ...


class WorkflowReservationService(Protocol):
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
    ) -> CoordinatedReservationLease: ...

    async def get(
        self,
        reservation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> CoordinatedReservationLease: ...

    async def require_for_new_work(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        lease_version: int,
        agent_observed_at: datetime | None = None,
    ) -> ReservationLease: ...

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
    ) -> CoordinatedReservationLease: ...


class WorkflowArtifactTransferPort(Protocol):
    async def require_access(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
    ) -> None: ...

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
    ) -> WorkflowArtifactTransferDescriptor: ...


class WorkflowCommandService(Protocol):
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
    ) -> tuple[RemoteCommand, DistributedOperation | None]: ...

    async def dispatch(
        self,
        command_id: UUID,
        *,
        reservation_lease: ReservationLease | None = None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]: ...


class WorkflowRemoteWorkRepository(Protocol):
    """Durable remote-work lookup used to recover reservation cleanup after restart."""

    async def list_terminal_workflow_commands(
        self,
        *,
        limit: int,
    ) -> list[RemoteCommand]: ...


class DistributedWorkflowReservationLifecycle:
    """Release workflow-owned reservations once durable remote work is terminal.

    Both terminal commands and terminal operations are inspected.  The overlap is
    intentional: those records are updated by separate compare-and-set writes, so
    either one can be the durable terminal witness after an abrupt process exit.
    Caller-managed reservations are retained unless the durable workflow command
    records the caller's explicit release-after intent.
    """

    def __init__(
        self,
        remote_work: WorkflowRemoteWorkRepository,
        reservations: WorkflowReservationService,
    ) -> None:
        self._remote_work = remote_work
        self._reservations = reservations

    async def release_terminal(self, *, limit: int = 1_000) -> int:
        _require_positive_seconds(limit, field="limit")
        commands = await self._remote_work.list_terminal_workflow_commands(
            limit=limit,
        )

        released = 0
        for command in commands:
            if await self._release_for_terminal_command(command):
                released += 1
        return released

    async def _release_for_terminal_command(self, command: RemoteCommand) -> bool:
        if (
            command.command_type is not RemoteCommandType.RUN_WORKFLOW
            or command.reservation_id is None
            or command.lease_version is None
        ):
            return False
        try:
            reservation = await self._reservations.get(command.reservation_id)
        except ReservationNotFoundError:
            return False
        release_after = _command_release_reservation_after(command)
        if (
            (
                release_after is False
                or (
                    release_after is None
                    and reservation.reservation.metadata.get(_RESERVATION_LIFECYCLE_METADATA_KEY)
                    != _WORKFLOW_MANAGED_RESERVATION
                )
            )
            or reservation.state
            in {
                ReservationLeaseState.RELEASED,
                ReservationLeaseState.EXPIRED,
                ReservationLeaseState.REVOKED,
            }
            or reservation.lease.lease_version != command.lease_version
            or reservation.lease.agent_id != command.agent_id
            or reservation.lease.bench_id != command.bench_id
        ):
            return False
        try:
            release_key = _derived_key(
                "workflow-terminal-release",
                str(reservation.reservation.id),
                str(command.lease_version),
            )
            if isinstance(self._reservations, CentralReservationLeaseService):
                released = await self._reservations.release(
                    reservation.reservation.id,
                    owner=reservation.reservation.owner,
                    expected_lease_version=command.lease_version,
                    idempotency_key=release_key,
                    allow_internal_authorisation=True,
                )
            else:
                released = await self._reservations.release(
                    reservation.reservation.id,
                    owner=reservation.reservation.owner,
                    expected_lease_version=command.lease_version,
                    idempotency_key=release_key,
                )
        except _EXPECTED_RELEASE_RACES:
            return False
        return released.state is ReservationLeaseState.RELEASED


@dataclass(slots=True)
class _IdempotentRequest:
    fingerprint: str
    future: asyncio.Future[DistributedWorkflowDispatch]


class DistributedWorkflowCoordinator:
    """Select, fence, stage and dispatch a complete workflow to one Agent.

    The public request intentionally has no Agent identifier. Agent ownership is
    derived from the selected global bench and is rechecked atomically by the
    central reservation service before any artifact capability or command is
    issued.
    """

    def __init__(
        self,
        inventory: WorkflowInventory,
        presence: WorkflowPresence,
        reservations: WorkflowReservationService,
        artifacts: WorkflowArtifactTransferPort,
        commands: WorkflowCommandService,
        *,
        authorisation: WorkflowAuthorisationService | None = None,
        definition_catalog: WorkflowDefinitionCatalog | None = None,
        clock: Callable[[], datetime] | None = None,
        maximum_idempotency_entries: int = 10_000,
    ) -> None:
        if maximum_idempotency_entries < 1:
            raise ValueError("maximum_idempotency_entries must be positive")
        self._inventory = inventory
        self._presence = presence
        self._reservations = reservations
        self._artifacts = artifacts
        self._commands = commands
        self._authorisation = authorisation
        self._definition_catalog = definition_catalog
        self._clock = clock or _utc_now
        self._maximum_idempotency_entries = maximum_idempotency_entries
        self._request_lock = asyncio.Lock()
        self._requests: dict[tuple[UUID, str], _IdempotentRequest] = {}

    def set_authorisation_service(
        self,
        authorisation: WorkflowAuthorisationService,
    ) -> None:
        """Attach runtime authorisation after the identity graph is constructed."""

        self._authorisation = authorisation

    def set_definition_catalog(self, catalog: WorkflowDefinitionCatalog) -> None:
        """Attach the trusted tenant-scoped workflow catalog used by identity calls."""

        self._definition_catalog = catalog

    async def run(self, request: DistributedWorkflowRequest) -> DistributedWorkflowDispatch:
        if not isinstance(request.allow_legacy_authorisation, bool):
            raise ValueError("allow_legacy_authorisation must be a boolean")
        request = await self._trusted_request(request)
        request = replace(
            request,
            required_capabilities=_normalise_required_capabilities(request.required_capabilities),
        )
        definition, normalized_inputs, artifact_inputs = _validate_workflow_request(request)
        owner = _require_text(request.owner, field="owner", maximum_length=200)
        idempotency_key = _require_text(
            request.idempotency_key,
            field="idempotency_key",
            maximum_length=500,
        )
        _require_positive_optional_seconds(
            request.reservation_duration_seconds,
            field="reservation_duration_seconds",
        )
        _require_positive_optional_seconds(
            request.lease_ttl_seconds,
            field="lease_ttl_seconds",
        )
        _require_positive_seconds(
            request.command_timeout_seconds,
            field="command_timeout_seconds",
        )
        if not isinstance(request.manage_reservation_lifecycle, bool):
            raise ValueError("manage_reservation_lifecycle must be a boolean")
        if request.reservation_id is not None and not isinstance(request.reservation_id, UUID):
            raise ValueError("reservation_id must be a UUID")
        if request.release_reservation_after is not None and not isinstance(
            request.release_reservation_after, bool
        ):
            raise ValueError("release_reservation_after must be a boolean")
        if request.reservation_id is None:
            if request.release_reservation_after is not None:
                raise ValueError("release_reservation_after requires an existing reservation_id")
        else:
            if request.release_reservation_after is None:
                raise ValueError(
                    "release_reservation_after must be explicit when reservation_id is supplied"
                )
            if (
                request.reservation_duration_seconds is not None
                or request.lease_ttl_seconds is not None
            ):
                raise ValueError(
                    "Existing workflow reservations cannot be granted or renewed by a run request"
                )
        await self._authorise_workflow(request, definition)
        for _input_name, reference in artifact_inputs:
            await self._artifacts.require_access(
                reference.artifact_id,
                organisation_id=request.organisation_id,
                authentication_context=request.authentication_context,
                allow_legacy_authorisation=request.allow_legacy_authorisation,
            )
        fingerprint = _workflow_fingerprint(
            request,
            definition=definition,
            inputs=normalized_inputs,
            owner=owner,
        )
        request_key = (_effective_request_organisation(request), idempotency_key)
        leader, future = await self._claim_request(request_key, fingerprint)
        if not leader:
            return await asyncio.shield(future)

        try:
            result = await self._run_once(
                request,
                definition=definition,
                inputs=normalized_inputs,
                artifact_inputs=artifact_inputs,
                owner=owner,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
        except BaseException as exc:
            await self._fail_request(request_key, future, exc)
            raise
        future.set_result(result)
        await self._trim_idempotency_cache()
        return result

    async def _trusted_request(
        self,
        request: DistributedWorkflowRequest,
    ) -> DistributedWorkflowRequest:
        context = request.authentication_context
        if context is None:
            return request
        if request.allow_legacy_authorisation:
            raise WorkflowInvalidError(
                "Legacy workflow authorisation cannot be combined with an authenticated principal."
            )
        catalog = self._definition_catalog
        try:
            workflow_name = request.definition.name
            workflow_version = request.definition.version
        except AttributeError as exc:
            raise WorkflowInvalidError("Workflow definition is incomplete or invalid.") from exc
        if (
            not isinstance(workflow_name, str)
            or not workflow_name
            or isinstance(workflow_version, bool)
            or not isinstance(workflow_version, int)
            or workflow_version < 1
        ):
            raise WorkflowInvalidError("Workflow definition is incomplete or invalid.")
        if catalog is None:
            raise WorkflowInvalidError(
                "Authenticated workflow dispatch requires a trusted definition catalog.",
                workflow_name=workflow_name,
                workflow_version=workflow_version,
            )
        principal = context.principal
        trusted = await catalog.get_definition(
            workflow_name,
            workflow_version,
            organisation_id=principal.organisation_id,
        )
        if trusted is None:
            raise WorkflowNotFoundError(
                "Workflow does not exist.",
                workflow_name=workflow_name,
                workflow_version=workflow_version,
            )
        try:
            matches_trusted = trusted == request.definition
        except (AttributeError, TypeError, ValueError):
            matches_trusted = False
        if not matches_trusted:
            raise WorkflowInvalidError(
                "Workflow definition does not match the trusted catalog entry.",
                workflow_name=trusted.name,
                workflow_version=trusted.version,
            )
        return replace(request, definition=trusted)

    async def _run_once(
        self,
        request: DistributedWorkflowRequest,
        *,
        definition: WorkflowDefinition,
        inputs: Mapping[str, object],
        artifact_inputs: tuple[tuple[str, ArtifactReference], ...],
        owner: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> DistributedWorkflowDispatch:
        reservation: CoordinatedReservationLease | None = None
        selected_agent: AgentRecord | None = None
        selected_bench: GlobalBenchRecord | None = None
        reused_reservation = request.reservation_id is not None
        raced_benches: list[str] = []
        organisation_key = str(_effective_request_organisation(request))
        if request.reservation_id is not None:
            reservation, selected_agent, selected_bench = await self._reuse_reservation(
                request,
                definition=definition,
                owner=owner,
            )
        else:
            candidates = await self._eligible_candidates(request, definition)
            if not candidates:
                raise _no_compatible_bench(request, definition)
            reservation_key = _derived_key(
                "workflow-reservation",
                organisation_key,
                idempotency_key,
            )
            for agent, bench in candidates:
                try:
                    grant_options: dict[str, Any] = {
                        "agent_id": agent.id,
                        "bench_id": bench.id,
                        "owner": owner,
                        "idempotency_key": reservation_key,
                        "reservation_duration_seconds": request.reservation_duration_seconds,
                        "lease_ttl_seconds": request.lease_ttl_seconds,
                        "source": ReservationSource.API,
                        "metadata": {
                            "workload": "workflow",
                            "workflow_name": definition.name,
                            "workflow_version": str(definition.version),
                            "workflow_fingerprint": fingerprint,
                            _RESERVATION_LIFECYCLE_METADATA_KEY: (
                                _WORKFLOW_MANAGED_RESERVATION
                                if request.manage_reservation_lifecycle
                                else _CALLER_MANAGED_RESERVATION
                            ),
                        },
                    }
                    if request.owner_principal is not None:
                        grant_options["owner_principal"] = request.owner_principal
                    if isinstance(self._reservations, CentralReservationLeaseService):
                        grant_options["authentication_context"] = request.authentication_context
                        grant_options["allow_internal_authorisation"] = True
                    candidate = await self._reservations.grant(**grant_options)
                except _RETRYABLE_GRANT_ERRORS:
                    raced_benches.append(bench.id)
                    continue
                except ReservationLeaseInvalidError as exc:
                    if not _is_atomic_eligibility_race(exc):
                        raise
                    raced_benches.append(bench.id)
                    continue
                if candidate.state is not ReservationLeaseState.ACTIVE:
                    await self._release_pre_dispatch(candidate)
                    raise ReservationLeaseInvalidError(
                        "Workflow reservation was not semantically confirmed by the Agent.",
                        reservation_id=str(candidate.reservation.id),
                        lease_state=candidate.state.value,
                    )
                reservation = candidate
                selected_agent = agent
                selected_bench = bench
                break

        if reservation is None or selected_agent is None or selected_bench is None:
            error = _no_compatible_bench(request, definition)
            error.details["raced_benches"] = raced_benches
            raise error

        transfers: list[WorkflowArtifactTransferDescriptor] = []
        try:
            now = _as_utc(self._clock(), field="workflow dispatch timestamp")
            for input_name, reference in artifact_inputs:
                target_path = _artifact_target_path(reference.artifact_id)
                descriptor = await self._artifacts.issue_download(
                    agent_id=selected_agent.id,
                    input_name=input_name,
                    artifact_id=reference.artifact_id,
                    target_path=target_path,
                    idempotency_key=_derived_key(
                        "workflow-artifact",
                        organisation_key,
                        idempotency_key,
                        input_name,
                        str(reference.artifact_id),
                    ),
                    organisation_id=request.organisation_id,
                    authentication_context=request.authentication_context,
                    allow_legacy_authorisation=request.allow_legacy_authorisation,
                )
                _validate_transfer_descriptor(
                    descriptor,
                    agent_id=selected_agent.id,
                    input_name=input_name,
                    artifact_id=reference.artifact_id,
                    target_path=target_path,
                )
                transfers.append(descriptor)

            expiry_candidates = [
                now + timedelta(seconds=request.command_timeout_seconds),
                reservation.lease.valid_until,
            ]
            expires_at = min(expiry_candidates)
            if expires_at <= now:
                raise ReservationLeaseInvalidError(
                    "Workflow reservation lease expired before command dispatch.",
                    reservation_id=str(reservation.reservation.id),
                    lease_version=reservation.lease.lease_version,
                )
            payload = {
                # The Agent must resolve artifact inputs to its verified local cache path.
                # Sending the centrally resolved virtual target (``artifacts/<id>``)
                # would bypass that resolver and leave the remote flash path unreadable.
                "definition": definition.model_dump(mode="json"),
                "inputs": _json_inputs(inputs),
                "owner": owner,
                "artifact_transfers": [transfer.as_payload() for transfer in transfers],
                "reservation_lease": reservation.lease.model_dump(mode="json"),
                _RESERVATION_LIFECYCLE_METADATA_KEY: _workflow_reservation_lifecycle_payload(
                    request,
                    reused_reservation=reused_reservation,
                ),
            }
            command_options: dict[str, Any] = {
                "agent_id": selected_agent.id,
                "bench_id": selected_bench.id,
                "command_type": RemoteCommandType.RUN_WORKFLOW,
                "payload": payload,
                "expires_at": expires_at,
                "idempotency_key": _derived_key(
                    "workflow-command",
                    organisation_key,
                    idempotency_key,
                ),
                "reservation_lease": reservation.lease,
                "operation_type": "RUN_WORKFLOW",
                "dispatch": False,
            }
            if request.actor_context is not None:
                command_options["actor_context"] = request.actor_context
                command_options["authorisation_snapshot_id"] = (
                    request.actor_context.authorisation_snapshot_id
                )
            if isinstance(self._commands, RemoteCommandService):
                command_options["authentication_context"] = request.authentication_context
                command_options["allow_legacy_authorisation"] = request.allow_legacy_authorisation
            command, operation = await self._commands.create(**command_options)
            if operation is None:
                raise RuntimeError("RUN_WORKFLOW command did not create a distributed operation")
        except BaseException:
            # A reused lease remains owned by the caller until a durable command
            # records this request's release-after intent.  Releasing it here can
            # fence unrelated work that is already using the same reservation.
            if not reused_reservation:
                await self._release_pre_dispatch(reservation)
            raise

        command, dispatched_operation = await self._commands.dispatch(
            command.id,
            reservation_lease=reservation.lease,
        )
        if dispatched_operation is not None:
            operation = dispatched_operation
        dispatch = DistributedWorkflowDispatch(
            agent=selected_agent,
            bench=selected_bench,
            definition=definition,
            inputs=dict(inputs),
            reservation=reservation,
            artifact_transfers=tuple(transfers),
            command=command,
            operation=operation,
        )
        context = (
            _authenticated_request_context(request, definition)
            if self._authorisation is not None
            else None
        )
        if self._authorisation is not None and context is not None:
            await self._authorisation.audit_success(
                context.principal,
                "WORKFLOW_STARTED",
                resource_type=ResourceType.WORKFLOW.value,
                resource_id=definition.name,
                metadata={
                    "bench_id": dispatch.bench.id,
                    "command_id": str(dispatch.command.id),
                    "operation_id": str(dispatch.operation.id),
                },
            )
        return dispatch

    async def _release_pre_dispatch(
        self,
        reservation: CoordinatedReservationLease,
    ) -> None:
        try:
            release_key = _derived_key(
                "workflow-pre-dispatch-release",
                str(reservation.reservation.id),
                str(reservation.lease.lease_version),
            )
            if isinstance(self._reservations, CentralReservationLeaseService):
                release_options: dict[str, Any] = {
                    "owner": reservation.reservation.owner,
                    "expected_lease_version": reservation.lease.lease_version,
                    "idempotency_key": release_key,
                }
                release_options["allow_internal_authorisation"] = True
                await self._reservations.release(
                    reservation.reservation.id,
                    **release_options,
                )
            else:
                await self._reservations.release(
                    reservation.reservation.id,
                    owner=reservation.reservation.owner,
                    expected_lease_version=reservation.lease.lease_version,
                    idempotency_key=release_key,
                )
        except _EXPECTED_RELEASE_RACES:
            # A concurrent terminal transition or lease renewal already fenced this
            # exact grant. Never broaden cleanup to a newer lease generation.
            return

    async def _reuse_reservation(
        self,
        request: DistributedWorkflowRequest,
        *,
        definition: WorkflowDefinition,
        owner: str,
    ) -> tuple[CoordinatedReservationLease, AgentRecord, GlobalBenchRecord]:
        reservation_id = request.reservation_id
        if reservation_id is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("Existing reservation selection requires a reservation ID")
        expected_organisation = _effective_request_organisation(request)
        reservation = await self._reservations.get(
            reservation_id,
            organisation_id=expected_organisation,
        )
        if request.bench_id is not None and request.bench_id != reservation.lease.bench_id:
            raise ReservationLeaseInvalidError(
                "Workflow reservation does not match the explicitly requested bench.",
                reservation_id=str(reservation_id),
                reservation_bench_id=reservation.lease.bench_id,
                requested_bench_id=request.bench_id,
            )

        # Pin routing to the caller's lease. This retains the usual workflow and
        # bench authorisation/compatibility checks without ever considering a
        # fallback bench that would need a second reservation.
        candidates = await self._eligible_candidates(
            replace(request, bench_id=reservation.lease.bench_id),
            definition,
        )
        selected = next(
            (
                (agent, bench)
                for agent, bench in candidates
                if agent.id == reservation.lease.agent_id and bench.id == reservation.lease.bench_id
            ),
            None,
        )
        if selected is None:
            error = _no_compatible_bench(request, definition)
            error.details["reservation_id"] = str(reservation_id)
            error.details["reservation_bench_id"] = reservation.lease.bench_id
            raise error

        if request.release_reservation_after is True:
            await self._authorise_reservation_release(request, selected[1])
        _require_reservation_principal_owner(reservation, request.owner_principal)
        current_lease = await self._reservations.require_for_new_work(
            reservation_id,
            agent_id=reservation.lease.agent_id,
            bench_id=reservation.lease.bench_id,
            owner=owner,
            lease_version=reservation.lease.lease_version,
        )
        if current_lease != reservation.lease:
            raise ReservationLeaseInvalidError(
                "Reservation lease changed while the workflow was being prepared.",
                reservation_id=str(reservation_id),
                lease_version=reservation.lease.lease_version,
            )
        return reservation, selected[0], selected[1]

    async def _eligible_candidates(
        self,
        request: DistributedWorkflowRequest,
        definition: WorkflowDefinition,
    ) -> list[tuple[AgentRecord, GlobalBenchRecord]]:
        if request.organisation_id is None:
            agents, benches = await asyncio.gather(
                self._presence.list_agents(),
                self._inventory.list_benches(),
            )
        else:
            if request.organisation_id != definition.organisation_id:
                raise WorkflowInvalidError(
                    "Workflow organisation does not match the authenticated request.",
                    workflow_name=definition.name,
                )
            agents, benches = await asyncio.gather(
                self._presence.list_agents(  # type: ignore[call-arg]
                    organisation_id=request.organisation_id
                ),
                self._inventory.list_benches(  # type: ignore[call-arg]
                    organisation_id=request.organisation_id
                ),
            )
        agents_by_id = {agent.id: agent for agent in agents}
        required_capabilities = {
            *(
                _normalise_capability(capability)
                for capability in definition.requirements.capabilities
            ),
            *request.required_capabilities,
        }
        required_bench_labels = dict(definition.requirements.labels)
        for key, value in request.bench_labels.items():
            if key in required_bench_labels and required_bench_labels[key] != value:
                return []
            required_bench_labels[key] = value
        location = request.location.strip().casefold() if request.location is not None else None
        preferred_location = (
            request.preferred_location.strip().casefold()
            if request.preferred_location is not None
            else None
        )
        candidates: list[tuple[AgentRecord, GlobalBenchRecord]] = []
        for bench in benches:
            agent = agents_by_id.get(bench.agent_id)
            if request.bench_id is not None and bench.id != request.bench_id:
                continue
            if not await self._authorise_bench(request, bench):
                continue
            if bench.status is not GlobalBenchStatus.ONLINE:
                continue
            if request.kind is not None and bench.kind is not request.kind:
                continue
            available = {_normalise_capability(capability) for capability in bench.capabilities}
            if not required_capabilities.issubset(available):
                continue
            if not _labels_match(bench.labels, required_bench_labels):
                continue
            if agent is None or agent.status is not AgentStatus.ONLINE:
                continue
            if location is not None and (
                agent.location is None or agent.location.casefold() != location
            ):
                continue
            if not _labels_match(agent.labels, request.agent_labels):
                continue
            candidates.append((agent, bench))
        return sorted(
            candidates,
            key=lambda route: (
                preferred_location is not None
                and (
                    route[0].location is None or route[0].location.casefold() != preferred_location
                ),
                -sum(
                    route[1].labels.get(key) == value
                    for key, value in request.preferred_bench_labels.items()
                ),
                route[1].id,
                route[0].slug,
                str(route[0].id),
            ),
        )

    async def _authorise_workflow(
        self,
        request: DistributedWorkflowRequest,
        definition: WorkflowDefinition,
    ) -> None:
        authorisation = self._authorisation
        if authorisation is None:
            return
        context = _authenticated_request_context(request, definition)
        if context is None:
            return
        await authorisation.require(
            context.principal,
            "workflows:run",
            AuthorisationResource(
                type=ResourceType.WORKFLOW,
                id=definition.name,
                organisation_id=definition.organisation_id,
            ),
            credential_restrictions=context.permission_restrictions,
        )

    async def _authorise_bench(
        self,
        request: DistributedWorkflowRequest,
        bench: GlobalBenchRecord,
    ) -> bool:
        authorisation = self._authorisation
        context = request.authentication_context
        if authorisation is None or context is None:
            return True
        resource = AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        )
        allowed = await authorisation.is_allowed(
            context.principal,
            "benches:operate",
            resource,
            credential_restrictions=context.permission_restrictions,
        )
        if not allowed and request.bench_id is not None:
            await authorisation.require(
                context.principal,
                "benches:operate",
                resource,
                credential_restrictions=context.permission_restrictions,
            )
        return allowed

    async def _authorise_reservation_release(
        self,
        request: DistributedWorkflowRequest,
        bench: GlobalBenchRecord,
    ) -> None:
        authorisation = self._authorisation
        context = request.authentication_context
        if authorisation is None or context is None:
            return
        await authorisation.require(
            context.principal,
            "benches:reserve",
            AuthorisationResource(
                type=ResourceType.BENCH,
                id=bench.id,
                organisation_id=bench.organisation_id,
                parent_agent_id=bench.agent_id,
            ),
            credential_restrictions=context.permission_restrictions,
        )

    async def _claim_request(
        self,
        request_key: tuple[UUID, str],
        fingerprint: str,
    ) -> tuple[bool, asyncio.Future[DistributedWorkflowDispatch]]:
        async with self._request_lock:
            existing = self._requests.get(request_key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise WorkflowInvalidError(
                        "Workflow idempotency key was reused with different request content.",
                        idempotency_key=request_key[1],
                    )
                return False, existing.future
            future: asyncio.Future[DistributedWorkflowDispatch] = (
                asyncio.get_running_loop().create_future()
            )
            self._requests[request_key] = _IdempotentRequest(fingerprint, future)
            return True, future

    async def _fail_request(
        self,
        request_key: tuple[UUID, str],
        future: asyncio.Future[DistributedWorkflowDispatch],
        error: BaseException,
    ) -> None:
        async with self._request_lock:
            current = self._requests.get(request_key)
            if current is not None and current.future is future:
                self._requests.pop(request_key, None)
            if not future.done():
                future.set_exception(error)
                # A leader may have no followers. Reading the exception prevents
                # an unobserved-Future warning without changing what followers see.
                future.exception()

    async def _trim_idempotency_cache(self) -> None:
        async with self._request_lock:
            excess = len(self._requests) - self._maximum_idempotency_entries
            if excess <= 0:
                return
            for key, entry in tuple(self._requests.items()):
                if excess <= 0:
                    break
                if entry.future.done():
                    self._requests.pop(key, None)
                    excess -= 1


def _validate_workflow_request(
    request: DistributedWorkflowRequest,
) -> tuple[
    WorkflowDefinition,
    dict[str, object],
    tuple[tuple[str, ArtifactReference], ...],
]:
    try:
        definition = WorkflowDefinition.model_validate(request.definition.model_dump(mode="python"))
    except (AttributeError, ValidationError) as exc:
        raise WorkflowInvalidError("Workflow definition is incomplete or invalid.") from exc
    supplied = dict(request.inputs)
    if any(not isinstance(name, str) for name in supplied):
        raise WorkflowInvalidError("Workflow input names must be strings.")
    if definition.version < 2 and not definition.inputs:
        resolved = resolve_workflow_inputs(definition, supplied)
        return resolved, supplied, ()

    unknown = sorted(set(supplied).difference(definition.inputs))
    if unknown:
        raise WorkflowInvalidError(
            f"Unknown workflow inputs: {', '.join(unknown)}.",
            unknown_inputs=unknown,
        )
    normalized: dict[str, object] = {}
    artifact_inputs: list[tuple[str, ArtifactReference]] = []
    for name, declaration in definition.inputs.items():
        if name in supplied:
            raw = supplied[name]
        elif declaration.default is not None:
            raw = declaration.default
        elif declaration.required:
            raise WorkflowInvalidError(
                f"Workflow input {name!r} is required.",
                missing_input=name,
            )
        else:
            continue
        if isinstance(declaration, StringWorkflowInput):
            if not isinstance(raw, str):
                raise WorkflowInvalidError(f"Workflow input {name!r} must be a string.")
            value: object = raw
        elif isinstance(declaration, IntegerWorkflowInput):
            if isinstance(raw, bool):
                raise WorkflowInvalidError(f"Workflow input {name!r} must be an integer.")
            if isinstance(raw, int):
                value = raw
            elif isinstance(raw, str) and _INTEGER_INPUT.fullmatch(raw.strip()):
                value = int(raw)
            else:
                raise WorkflowInvalidError(f"Workflow input {name!r} must be an integer.")
        elif isinstance(declaration, BooleanWorkflowInput):
            if isinstance(raw, bool):
                value = raw
            elif isinstance(raw, str) and raw.strip().casefold() in {"true", "false"}:
                value = raw.strip().casefold() == "true"
            else:
                raise WorkflowInvalidError(f"Workflow input {name!r} must be a boolean.")
        elif isinstance(declaration, ArtifactWorkflowInput):
            try:
                reference = (
                    raw
                    if isinstance(raw, ArtifactReference)
                    else ArtifactReference.model_validate(raw)
                )
            except ValidationError as exc:
                raise WorkflowInvalidError(
                    f"Workflow input {name!r} must be an artifact reference.",
                    input_name=name,
                ) from exc
            value = reference
            artifact_inputs.append((name, reference))
        else:  # pragma: no cover - the discriminated union is closed
            raise WorkflowInvalidError(f"Workflow input {name!r} is unsupported.")
        normalized[name] = value

    resolved = resolve_workflow_inputs(
        definition,
        normalized,
        artifact_resolver=lambda reference: Path(_artifact_target_path(reference.artifact_id)),
    )
    return resolved, normalized, tuple(artifact_inputs)


def _normalise_required_capabilities(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise WorkflowInvalidError("Required capabilities must be a collection of names.")
    normalised: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise WorkflowInvalidError("Required capability names must be non-empty strings.")
        normalised.add(_normalise_capability(value))
    return tuple(sorted(normalised))


def _normalise_capability(value: str) -> str:
    normalized = value.strip().casefold()
    return "flash" if normalized == "firmware" else normalized


def _authenticated_request_context(
    request: DistributedWorkflowRequest,
    definition: WorkflowDefinition,
) -> AuthenticationContext | None:
    context = request.authentication_context
    if context is None:
        if request.allow_legacy_authorisation:
            if request.actor_context is not None:
                raise WorkflowInvalidError(
                    "Legacy workflow authorisation cannot carry an authenticated actor."
                )
            return None
        raise AuthenticationRequiredError(
            "An authenticated principal is required to run workflows."
        )
    if request.allow_legacy_authorisation:
        raise WorkflowInvalidError(
            "Legacy workflow authorisation cannot be combined with an authenticated principal."
        )

    principal = context.principal
    if principal.organisation_id != definition.organisation_id:
        raise WorkflowInvalidError(
            "Workflow organisation does not match the authenticated principal.",
            workflow_name=definition.name,
        )
    if request.organisation_id != principal.organisation_id:
        raise WorkflowInvalidError(
            "Workflow request organisation does not match the authenticated principal.",
            workflow_name=definition.name,
        )

    actor = request.actor_context
    if actor is None:
        raise WorkflowInvalidError(
            "Authenticated workflow requests must carry their durable actor context."
        )
    if (
        actor.principal_id != principal.id
        or actor.principal_type is not principal.type
        or actor.display_name != principal.display_name
        or actor.organisation_id != principal.organisation_id
        or actor.authorisation_snapshot_id != context.authorisation_snapshot_id
    ):
        raise WorkflowInvalidError(
            "Workflow actor context does not match the authenticated principal."
        )
    owner_principal = request.owner_principal
    if owner_principal is not None and (
        owner_principal.principal_id != principal.id
        or owner_principal.principal_type is not principal.type
        or owner_principal.display_name != principal.display_name
    ):
        raise WorkflowInvalidError(
            "Workflow reservation owner does not match the authenticated principal."
        )
    return context


def _workflow_fingerprint(
    request: DistributedWorkflowRequest,
    *,
    definition: WorkflowDefinition,
    inputs: Mapping[str, object],
    owner: str,
) -> str:
    payload = {
        "definition": definition.model_dump(mode="json"),
        "inputs": _json_inputs(inputs),
        "owner": owner,
        "owner_principal": (
            request.owner_principal.model_dump(mode="json")
            if request.owner_principal is not None
            else None
        ),
        "actor_context": (
            request.actor_context.model_dump(
                mode="json",
                exclude={"authorisation_snapshot_id"},
            )
            if request.actor_context is not None
            else None
        ),
        "authentication_context": _authentication_fingerprint(request.authentication_context),
        "organisation_id": (
            str(request.organisation_id) if request.organisation_id is not None else None
        ),
        "allow_legacy_authorisation": request.allow_legacy_authorisation,
        "bench_id": request.bench_id,
        "kind": request.kind.value if request.kind is not None else None,
        "location": request.location,
        "preferred_location": request.preferred_location,
        "bench_labels": dict(request.bench_labels),
        "preferred_bench_labels": dict(request.preferred_bench_labels),
        "agent_labels": dict(request.agent_labels),
        "required_capabilities": list(request.required_capabilities),
        "reservation_duration_seconds": request.reservation_duration_seconds,
        "lease_ttl_seconds": request.lease_ttl_seconds,
        "command_timeout_seconds": request.command_timeout_seconds,
        "manage_reservation_lifecycle": request.manage_reservation_lifecycle,
        "reservation_id": (
            str(request.reservation_id) if request.reservation_id is not None else None
        ),
        "release_reservation_after": request.release_reservation_after,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _effective_request_organisation(request: DistributedWorkflowRequest) -> UUID:
    if request.authentication_context is not None:
        return request.authentication_context.principal.organisation_id
    return request.organisation_id or LEGACY_ORGANISATION_ID


def _authentication_fingerprint(
    context: AuthenticationContext | None,
) -> dict[str, object] | None:
    if context is None:
        return None
    return {
        "principal": context.principal.model_dump(mode="json"),
        "permission_restrictions": (
            sorted(context.permission_restrictions)
            if context.permission_restrictions is not None
            else None
        ),
    }


def _json_inputs(inputs: Mapping[str, object]) -> dict[str, object]:
    encoded: dict[str, object] = {}
    for name, value in inputs.items():
        if isinstance(value, ArtifactReference):
            encoded[name] = value.model_dump(mode="json")
        else:
            encoded[name] = value
    return encoded


def _workflow_reservation_lifecycle_payload(
    request: DistributedWorkflowRequest,
    *,
    reused_reservation: bool,
) -> dict[str, object]:
    if reused_reservation:
        management = _CALLER_MANAGED_RESERVATION
        release_after = request.release_reservation_after is True
    else:
        management = (
            _WORKFLOW_MANAGED_RESERVATION
            if request.manage_reservation_lifecycle
            else _CALLER_MANAGED_RESERVATION
        )
        release_after = request.manage_reservation_lifecycle
    return {
        _RESERVATION_LIFECYCLE_MANAGEMENT_KEY: management,
        _RESERVATION_LIFECYCLE_RELEASE_AFTER_KEY: release_after,
    }


def _command_release_reservation_after(command: RemoteCommand) -> bool | None:
    if _RESERVATION_LIFECYCLE_METADATA_KEY not in command.payload:
        # Commands persisted before Phase 7 use reservation metadata instead.
        return None
    raw_lifecycle = command.payload[_RESERVATION_LIFECYCLE_METADATA_KEY]
    if not isinstance(raw_lifecycle, Mapping):
        return False
    management = raw_lifecycle.get(_RESERVATION_LIFECYCLE_MANAGEMENT_KEY)
    release_after = raw_lifecycle.get(_RESERVATION_LIFECYCLE_RELEASE_AFTER_KEY)
    if management not in {
        _WORKFLOW_MANAGED_RESERVATION,
        _CALLER_MANAGED_RESERVATION,
    } or not isinstance(release_after, bool):
        return False
    return release_after


def _require_reservation_principal_owner(
    reservation: CoordinatedReservationLease,
    owner_principal: ReservationOwner | None,
) -> None:
    stored_id = reservation.reservation.owner_principal_id
    stored_type = reservation.reservation.owner_principal_type
    if stored_id is None and stored_type is None and owner_principal is None:
        return
    if (
        owner_principal is None
        or stored_id != owner_principal.principal_id
        or stored_type != owner_principal.principal_type.value
    ):
        raise ReservationOwnerMismatchError(
            "Reservation owner principal does not match.",
            reservation_id=str(reservation.reservation.id),
        )


def _derived_key(kind: str, raw_key: str, *parts: str) -> str:
    material = "\0".join((raw_key, *parts))
    return f"{kind}:{hashlib.sha256(material.encode()).hexdigest()}"


def _artifact_target_path(artifact_id: UUID) -> str:
    return f"artifacts/{artifact_id}"


def _validate_transfer_descriptor(
    descriptor: WorkflowArtifactTransferDescriptor,
    *,
    agent_id: UUID,
    input_name: str,
    artifact_id: UUID,
    target_path: str,
) -> None:
    if (
        descriptor.agent_id != agent_id
        or descriptor.input_name != input_name
        or descriptor.artifact_id != artifact_id
        or descriptor.target_path != target_path
    ):
        raise ValueError("Artifact transfer descriptor does not match its scoped request")


def _require_safe_relative_path(value: str) -> None:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("Artifact transfer target_path must be a safe relative path")


def _labels_match(actual: Mapping[str, str], required: Mapping[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in required.items())


def _no_compatible_bench(
    request: DistributedWorkflowRequest,
    definition: WorkflowDefinition,
) -> NoCompatibleBenchError:
    return NoCompatibleBenchError(
        "No online, non-draining global bench satisfies the workflow requirements.",
        workflow_name=definition.name,
        workflow_version=definition.version,
        bench_id=request.bench_id,
        kind=request.kind.value if request.kind is not None else None,
        location=request.location,
        capabilities=sorted(
            {
                *definition.requirements.capabilities,
                *request.required_capabilities,
            }
        ),
        bench_labels={**definition.requirements.labels, **dict(request.bench_labels)},
        agent_labels=dict(request.agent_labels),
    )


def _is_atomic_eligibility_race(error: ReservationLeaseInvalidError) -> bool:
    return "agent_id" in error.details and "bench_id" in error.details


def _require_text(value: str, *, field: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field} cannot exceed {maximum_length} characters")
    return normalized


def _require_positive_optional_seconds(value: int | None, *, field: str) -> None:
    if value is not None:
        _require_positive_seconds(value, field=field)


def _require_positive_seconds(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DistributedWorkflowCoordinator",
    "DistributedWorkflowDispatch",
    "DistributedWorkflowRequest",
    "DistributedWorkflowReservationLifecycle",
    "WorkflowArtifactTransferDescriptor",
    "WorkflowArtifactTransferPort",
    "WorkflowAuthorisationService",
    "WorkflowCommandService",
    "WorkflowDefinitionCatalog",
    "WorkflowInventory",
    "WorkflowPresence",
    "WorkflowRemoteWorkRepository",
    "WorkflowReservationService",
]
