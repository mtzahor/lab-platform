from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    BenchNotFoundError,
    PermissionDeniedError,
    PlatformError,
    QueueEntryNotFoundError,
    QueueOwnerMismatchError,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    ApiToken,
    ApiTokenScope,
    AuthenticationContext,
    AuthorisationResource,
    CiSession,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    PrincipalType,
    QueueEntry,
    QueueEntryStatus,
    RemoteCommandType,
    Reservation,
    ReservationStatus,
    ResourceType,
)
from pydantic import BaseModel, ConfigDict, Field

AuthenticatedActor = ApiToken | AuthenticationContext
AuthenticationDependency = Callable[..., object]

_ACTIVE_OPERATION_STATUSES = frozenset(
    {
        DistributedOperationStatus.CREATED,
        DistributedOperationStatus.DISPATCHED,
        DistributedOperationStatus.ACCEPTED,
        DistributedOperationStatus.RUNNING,
        DistributedOperationStatus.UNKNOWN,
        DistributedOperationStatus.RECONCILING,
    }
)
_ACTIVE_RESERVATION_STATES = frozenset(
    {
        ReservationLeaseState.ACTIVATING,
        ReservationLeaseState.ACTIVE,
        ReservationLeaseState.RENEWING,
        ReservationLeaseState.UNKNOWN,
    }
)
_MAX_SSE_CONNECTION_SECONDS = 60
_BENCH_AGENT_TIMELINE_EVENTS = frozenset(
    {
        "AGENT_CONNECTED",
        "AGENT_DISCONNECTED",
        "AGENT_CREDENTIAL_ROTATED",
        "AGENT_DRAIN_REQUESTED",
        "AGENT_REVOKED",
        "AGENT_UNDRAINED",
        "INVENTORY_REFRESH_REQUESTED",
        "RECONCILIATION_REQUESTED",
        "REMOTE_COMMAND_REPLAY_DEFERRED",
    }
)


async def _sse_pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _sse_frame(
    event_type: str,
    sequence: int,
    *,
    data: dict[str, object] | None = None,
    observed_at: datetime | None = None,
) -> str:
    timestamp = observed_at or datetime.now(UTC)
    event_id = f"{int(timestamp.timestamp() * 1000)}-{sequence}"
    envelope: dict[str, object] = {
        "id": event_id,
        "type": event_type,
        "timestamp": timestamp.isoformat(),
    }
    if data is not None:
        envelope["data"] = data
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n"
    )


def _request_bearer_credentials(
    request: Request,
) -> HTTPAuthorizationCredentials | None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, credential = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credential.strip():
        return None
    return HTTPAuthorizationCredentials(
        scheme=scheme,
        credentials=credential.strip(),
    )


def _same_authenticated_actor(
    expected: AuthenticatedActor | None,
    current: AuthenticatedActor | None,
) -> bool:
    if expected is None or current is None:
        return expected is current
    if isinstance(expected, ApiToken) or isinstance(current, ApiToken):
        return (
            isinstance(expected, ApiToken)
            and isinstance(current, ApiToken)
            and expected.id == current.id
        )
    return (
        expected.principal.id == current.principal.id
        and expected.principal.type is current.principal.type
        and expected.principal.organisation_id == current.principal.organisation_id
        and expected.session_id == current.session_id
        and expected.credential_id == current.credential_id
    )


async def _refresh_sse_actor(
    request: Request,
    expected: AuthenticatedActor | None,
    authentication_dependency: AuthenticationDependency,
) -> tuple[bool, AuthenticatedActor | None]:
    try:
        pending = authentication_dependency(
            request,
            _request_bearer_credentials(request),
        )
        current = await cast(Awaitable[AuthenticatedActor | None], pending)
    except PlatformError:
        return False, None
    return _same_authenticated_actor(expected, current), current


def _last_event_sequence(last_event_id: str | None) -> int:
    if not last_event_id:
        return 0
    _prefix, separator, raw_sequence = last_event_id.rpartition("-")
    if not separator:
        return 0
    try:
        return max(0, int(raw_sequence))
    except ValueError:
        return 0


