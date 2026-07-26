from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from lab_platform.agent.api.artifacts import _require_session_owner
from lab_platform.agent.api.auth import require_scopes
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    BenchRequest,
    CiProvider,
)
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


class CiApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateCiSessionRequest(CiApiModel):
    provider: CiProvider = CiProvider.UNKNOWN
    external_run_id: str = Field(min_length=1, max_length=500)
    repository: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    actor: str | None = None
    bench_request: BenchRequest = Field(default_factory=BenchRequest)


class RunCiWorkflowRequest(CiApiModel):
    workflow_name: str = Field(min_length=1, max_length=500)
    version: int | None = Field(default=None, ge=1)
    inputs: dict[str, object] = Field(default_factory=dict)


def create_ci_router(agent: "LabAgent") -> APIRouter:
    router = APIRouter(prefix="/api/v1/ci", tags=["ci-sessions"])
    authorize_create = require_scopes(
        agent,
        ApiTokenScope.CI_SESSIONS,
        ApiTokenScope.BENCHES_READ,
        ApiTokenScope.RESERVATIONS_WRITE,
    )
    authorize_session = require_scopes(agent, ApiTokenScope.CI_SESSIONS)
    authorize_run = require_scopes(
        agent,
        ApiTokenScope.CI_SESSIONS,
        ApiTokenScope.WORKFLOWS_RUN,
    )

    @router.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: CreateCiSessionRequest,
        token: Annotated[ApiToken, Depends(authorize_create)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        bench_request = request.bench_request
        default_updates: dict[str, object] = {}
        if "maximum_wait_seconds" not in bench_request.model_fields_set:
            default_updates["maximum_wait_seconds"] = agent.config.ci.bench_wait_timeout_seconds
        if "reservation_duration_seconds" not in bench_request.model_fields_set:
            default_updates["reservation_duration_seconds"] = (
                agent.config.ci.default_reservation_minutes * 60
            )
        if default_updates:
            bench_request = bench_request.model_copy(update=default_updates)
        session = await agent.ci_session_service.create(
            provider=request.provider,
            external_run_id=request.external_run_id,
            repository=request.repository,
            ref=request.ref,
            commit_sha=request.commit_sha,
            actor=request.actor,
            requested_by=token.owner,
            bench_request=bench_request,
            idempotency_key=idempotency_key,
        )
        return await agent.ci_session_service.details(session.id)

    @router.get("/sessions/{session_id}")
    async def get_session(
        session_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_session)],
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id)
        _require_session_owner(session.requested_by, token)
        return await agent.ci_session_service.details(session_id)

    @router.post("/sessions/{session_id}/heartbeat")
    async def heartbeat_session(
        session_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_session)],
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        updated = await agent.ci_session_service.heartbeat(session_id)
        return updated.model_dump(mode="json")

    @router.post("/sessions/{session_id}/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_session_workflow(
        session_id: UUID,
        request: RunCiWorkflowRequest,
        token: Annotated[ApiToken, Depends(authorize_run)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        started = await agent.ci_session_service.start_workflow(
            session_id,
            workflow_name=request.workflow_name,
            version=request.version,
            inputs=request.inputs,
            idempotency_key=idempotency_key,
        )
        return started.model_dump(mode="json")

    @router.post("/sessions/{session_id}/cancel")
    async def cancel_session(
        session_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_session)],
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        cancelled = await agent.ci_session_service.cancel(session_id)
        return await agent.ci_session_service.details(cancelled.id)

    @router.post("/sessions/{session_id}/finalize")
    async def finalize_session(
        session_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_session)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        finalized = await agent.ci_session_service.finalize(
            session_id,
            idempotency_key=idempotency_key,
        )
        return await agent.ci_session_service.details(finalized.id)

    return router
