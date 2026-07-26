from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from lab_platform.agent.api.auth import require_legacy_scopes
from lab_platform.core.errors import PermissionDeniedError, ReservationNotActiveError
from lab_platform.core.workflows import WorkflowReservationRequiredError
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    QueueEntry,
    ReservationSource,
    ReservationStatus,
    TimelineCategory,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


class Phase3ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OwnerRequest(Phase3ApiModel):
    owner: str = Field(min_length=1, max_length=200)


class CreateReservationRequest(OwnerRequest):
    bench_id: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime | None = None
    duration_seconds: int = Field(gt=0)
    queue_if_busy: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ExtendReservationRequest(OwnerRequest):
    duration_seconds: int = Field(gt=0)


class QueueReservationRequest(OwnerRequest):
    duration_seconds: int = Field(gt=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class RunWorkflowRequest(OwnerRequest):
    bench_id: str = Field(min_length=1, max_length=200)
    reservation_id: UUID | None = None
    reserve_duration_seconds: int | None = Field(default=None, gt=0)
    release_after: bool = False
    inputs: dict[str, str] = Field(default_factory=dict)


def create_phase3_router(agent: "LabAgent") -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    authorize_reservations = require_legacy_scopes(
        agent,
        ApiTokenScope.RESERVATIONS_WRITE,
    )
    authorize_workflows = require_legacy_scopes(agent, ApiTokenScope.WORKFLOWS_RUN)
    authorize_operations = require_legacy_scopes(agent, ApiTokenScope.OPERATIONS_READ)

    @router.get("/reservations")
    async def list_reservations(
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
        bench_id: str | None = None,
        owner: str | None = None,
        status: ReservationStatus | None = None,
        starts_after: AwareDatetime | None = None,
        starts_before: AwareDatetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, object]:
        if token is not None:
            _require_owner(owner, token, allow_unspecified=True)
            owner = token.owner
        reservations = await agent.reservation_service.list(
            bench_id=bench_id,
            owner=owner,
            status=status,
            starts_after=starts_after,
            starts_before=starts_before,
            limit=limit,
        )
        return {"items": [item.model_dump(mode="json") for item in reservations]}

    @router.post("/reservations", status_code=201)
    async def create_reservation(
        request: CreateReservationRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        await agent.backend_registry.get_backend_for_bench(request.bench_id)
        result = await agent.reservation_service.create(
            request.bench_id,
            owner,
            starts_at=request.starts_at,
            duration_seconds=request.duration_seconds,
            queue_if_busy=request.queue_if_busy,
            idempotency_key=request.idempotency_key,
            source=ReservationSource.API,
        )
        return result.model_dump(mode="json")

    @router.get("/reservations/{reservation_id}")
    async def get_reservation(
        reservation_id: UUID,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        reservation = await agent.reservation_service.get(reservation_id)
        _require_owner(reservation.owner, token)
        return reservation.model_dump(mode="json")

    @router.post("/reservations/{reservation_id}/release")
    async def release_reservation(
        reservation_id: UUID,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        reservation = await agent.reservation_service.release(reservation_id, owner)
        if reservation is None:  # pragma: no cover - UUID release always returns a record
            raise ReservationNotActiveError(f"Reservation {reservation_id} is not active.")
        return reservation.model_dump(mode="json")

    @router.post("/reservations/{reservation_id}/extend")
    async def extend_reservation(
        reservation_id: UUID,
        request: ExtendReservationRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        reservation = await agent.reservation_service.extend(
            reservation_id, owner, request.duration_seconds
        )
        return reservation.model_dump(mode="json")

    @router.post("/reservations/{reservation_id}/cancel")
    async def cancel_reservation(
        reservation_id: UUID,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        reservation = await agent.reservation_service.cancel(reservation_id, owner)
        return reservation.model_dump(mode="json")

    @router.get("/benches/{bench_id}/queue")
    async def list_queue(
        bench_id: str,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> dict[str, object]:
        entries = await agent.queue_repository.list(bench_id=bench_id)
        if token is not None:
            entries = [entry for entry in entries if entry.owner == token.owner]
        return {"items": [entry.model_dump(mode="json") for entry in entries]}

    @router.post("/benches/{bench_id}/queue", status_code=201)
    async def queue_reservation(
        bench_id: str,
        request: QueueReservationRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        await agent.backend_registry.get_backend_for_bench(bench_id)
        entry = await agent.reservation_service.enqueue(
            bench_id,
            owner,
            duration_seconds=request.duration_seconds,
            idempotency_key=request.idempotency_key,
        )
        return entry.model_dump(mode="json")

    @router.delete("/queue/{queue_entry_id}", status_code=204)
    async def cancel_queue(
        queue_entry_id: UUID,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_reservations)],
    ) -> Response:
        owner = _authorized_owner(request.owner, token)
        await agent.reservation_service.cancel_queue_entry(queue_entry_id, owner)
        return Response(status_code=204)

    @router.get("/benches/{bench_id}/timeline")
    async def bench_timeline(
        bench_id: str,
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
        category: TimelineCategory | None = None,
        after: AwareDatetime | None = None,
        before: AwareDatetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, object]:
        entries = await agent.timeline_repository.list_timeline(
            bench_id,
            category=category,
            after=after,
            before=before,
            limit=limit,
        )
        if token is not None:
            entries = [entry for entry in entries if entry.actor == token.owner]
        return {"items": [entry.model_dump(mode="json") for entry in entries]}

    @router.get("/workflows")
    async def list_workflows(
        _token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> dict[str, object]:
        definitions = await agent.workflow_service.list_definitions()
        return {"items": [item.model_dump(mode="json") for item in definitions]}

    @router.get("/workflows/{workflow_name}")
    async def get_workflow(
        workflow_name: str,
        _token: Annotated[ApiToken | None, Depends(authorize_workflows)],
        version: int | None = None,
    ) -> object:
        definition = await agent.workflow_service.get_definition(workflow_name, version)
        return definition.model_dump(mode="json")

    @router.post("/workflows/{workflow_name}/runs", status_code=201)
    async def run_workflow(
        workflow_name: str,
        request: RunWorkflowRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        active = await agent.reservation_service.get_active(request.bench_id)
        if request.reserve_duration_seconds is not None and active is None:
            created = await agent.reservation_service.create(
                request.bench_id,
                owner,
                duration_seconds=request.reserve_duration_seconds,
                source=ReservationSource.WORKFLOW,
            )
            if isinstance(created, QueueEntry):  # pragma: no cover - queueing was not requested
                raise WorkflowReservationRequiredError(
                    "The workflow reservation was queued.", bench_id=request.bench_id
                )
            active = created
        if request.reservation_id is not None and (
            active is None or active.id != request.reservation_id
        ):
            raise WorkflowReservationRequiredError(
                "The requested active reservation does not exist for this bench.",
                bench_id=request.bench_id,
                reservation_id=str(request.reservation_id),
            )
        run = await agent.workflow_service.start(
            workflow_name,
            bench_id=request.bench_id,
            owner=owner,
            inputs=request.inputs,
        )
        if request.release_after:
            agent.release_reservation_after_workflow(run.id, run.reservation_id, owner)
        return run.model_dump(mode="json")

    @router.get("/workflow-runs/{workflow_run_id}")
    async def get_workflow_run(
        workflow_run_id: UUID,
        token: Annotated[ApiToken | None, Depends(authorize_operations)],
    ) -> dict[str, object]:
        run = await agent.workflow_service.get_run(workflow_run_id)
        _require_owner(run.owner, token)
        steps = await agent.workflow_service.list_step_results(workflow_run_id)
        return {
            **run.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
        }

    @router.post("/workflow-runs/{workflow_run_id}/cancel")
    async def cancel_workflow_run(
        workflow_run_id: UUID,
        request: OwnerRequest,
        token: Annotated[ApiToken | None, Depends(authorize_workflows)],
    ) -> object:
        owner = _authorized_owner(request.owner, token)
        run = await agent.workflow_service.cancel(workflow_run_id, owner)
        return run.model_dump(mode="json")

    return router


def _authorized_owner(owner: str, token: ApiToken | None) -> str:
    _require_owner(owner, token)
    return token.owner if token is not None else owner


def _require_owner(
    owner: str | None,
    token: ApiToken | None,
    *,
    allow_unspecified: bool = False,
) -> None:
    if token is None or (allow_unspecified and owner is None):
        return
    if owner != token.owner:
        raise PermissionDeniedError("The resource belongs to another API token owner.")