def _mapping_records(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _record_ids(value: object) -> set[str]:
    return {
        str(identifier)
        for item in _mapping_records(value)
        if (identifier := item.get("id")) is not None
    }


def _resource_views(data: Mapping[str, object]) -> dict[str, dict[str, object]]:
    counts = data.get("counts")
    count_values = dict(counts) if isinstance(counts, Mapping) else {}
    active_operations = _mapping_records(data.get("active_operations"))
    workflow_operations = [
        item
        for item in active_operations
        if item.get("operation_type") == RemoteCommandType.RUN_WORKFLOW.value
    ]
    serial_operations = [
        item
        for item in active_operations
        if item.get("operation_type") == RemoteCommandType.READ_SERIAL.value
    ]
    return {
        "operation.updated": {"active_operations": active_operations},
        "workflow.updated": {
            "active_operations": workflow_operations,
            "failed_workflow_runs": _mapping_records(data.get("failed_workflow_runs")),
        },
        "serial.updated": {"active_operations": serial_operations},
        "agent.updated": {
            "agents_online": count_values.get("agents_online", 0),
            "recent_agent_disconnects": _mapping_records(data.get("recent_agent_disconnects")),
        },
        "bench.updated": {
            "benches_available": count_values.get("benches_available", 0),
            "benches_offline": count_values.get("benches_offline", 0),
            "degraded_benches": _mapping_records(data.get("degraded_benches")),
        },
        "reservation.updated": {
            "active_reservations": _mapping_records(data.get("active_reservations")),
            "upcoming_reservations": _mapping_records(data.get("upcoming_reservations")),
            "queued_reservations": count_values.get("queued_reservations", 0),
        },
        "ci_session.updated": {
            "active_ci_sessions": _mapping_records(data.get("active_ci_sessions"))
        },
    }


def _view_record_ids(view: Mapping[str, object]) -> set[str]:
    identifiers: set[str] = set()
    for value in view.values():
        identifiers.update(_record_ids(value))
    return identifiers


def _resource_event_payload(
    event_type: str,
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> dict[str, object]:
    previous_ids = _view_record_ids(previous)
    current_ids = _view_record_ids(current)
    ended_ids = previous_ids - current_ids
    new_ids = current_ids - previous_ids
    notify = False
    status_value = "UPDATED"
    summary = {
        "operation.updated": "Operation activity changed.",
        "workflow.updated": "Workflow activity changed.",
        "serial.updated": "Serial capture activity changed.",
        "agent.updated": "Agent connectivity changed.",
        "bench.updated": "Bench availability changed.",
        "reservation.updated": "Reservation activity changed.",
        "ci_session.updated": "CI session activity changed.",
    }[event_type]

    if event_type == "operation.updated" and ended_ids:
        previous_operations = {
            str(item.get("id")): item
            for item in _mapping_records(previous.get("active_operations"))
        }
        ended_interactive = {
            identifier
            for identifier in ended_ids
            if previous_operations.get(identifier, {}).get("operation_type")
            not in {
                RemoteCommandType.RUN_WORKFLOW.value,
                RemoteCommandType.READ_SERIAL.value,
            }
        }
        if ended_interactive:
            notify = True
            status_value = "SUCCEEDED"
            summary = "An interactive operation finished."
    elif event_type == "workflow.updated":
        previous_failed = _record_ids(previous.get("failed_workflow_runs"))
        current_failed = _record_ids(current.get("failed_workflow_runs"))
        if current_failed - previous_failed:
            notify = True
            status_value = "FAILED"
            summary = "A workflow failed."
        elif ended_ids:
            notify = True
            status_value = "SUCCEEDED"
            summary = "A workflow finished."
    elif event_type == "serial.updated" and ended_ids:
        notify = True
        status_value = "SUCCEEDED"
        summary = "A serial capture finished."
    elif event_type == "agent.updated":
        raw_before_online = previous.get("agents_online", 0)
        raw_after_online = current.get("agents_online", 0)
        before_online = raw_before_online if isinstance(raw_before_online, int) else 0
        after_online = raw_after_online if isinstance(raw_after_online, int) else 0
        if after_online != before_online or new_ids:
            notify = True
            if after_online < before_online or new_ids:
                status_value = "WARNING"
                summary = "An Agent disconnected."
            else:
                status_value = "ACTIVE"
                summary = "An Agent connected."
    elif event_type == "bench.updated":
        previous_degraded = _record_ids(previous.get("degraded_benches"))
        current_degraded = _record_ids(current.get("degraded_benches"))
        if current_degraded - previous_degraded:
            notify = True
            status_value = "WARNING"
            summary = "A bench became degraded or offline."
        elif previous_degraded - current_degraded:
            notify = True
            status_value = "SUCCEEDED"
            summary = "A bench recovered."
    elif event_type == "reservation.updated":
        previous_active = _record_ids(previous.get("active_reservations"))
        current_active = _record_ids(current.get("active_reservations"))
        activated = current_active - previous_active
        completed = previous_active - current_active
        if activated or completed or ended_ids or new_ids:
            notify = True
            status_value = "ACTIVE" if activated or new_ids else "COMPLETE"
            summary = "Reservation activity changed."
    elif event_type == "ci_session.updated" and (ended_ids or new_ids):
        notify = True
        status_value = "ACTIVE" if new_ids else "COMPLETE"
        summary = "CI session activity changed."

    return {
        "resource_type": event_type.removesuffix(".updated").upper(),
        "summary": summary,
        "status": status_value,
        "notify": notify,
        "data": {
            "previous_count": len(previous_ids),
            "current_count": len(current_ids),
        },
    }


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OverviewCounts(DashboardModel):
    benches_total: int = 0
    benches_available: int = 0
    benches_reserved: int = 0
    benches_offline: int = 0
    agents_online: int = 0
    active_operations: int = 0
    queued_reservations: int = 0
    failed_workflows_24h: int = 0
    active_ci_sessions: int = 0


class ReservationActionPermissions(DashboardModel):
    owned_by_caller: bool = False
    release: bool = False
    extend: bool = False
    cancel: bool = False
    administrator: bool = False


class ReservationSummary(DashboardModel):
    id: UUID
    bench_id: str
    owner: str
    state: ReservationLeaseState
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    lease_valid_until: datetime
    lease_version: int
    release_pending: bool = False
    permissions: ReservationActionPermissions


class UpcomingReservationSummary(DashboardModel):
    id: UUID
    bench_id: str
    owner: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None
    permissions: ReservationActionPermissions


class WorkflowRunSummary(DashboardModel):
    id: UUID
    workflow_name: str
    agent_id: UUID
    bench_id: str
    reservation_id: UUID | None = None
    status: str
    operation_status: DistributedOperationStatus
    progress: int | None = None
    message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class OperationSummary(DashboardModel):
    id: UUID
    operation_type: str
    agent_id: UUID
    bench_id: str
    reservation_id: UUID | None = None
    status: DistributedOperationStatus
    progress: int | None = None
    message: str | None = None
    created_at: datetime
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_agent_update_at: datetime | None = None
    reconciliation_deadline: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class OverviewResponse(DashboardModel):
    generated_at: datetime
    counts: OverviewCounts
    active_operations: list[OperationSummary]
    failed_workflow_runs: list[WorkflowRunSummary]
    degraded_benches: list[GlobalBenchRecord]
    active_reservations: list[ReservationSummary]
    upcoming_reservations: list[UpcomingReservationSummary]
    recent_agent_disconnects: list[AgentRecord]
    active_ci_sessions: list[CiSession]


class WorkflowRunsPage(DashboardModel):
    items: list[WorkflowRunSummary]
    total: int


class SerialLine(DashboardModel):
    cursor: int = Field(ge=0)
    timestamp: datetime | None = None
    text: str


class SerialArtifactReference(DashboardModel):
    id: UUID
    local_artifact_id: UUID
    name: str
    size_bytes: int = Field(ge=0)
    sha256: str
    content_type: str | None = None
    ready: bool
    truncated: bool = False
    download_url: str


class SerialWindow(DashboardModel):
    operation_id: UUID
    operation_status: DistributedOperationStatus
    connection_state: Literal["LIVE", "RECONNECTING", "STALE", "COMPLETE"]
    lines: list[SerialLine]
    cursor: int
    next_cursor: int
    total: int
    retained_from: int
    has_more: bool
    truncated: bool = False
    tail_truncated: bool = False
    artifact_truncated: bool = False
    artifact: SerialArtifactReference | None = None


class TimelineEvent(DashboardModel):
    id: str
    timestamp: datetime
    event_type: str
    severity: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    title: str
    data: dict[str, object] = Field(default_factory=dict)


class BenchTimelineResponse(DashboardModel):
    bench_id: str
    generated_at: datetime
    items: list[TimelineEvent]


class QueueCreateRequest(DashboardModel):
    duration_seconds: int = Field(default=1_800, gt=0, le=86_400)
    idempotency_key: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2_000)
    owner: str | None = Field(default=None, min_length=1, max_length=200)


class QueueEntryResponse(DashboardModel):
    id: UUID
    bench_id: str
    requester: str
    requested_duration_seconds: int
    description: str | None = None
    status: QueueEntryStatus
    position: int | None = None
    created_at: datetime
    expected_available_at: datetime | None = None
    cancellable: bool = False


class QueuePage(DashboardModel):
    items: list[QueueEntryResponse]
    total: int


def create_dashboard_router(
    runtime: ControlPlaneRuntime,
    *,
    collection_auth: AuthenticationDependency,
    bench_auth: AuthenticationDependency,
    operation_auth: AuthenticationDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["dashboard"])
    collection_dependency = Depends(collection_auth)
    bench_dependency = Depends(bench_auth)
    operation_dependency = Depends(operation_auth)

    @router.get("/overview", response_model=OverviewResponse)
    async def overview(
        token: AuthenticatedActor | None = collection_dependency,
    ) -> OverviewResponse:
        return await build_overview(runtime, token)

    @router.get("/workflow-runs", response_model=WorkflowRunsPage)
    async def list_workflow_runs(
        token: AuthenticatedActor | None = collection_dependency,
        operation_status: Annotated[
            DistributedOperationStatus | None, Query(alias="status")
        ] = None,
        bench_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 500,
    ) -> WorkflowRunsPage:
        operations = await _visible_operations(
            runtime,
            token,
            bench_id=bench_id,
            status=operation_status,
            limit=limit,
        )
        runs = [
            _workflow_run_summary(operation)
            for operation in operations
            if operation.operation_type == RemoteCommandType.RUN_WORKFLOW.value
        ]
        return WorkflowRunsPage(items=runs, total=len(runs))

    @router.get("/benches/{bench_id:path}/queue", response_model=QueuePage)
    async def list_queue(
        bench_id: str,
        token: AuthenticatedActor | None = bench_dependency,
    ) -> QueuePage:
        bench = await _require_visible_bench(runtime, token, bench_id)
        entries = await runtime.reservation_queue.list(
            bench_id=bench_id,
            organisation_id=_organisation_id(token),
        )
        visible = [entry for entry in entries if _queue_entry_visible(token, entry)]
        administrative = await _queue_administration_allowed(runtime, token, bench)
        current = await runtime.reservation_repository.get_current_for_bench(
            bench_id,
            organisation_id=_organisation_id(token),
        )
        expected = current.reservation.ends_at if current is not None else None
        responses: list[QueueEntryResponse] = []
        for entry in visible:
            responses.append(
                _queue_entry_response(
                    entry,
                    token,
                    expected_available_at=expected,
                    administrative=administrative,
                )
            )
            if expected is not None:
                expected += timedelta(seconds=entry.requested_duration_seconds)
        return QueuePage(
            items=responses,
            total=len(visible),
        )

    @router.post(
        "/benches/{bench_id:path}/queue",
        response_model=QueueEntryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def join_queue(
        bench_id: str,
        body: QueueCreateRequest,
        token: AuthenticatedActor | None = bench_dependency,
    ) -> QueueEntryResponse:
        bench = await _require_visible_bench(runtime, token, bench_id)
        principal_type: Literal["USER", "SERVICE_ACCOUNT"] | None
        if isinstance(token, AuthenticationContext):
            allowed = await runtime.authorisation.is_allowed(
                token.principal,
                "benches:reserve",
                AuthorisationResource(
                    type=ResourceType.BENCH,
                    id=bench.id,
                    organisation_id=bench.organisation_id,
                    parent_agent_id=bench.agent_id,
                ),
                credential_restrictions=token.permission_restrictions,
            )
            if not allowed:
                raise PermissionDeniedError(
                    "The caller cannot join the reservation queue for this bench."
                )
            owner = token.principal.display_name
            principal_id = token.principal.id
            principal_type = (
                "USER" if token.principal.type is PrincipalType.USER else "SERVICE_ACCOUNT"
            )
        elif isinstance(token, ApiToken):
            if ApiTokenScope.RESERVATIONS_WRITE not in token.scopes:
                raise PermissionDeniedError("The API token cannot join reservation queues.")
            if body.owner is not None and body.owner != token.owner:
                raise QueueOwnerMismatchError(
                    "A legacy API token cannot queue work for another owner."
                )
            owner = token.owner
            principal_id = None
            principal_type = None
        else:
            raise AuthenticationRequiredError("Authentication is required.")
        existing = await runtime.reservation_queue.get_by_idempotency_key(
            bench.id,
            body.idempotency_key,
            organisation_id=bench.organisation_id,
        )
        if existing is not None:
            if not _queue_entry_owned_by(token, existing):
                raise QueueOwnerMismatchError(
                    "The queue idempotency key belongs to another caller."
                )
            return _queue_entry_response(existing, token)
        entry = await runtime.reservation_queue.create(
            QueueEntry(
                organisation_id=bench.organisation_id,
                bench_id=bench.id,
                owner=owner,
                owner_principal_id=principal_id,
                owner_principal_type=principal_type,
                requested_duration_seconds=body.duration_seconds,
                description=body.description,
                created_at=datetime.now(UTC),
                idempotency_key=body.idempotency_key,
            )
        )
        if not _queue_entry_owned_by(token, entry):
            raise QueueOwnerMismatchError("The queue idempotency key belongs to another caller.")
        return _queue_entry_response(entry, token)

    @router.delete("/queue/{queue_entry_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
    async def leave_queue(
        queue_entry_id: UUID,
        token: AuthenticatedActor | None = collection_dependency,
    ) -> Response:
        entry = await runtime.reservation_queue.get(
            queue_entry_id,
            organisation_id=_organisation_id(token),
        )
        if entry is None:
            raise QueueEntryNotFoundError("Queue entry does not exist.")
        if not await _queue_cancellation_allowed(runtime, token, entry):
            raise QueueOwnerMismatchError(
                "Only the queue entry owner or a bench administrator may cancel it."
            )
        await runtime.reservation_queue.cancel(
            queue_entry_id,
            entry.owner,
            datetime.now(UTC),
            organisation_id=entry.organisation_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/operations/{operation_id:uuid}/serial", response_model=SerialWindow)
    async def operation_serial(
        operation_id: UUID,
        token: AuthenticatedActor | None = operation_dependency,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
    ) -> SerialWindow:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        result = operation.result or {}
        raw_lines = result.get("lines")
        source = raw_lines if isinstance(raw_lines, list) else []
        retained_from = _non_negative_int(result.get("line_offset"), default=0)
        available_end = retained_from + len(source)
        total = max(
            available_end,
            _non_negative_int(result.get("line_count"), default=available_end),
        )
        selection_start = max(cursor, retained_from)
        relative_start = max(0, selection_start - retained_from)
        selected = source[relative_start : relative_start + limit]
        lines: list[SerialLine] = []
        for index, item in enumerate(selected, start=selection_start):
            if isinstance(item, dict):
                raw_timestamp = item.get("timestamp") or item.get("observed_at")
                try:
                    timestamp = (
                        datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
                        if raw_timestamp is not None
                        else None
                    )
                except ValueError:
                    timestamp = None
                text = str(item.get("text", ""))
            else:
                timestamp = None
                text = str(item)
            lines.append(SerialLine(cursor=index, timestamp=timestamp, text=text))
        next_cursor = selection_start + len(lines)
        artifact_truncated = result.get("truncated") is True
        remote_artifacts = await runtime.remote_artifacts.list(
            organisation_id=operation.organisation_id,
            command_id=operation.remote_command_id,
            limit=100,
        )
        remote_artifact = next(
            (item for item in remote_artifacts if item.artifact_type == "serial_log"),
            None,
        )
        artifact = (
            SerialArtifactReference(
                id=remote_artifact.id,
                local_artifact_id=remote_artifact.local_artifact_id,
                name=remote_artifact.name,
                size_bytes=remote_artifact.size_bytes,
                sha256=remote_artifact.sha256,
                content_type=remote_artifact.content_type,
                ready=remote_artifact.uploaded_at is not None,
                truncated=artifact_truncated,
                download_url=f"/api/v1/artifacts/{remote_artifact.id}/content",
            )
            if remote_artifact is not None
            else None
        )
        connection_state: Literal["LIVE", "RECONNECTING", "STALE", "COMPLETE"]
        if operation.status in {
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.RECONCILING,
        }:
            connection_state = "RECONNECTING"
        elif operation.status in _ACTIVE_OPERATION_STATUSES:
            connection_state = "LIVE"
        else:
            connection_state = "COMPLETE"
        return SerialWindow(
            operation_id=operation.id,
            operation_status=operation.status,
            connection_state=connection_state,
            lines=lines,
            cursor=cursor,
            next_cursor=next_cursor,
            total=total,
            retained_from=retained_from,
            has_more=next_cursor < available_end,
            truncated=cursor < retained_from,
            tail_truncated=retained_from > 0,
            artifact_truncated=artifact_truncated,
            artifact=artifact,
        )

    @router.get("/benches/{bench_id:path}/timeline", response_model=BenchTimelineResponse)
    async def bench_timeline(
        bench_id: str,
        token: AuthenticatedActor | None = bench_dependency,
        limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
    ) -> BenchTimelineResponse:
        bench = await runtime.operational_access.get_bench(
            bench_id,
            authentication_context=(token if isinstance(token, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(token, ApiToken),
        )
        operations = await _safe_operations(runtime, token, bench_id=bench.id, limit=limit)
        reservations = await _visible_reservations(runtime, token, {bench.id}, limit=limit)
        events: list[TimelineEvent] = []
        for operation in operations:
            timestamp = operation.completed_at or operation.started_at or operation.created_at
            severity: Literal["INFO", "WARNING", "ERROR"] = (
                "ERROR"
                if operation.status is DistributedOperationStatus.FAILED
                else "WARNING"
                if operation.status
                in {
                    DistributedOperationStatus.UNKNOWN,
                    DistributedOperationStatus.RECONCILING,
                    DistributedOperationStatus.CANCELLED,
                }
                else "INFO"
            )
            events.append(
                TimelineEvent(
                    id=f"operation:{operation.id}",
                    timestamp=timestamp,
                    event_type="OPERATION_STATUS",
                    severity=severity,
                    title=f"{operation.operation_type} {operation.status.value.casefold()}",
                    data={
                        "operation_id": str(operation.id),
                        "status": operation.status.value,
                        "progress": operation.progress,
                        "message": operation.message,
                    },
                )
            )
        for record in reservations:
            reservation = record.reservation
            events.append(
                TimelineEvent(
                    id=f"reservation:{reservation.id}:{record.revision}",
                    timestamp=(
                        reservation.released_at
                        or reservation.activated_at
                        or reservation.created_at
                    ),
                    event_type="RESERVATION_STATUS",
                    severity=(
                        "WARNING" if record.state is ReservationLeaseState.UNKNOWN else "INFO"
                    ),
                    title=f"Reservation {record.state.value.casefold()}",
                    data={
                        "reservation_id": str(reservation.id),
                        "state": record.state.value,
                        "owner": reservation.owner,
                    },
                )
            )
        agent_events = await runtime.timeline.list(
            bench.agent_id,
            limit=min(10_000, limit * 4),
        )
        for timeline_record in agent_events:
            event_bench_id = timeline_record.metadata.get("bench_id")
            if event_bench_id is not None:
                if str(event_bench_id) != bench.id:
                    continue
            elif timeline_record.event_type not in _BENCH_AGENT_TIMELINE_EVENTS:
                continue
            timeline_severity: Literal["INFO", "WARNING", "ERROR"] = (
                "ERROR"
                if timeline_record.severity.value == "ERROR"
                else "WARNING"
                if timeline_record.severity.value == "WARNING"
                else "INFO"
            )
            events.append(
                TimelineEvent(
                    id=f"agent-timeline:{timeline_record.id}",
                    timestamp=timeline_record.timestamp,
                    event_type=timeline_record.event_type,
                    severity=timeline_severity,
                    title=timeline_record.message,
                    data={
                        **timeline_record.metadata,
                        "agent_id": str(timeline_record.agent_id),
                    },
                )
            )
        events.append(
            TimelineEvent(
                id=f"bench:{bench.id}:{bench.updated_at.isoformat()}",
                timestamp=bench.updated_at,
                event_type="BENCH_STATUS",
                severity=(
                    "ERROR"
                    if bench.health is HealthStatus.UNHEALTHY
                    else "WARNING"
                    if bench.status is not GlobalBenchStatus.ONLINE
                    else "INFO"
                ),
                title=f"Bench {bench.status.value.casefold()}",
                data={"status": bench.status.value, "health": bench.health.value},
            )
        )
        events.sort(key=lambda item: item.timestamp, reverse=True)
        return BenchTimelineResponse(
            bench_id=bench.id,
            generated_at=datetime.now(UTC),
            items=events[:limit],
        )

    @router.get("/events")
    async def events(
        request: Request,
        token: AuthenticatedActor | None = collection_dependency,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        once: bool = False,
    ) -> StreamingResponse:
        if not runtime.config.web.live_updates.sse_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Server-sent events are disabled; use polling.",
            )

        async def stream() -> AsyncIterator[str]:
            current_actor = token
            previous_digest: str | None = None
            previous_views: dict[str, dict[str, object]] | None = None
            sequence = _last_event_sequence(last_event_id)
            connected_at = datetime.now(UTC)
            while (
                not await request.is_disconnected()
                and (datetime.now(UTC) - connected_at).total_seconds() < _MAX_SSE_CONNECTION_SECONDS
            ):
                with runtime.authorisation.request_scope():
                    actor_is_current, refreshed_actor = await _refresh_sse_actor(
                        request,
                        current_actor,
                        collection_auth,
                    )
                    if not actor_is_current:
                        return
                    current_actor = refreshed_actor
                    snapshot = await build_overview(runtime, current_actor)
                data = snapshot.model_dump(mode="json")
                digest_payload = {
                    key: value for key, value in data.items() if key != "generated_at"
                }
                encoded = json.dumps(
                    digest_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                views = _resource_views(data)
                if digest != previous_digest:
                    sequence += 1
                    event_type = (
                        "overview.snapshot" if previous_digest is None else "overview.updated"
                    )
                    yield _sse_frame(event_type, sequence, data=data)
                    if previous_views is not None:
                        for resource_event, current_view in views.items():
                            previous_view = previous_views.get(resource_event, {})
                            if current_view == previous_view:
                                continue
                            sequence += 1
                            yield _sse_frame(
                                resource_event,
                                sequence,
                                data=_resource_event_payload(
                                    resource_event,
                                    previous_view,
                                    current_view,
                                ),
                            )
                    previous_digest = digest
                    previous_views = views
                else:
                    sequence += 1
                    yield _sse_frame("heartbeat", sequence)
                if once:
                    return
                await _sse_pause(runtime.config.web.live_updates.polling_fallback_seconds)
            yield ": reconnect to refresh authentication\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def build_overview(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
) -> OverviewResponse:
    benches, agents, operations, ci_sessions = await asyncio.gather(
        _safe_benches(runtime, actor),
        _safe_agents(runtime, actor),
        _safe_operations(runtime, actor, limit=500),
        _safe_ci_sessions(runtime, actor),
    )
    reservations = await _visible_reservations(
        runtime,
        actor,
        {bench.id for bench in benches},
        limit=500,
    )
    upcoming = await _visible_scheduled_reservations(
        runtime,
        actor,
        {bench.id for bench in benches},
        limit=500,
    )
    queued = await runtime.reservation_queue.list_waiting(
        organisation_id=_organisation_id(actor),
        limit=10_000,
    )
    queued = [entry for entry in queued if entry.bench_id in {bench.id for bench in benches}]
    active_reservations = [
        record for record in reservations if record.state in _ACTIVE_RESERVATION_STATES
    ]
    reserved_benches = {record.reservation.bench_id for record in active_reservations}
    active_operations = [
        operation for operation in operations if operation.status in _ACTIVE_OPERATION_STATUSES
    ]
    busy_benches = {operation.bench_id for operation in active_operations}
    failed_cutoff = datetime.now(UTC) - timedelta(hours=24)
    failed_workflows = [
        operation
        for operation in operations
        if operation.operation_type == RemoteCommandType.RUN_WORKFLOW.value
        and operation.status is DistributedOperationStatus.FAILED
        and operation.created_at >= failed_cutoff
    ]
    active_ci = [
        session
        for session in ci_sessions
        if session.status
        not in {
            CiSessionStatus.SUCCEEDED,
            CiSessionStatus.FAILED,
            CiSessionStatus.CANCELLED,
            CiSessionStatus.TIMED_OUT,
            CiSessionStatus.COMPLETED,
        }
    ]
    available = [
        bench
        for bench in benches
        if bench.online
        and bench.health is not HealthStatus.UNHEALTHY
        and bench.id not in reserved_benches
        and bench.id not in busy_benches
    ]
    recent_disconnects = [
        agent
        for agent in agents
        if agent.disconnected_at is not None and agent.disconnected_at >= failed_cutoff
    ]
    recent_disconnects.sort(
        key=lambda item: item.disconnected_at or item.registered_at,
        reverse=True,
    )
    active_reservation_rows = active_reservations[:25]
    upcoming_reservation_rows = upcoming[:25]
    active_reservation_permissions = await asyncio.gather(
        *(
            reservation_action_permissions(runtime, actor, reservation)
            for reservation in active_reservation_rows
        )
    )
    upcoming_reservation_permissions = await asyncio.gather(
        *(
            reservation_action_permissions(runtime, actor, reservation)
            for reservation in upcoming_reservation_rows
        )
    )
    return OverviewResponse(
        generated_at=datetime.now(UTC),
        counts=OverviewCounts(
            benches_total=len(benches),
            benches_available=len(available),
            benches_reserved=len(reserved_benches),
            benches_offline=sum(not bench.online for bench in benches),
            agents_online=sum(
                agent.status in {AgentStatus.ONLINE, AgentStatus.DEGRADED} for agent in agents
            ),
            active_operations=len(active_operations),
            queued_reservations=len(queued),
            failed_workflows_24h=len(failed_workflows),
            active_ci_sessions=len(active_ci),
        ),
        active_operations=[_operation_summary(operation) for operation in active_operations[:25]],
        failed_workflow_runs=[
            _workflow_run_summary(operation) for operation in failed_workflows[:25]
        ],
        degraded_benches=[
            bench
            for bench in benches
            if not bench.online
            or bench.status is GlobalBenchStatus.DEGRADED
            or bench.health is not HealthStatus.HEALTHY
        ][:25],
        active_reservations=[
            _reservation_summary(record, permissions)
            for record, permissions in zip(
                active_reservation_rows, active_reservation_permissions, strict=True
            )
        ],
        upcoming_reservations=[
            _upcoming_reservation_summary(reservation, permissions)
            for reservation, permissions in zip(
                upcoming_reservation_rows, upcoming_reservation_permissions, strict=True
            )
        ],
        recent_agent_disconnects=recent_disconnects[:25],
        active_ci_sessions=active_ci[:25],
    )


async def enrich_benches(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    benches: list[GlobalBenchRecord],
) -> list[dict[str, object]]:
    """Build the dashboard's composite bench rows without bypassing read policy."""

    agents, operations = await asyncio.gather(
        _safe_agents(runtime, actor),
        _safe_operations(runtime, actor, limit=10_000),
    )
    reservations = await _visible_reservations(
        runtime,
        actor,
        {bench.id for bench in benches},
        limit=10_000,
    )
    agents_by_id = {agent.id: agent for agent in agents}
    reservations_by_bench = {
        record.reservation.bench_id: record
        for record in reservations
        if record.state in _ACTIVE_RESERVATION_STATES
    }
    operations_by_bench = {
        operation.bench_id: operation
        for operation in reversed(operations)
        if operation.status in _ACTIVE_OPERATION_STATUSES
    }
    permission_rows = await asyncio.gather(
        *(_bench_permissions(runtime, actor, bench) for bench in benches)
    )
    rows: list[dict[str, object]] = []
    for bench, permissions in zip(benches, permission_rows, strict=True):
        agent = agents_by_id.get(bench.agent_id)
        reservation = reservations_by_bench.get(bench.id)
        reservation_payload: dict[str, object] | None = None
        if reservation is not None:
            reservation_permissions = await reservation_action_permissions(
                runtime,
                actor,
                reservation,
                parent_agent_id=bench.agent_id,
                bench_permissions=permissions,
            )
            reservation_payload = _reservation_summary(
                reservation, reservation_permissions
            ).model_dump(mode="json")
        operation = operations_by_bench.get(bench.id)
        if not bench.online:
            availability = "OFFLINE"
        elif agent is not None and agent.status in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
            availability = "DRAINING"
        elif operation is not None:
            availability = "BUSY"
        elif reservation is not None:
            availability = "RESERVED"
        elif bench.health is HealthStatus.UNHEALTHY:
            availability = "UNHEALTHY"
        else:
            availability = "AVAILABLE"
        rows.append(
            {
                **bench.model_dump(mode="json"),
                "agent": (
                    {
                        "id": str(agent.id),
                        "slug": agent.slug,
                        "name": agent.name,
                        "status": agent.status.value,
                        "location": agent.location,
                        "last_seen_at": (
                            agent.last_seen_at.isoformat()
                            if agent.last_seen_at is not None
                            else None
                        ),
                    }
                    if agent is not None
                    else None
                ),
                "availability": availability,
                "current_reservation": reservation_payload,
                "active_operation": (
                    operation.model_dump(mode="json", exclude={"result"})
                    if operation is not None
                    else None
                ),
                "permissions": permissions,
            }
        )
    return rows


async def _require_visible_bench(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    bench_id: str,
) -> GlobalBenchRecord:
    try:
        return await runtime.operational_access.get_bench(
            bench_id,
            authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(actor, ApiToken),
        )
    except BenchNotFoundError:
        raise


def _organisation_id(actor: AuthenticatedActor | None) -> UUID | None:
    return actor.principal.organisation_id if isinstance(actor, AuthenticationContext) else None


def _queue_entry_owned_by(
    actor: AuthenticatedActor | None,
    entry: QueueEntry,
) -> bool:
    if isinstance(actor, AuthenticationContext):
        if entry.owner_principal_id is None:
            return False
        return (
            entry.organisation_id == actor.principal.organisation_id
            and entry.owner_principal_id == actor.principal.id
            and entry.owner_principal_type == actor.principal.type.value
        )
    if isinstance(actor, ApiToken):
        return entry.owner_principal_id is None and entry.owner == actor.owner
    return False


def _reservation_owned_by(
    actor: AuthenticatedActor | None,
    reservation: Reservation,
) -> bool:
    if isinstance(actor, AuthenticationContext):
        if reservation.organisation_id != actor.principal.organisation_id:
            return False
        if reservation.owner_principal_id is not None:
            return (
                reservation.owner_principal_id == actor.principal.id
                and reservation.owner_principal_type == actor.principal.type.value
            )
        return reservation.owner == actor.principal.display_name
    if isinstance(actor, ApiToken):
        return reservation.owner == actor.owner
    return False


async def reservation_action_permissions(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    value: CoordinatedReservationLease | Reservation,
    *,
    parent_agent_id: UUID | None = None,
    bench_permissions: Mapping[str, bool] | None = None,
) -> ReservationActionPermissions:
    """Describe usable reservation actions without weakening route enforcement."""

    reservation = value.reservation if isinstance(value, CoordinatedReservationLease) else value
    if (
        isinstance(actor, AuthenticationContext)
        and reservation.organisation_id != actor.principal.organisation_id
    ):
        return ReservationActionPermissions()

    if parent_agent_id is None and isinstance(value, CoordinatedReservationLease):
        parent_agent_id = value.lease.agent_id
    if parent_agent_id is None:
        bench = await runtime.inventory_repository.get(
            reservation.bench_id,
            organisation_id=_organisation_id(actor),
        )
        if bench is None or bench.organisation_id != reservation.organisation_id:
            return ReservationActionPermissions()
        parent_agent_id = bench.agent_id

    owned = _reservation_owned_by(actor, reservation)
    if isinstance(actor, AuthenticationContext):
        if bench_permissions is None:
            resource = AuthorisationResource(
                type=ResourceType.BENCH,
                id=reservation.bench_id,
                organisation_id=reservation.organisation_id,
                parent_agent_id=parent_agent_id,
            )
            reserve_allowed, administrator = await asyncio.gather(
                runtime.authorisation.is_allowed(
                    actor.principal,
                    "benches:reserve",
                    resource,
                    credential_restrictions=actor.permission_restrictions,
                ),
                runtime.authorisation.is_allowed(
                    actor.principal,
                    "benches:manage",
                    resource,
                    credential_restrictions=actor.permission_restrictions,
                ),
            )
        else:
            reserve_allowed = bool(bench_permissions.get("reserve", False))
            administrator = bool(bench_permissions.get("manage", False))
    elif isinstance(actor, ApiToken):
        reserve_allowed = ApiTokenScope.RESERVATIONS_WRITE in actor.scopes
        # The legacy revoke route is intentionally governed by the same coarse
        # reservations-write scope. Reflect that existing server contract here.
        administrator = reserve_allowed
    else:
        reserve_allowed = False
        administrator = False

    owner_action = owned and reserve_allowed
    active = (
        isinstance(value, CoordinatedReservationLease) and value.state in _ACTIVE_RESERVATION_STATES
    )
    extendable = (
        isinstance(value, CoordinatedReservationLease)
        and value.state is ReservationLeaseState.ACTIVE
    )
    scheduled = isinstance(value, Reservation) and value.status is ReservationStatus.SCHEDULED
    return ReservationActionPermissions(
        owned_by_caller=owned,
        release=active and (owner_action or administrator),
        extend=extendable and owner_action,
        cancel=scheduled and (owner_action or administrator),
        administrator=administrator,
    )


async def _queue_cancellation_allowed(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    entry: QueueEntry,
) -> bool:
    owned = _queue_entry_owned_by(actor, entry)
    if isinstance(actor, ApiToken):
        return (
            owned and ApiTokenScope.RESERVATIONS_WRITE in actor.scopes
        ) or ApiTokenScope.AGENTS_ADMIN in actor.scopes
    if not isinstance(actor, AuthenticationContext):
        return False
    if owned:
        return True
    bench = await runtime.inventory_repository.get(
        entry.bench_id,
        organisation_id=actor.principal.organisation_id,
    )
    if bench is None:
        return False
    return await runtime.authorisation.is_allowed(
        actor.principal,
        "benches:manage",
        AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        ),
        credential_restrictions=actor.permission_restrictions,
    )


async def _queue_administration_allowed(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    bench: GlobalBenchRecord,
) -> bool:
    if isinstance(actor, ApiToken):
        return ApiTokenScope.AGENTS_ADMIN in actor.scopes
    if not isinstance(actor, AuthenticationContext):
        return False
    return await runtime.authorisation.is_allowed(
        actor.principal,
        "benches:manage",
        AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        ),
        credential_restrictions=actor.permission_restrictions,
    )


def _queue_entry_visible(
    actor: AuthenticatedActor | None,
    entry: QueueEntry,
) -> bool:
    # Queue order is operationally useful, but exposing other requesters' names
    # to non-administrators is unnecessary. The response mapper redacts those
    # names while preserving stable FIFO positions.
    return isinstance(actor, (AuthenticationContext, ApiToken))


def _queue_entry_response(
    entry: QueueEntry,
    actor: AuthenticatedActor | None,
    *,
    expected_available_at: datetime | None = None,
    administrative: bool = False,
) -> QueueEntryResponse:
    owned = _queue_entry_owned_by(actor, entry)
    requester = entry.owner if owned or administrative else "Another user"
    if (
        isinstance(actor, AuthenticationContext)
        and actor.principal.type is PrincipalType.SERVICE_ACCOUNT
        and not owned
    ):
        requester = "Another requester"
    return QueueEntryResponse(
        id=entry.id,
        bench_id=entry.bench_id,
        requester=requester,
        requested_duration_seconds=entry.requested_duration_seconds,
        description=entry.description if owned or administrative else None,
        status=entry.status,
        position=entry.position,
        created_at=entry.created_at,
        expected_available_at=expected_available_at,
        cancellable=(owned or administrative) and entry.status is QueueEntryStatus.WAITING,
    )


async def _safe_benches(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
) -> list[GlobalBenchRecord]:
    try:
        return await runtime.operational_access.list_benches(
            authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(actor, ApiToken),
        )
    except (AuthenticationRequiredError, PermissionDeniedError):
        return []


async def _safe_agents(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
) -> list[AgentRecord]:
    if isinstance(actor, ApiToken) and ApiTokenScope.AGENTS_READ not in actor.scopes:
        return []
    try:
        return await runtime.operational_access.list_agents(
            authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(actor, ApiToken),
        )
    except (AuthenticationRequiredError, PermissionDeniedError):
        return []


async def _visible_operations(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    *,
    bench_id: str | None = None,
    status: DistributedOperationStatus | None = None,
    limit: int = 500,
) -> list[DistributedOperation]:
    if isinstance(actor, ApiToken) and ApiTokenScope.OPERATIONS_READ not in actor.scopes:
        return []
    return await runtime.operational_access.list_operations(
        bench_id=bench_id,
        status=status,
        limit=limit,
        authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
        allow_legacy_authorisation=isinstance(actor, ApiToken),
    )


async def _safe_operations(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    *,
    bench_id: str | None = None,
    limit: int = 500,
) -> list[DistributedOperation]:
    try:
        return await _visible_operations(
            runtime,
            actor,
            bench_id=bench_id,
            limit=limit,
        )
    except (AuthenticationRequiredError, PermissionDeniedError):
        return []


async def _safe_ci_sessions(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
) -> list[CiSession]:
    if isinstance(actor, ApiToken) and ApiTokenScope.CI_SESSIONS not in actor.scopes:
        return []
    try:
        return await runtime.operational_access.list_ci_sessions(
            limit=100,
            authentication_context=(actor if isinstance(actor, AuthenticationContext) else None),
            allow_legacy_authorisation=isinstance(actor, ApiToken),
        )
    except (AuthenticationRequiredError, PermissionDeniedError):
        return []


async def _visible_reservations(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    visible_bench_ids: set[str],
    *,
    limit: int,
) -> list[CoordinatedReservationLease]:
    if not visible_bench_ids:
        return []
    if isinstance(actor, ApiToken) and ApiTokenScope.RESERVATIONS_WRITE not in actor.scopes:
        return []
    organisation_id = (
        actor.principal.organisation_id if isinstance(actor, AuthenticationContext) else None
    )
    records = await runtime.reservation_repository.list(
        organisation_id=organisation_id,
        limit=limit,
    )
    return [record for record in records if record.reservation.bench_id in visible_bench_ids]


async def _visible_scheduled_reservations(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    visible_bench_ids: set[str],
    *,
    limit: int,
) -> list[Reservation]:
    if not visible_bench_ids:
        return []
    if isinstance(actor, ApiToken) and ApiTokenScope.RESERVATIONS_WRITE not in actor.scopes:
        return []
    records = await runtime.reservations.list_scheduled(
        organisation_id=_organisation_id(actor),
        starts_after=datetime.now(UTC),
        limit=limit,
    )
    records = [record for record in records if record.bench_id in visible_bench_ids]
    records.sort(key=lambda record: record.starts_at or record.created_at)
    return records


async def _bench_permissions(
    runtime: ControlPlaneRuntime,
    actor: AuthenticatedActor | None,
    bench: GlobalBenchRecord,
) -> dict[str, bool]:
    actions = {
        "read": "benches:read",
        "reserve": "benches:reserve",
        "operate": "benches:operate",
        "reset": "benches:reset",
        "serial": "benches:serial",
        "flash": "benches:flash",
        "manage": "benches:manage",
    }
    if isinstance(actor, AuthenticationContext):
        resource = AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        )
        decisions = await asyncio.gather(
            *(
                runtime.authorisation.is_allowed(
                    actor.principal,
                    permission,
                    resource,
                    credential_restrictions=actor.permission_restrictions,
                )
                for permission in actions.values()
            )
        )
        permissions = dict(zip(actions, decisions, strict=True))
        return _action_permission_aliases(permissions)
    if isinstance(actor, ApiToken):
        permissions = {
            "read": ApiTokenScope.BENCHES_READ in actor.scopes,
            "reserve": ApiTokenScope.RESERVATIONS_WRITE in actor.scopes,
            "operate": ApiTokenScope.WORKFLOWS_RUN in actor.scopes,
            "reset": ApiTokenScope.WORKFLOWS_RUN in actor.scopes,
            "serial": ApiTokenScope.WORKFLOWS_RUN in actor.scopes,
            "flash": (
                ApiTokenScope.WORKFLOWS_RUN in actor.scopes
                and ApiTokenScope.ARTIFACTS_WRITE in actor.scopes
            ),
            "manage": ApiTokenScope.AGENTS_ADMIN in actor.scopes,
        }
        return _action_permission_aliases(permissions)
    return _action_permission_aliases({action: False for action in actions})


def _action_permission_aliases(permissions: dict[str, bool]) -> dict[str, bool]:
    return {
        **permissions,
        # Reservation ownership is unavailable at the bench boundary. Action flags live
        # on each reservation summary and remain false here for compatibility.
        "release": False,
        "extend": False,
        "cancel_reservation": False,
        "probe": permissions["operate"],
        "read_serial": permissions["serial"],
        # These actions require a selected workflow or operation resource; a bench-only
        # permission summary cannot safely pre-authorise them.
        "run_workflow": False,
        "cancel_operation": False,
    }


def _non_negative_int(value: object, *, default: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def _reservation_summary(
    record: CoordinatedReservationLease,
    permissions: ReservationActionPermissions,
) -> ReservationSummary:
    reservation = record.reservation
    return ReservationSummary(
        id=reservation.id,
        bench_id=reservation.bench_id,
        owner=reservation.owner,
        state=record.state,
        starts_at=reservation.starts_at,
        ends_at=reservation.ends_at,
        lease_valid_until=record.lease.valid_until,
        lease_version=record.lease.lease_version,
        release_pending=reservation.release_pending,
        permissions=permissions,
    )


def _upcoming_reservation_summary(
    reservation: Reservation,
    permissions: ReservationActionPermissions,
) -> UpcomingReservationSummary:
    if reservation.starts_at is None or reservation.ends_at is None:
        raise ValueError("Scheduled reservation is missing its bounded time window")
    return UpcomingReservationSummary(
        id=reservation.id,
        bench_id=reservation.bench_id,
        owner=reservation.owner,
        starts_at=reservation.starts_at,
        ends_at=reservation.ends_at,
        description=reservation.metadata.get("description"),
        permissions=permissions,
    )


def _workflow_run_summary(operation: DistributedOperation) -> WorkflowRunSummary:
    raw_workflow = (operation.result or {}).get("workflow_run")
    workflow = raw_workflow if isinstance(raw_workflow, dict) else {}
    workflow_name = str(
        workflow.get("workflow_name") or workflow.get("name") or operation.operation_type
    )
    return WorkflowRunSummary(
        id=operation.id,
        workflow_name=workflow_name,
        agent_id=operation.agent_id,
        bench_id=operation.bench_id,
        reservation_id=operation.reservation_id,
        status=_workflow_status(operation),
        operation_status=operation.status,
        progress=operation.progress,
        message=operation.message,
        created_at=operation.created_at,
        started_at=operation.started_at,
        completed_at=operation.completed_at,
        error_code=operation.error_code,
        error_message=operation.error_message,
    )


def _operation_summary(operation: DistributedOperation) -> OperationSummary:
    return OperationSummary.model_validate(
        operation.model_dump(exclude={"remote_command_id", "organisation_id", "result"})
    )


def _workflow_status(operation: DistributedOperation) -> str:
    if operation.status in {
        DistributedOperationStatus.CREATED,
        DistributedOperationStatus.DISPATCHED,
        DistributedOperationStatus.ACCEPTED,
    }:
        return "pending"
    if operation.status is DistributedOperationStatus.RUNNING:
        return "running"
    if operation.status is DistributedOperationStatus.UNKNOWN:
        return "unknown"
    if operation.status is DistributedOperationStatus.RECONCILING:
        return "reconciling"
    return operation.status.value.casefold()
