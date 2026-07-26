from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile, status
from fastapi.responses import FileResponse
from lab_platform.agent.api.auth import require_scopes
from lab_platform.core.errors import CiSessionConflictError, PermissionDeniedError
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    CiSessionStatus,
)

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


def create_artifact_router(agent: "LabAgent") -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["artifacts"])
    authorize_write = require_scopes(agent, ApiTokenScope.ARTIFACTS_WRITE)
    authorize_read = require_scopes(agent, ApiTokenScope.ARTIFACTS_READ)

    @router.post("/artifacts", status_code=status.HTTP_201_CREATED)
    async def upload_artifact(
        token: Annotated[ApiToken, Depends(authorize_write)],
        file: Annotated[UploadFile, File()],
        ci_session_id: Annotated[UUID, Form()],
        name: Annotated[str | None, Form()] = None,
        artifact_type: Annotated[str, Form()] = "firmware",
        sha256: Annotated[str | None, Form()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(ci_session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        if idempotency_key is not None:
            replay = await agent.artifact_service.get_by_idempotency_key(
                ci_session_id,
                idempotency_key,
            )
            if replay is not None:
                await file.close()
                return _public_artifact(replay)
        if session.status not in {
            CiSessionStatus.CREATED,
            CiSessionStatus.WAITING_FOR_BENCH,
            CiSessionStatus.RESERVED,
        }:
            raise CiSessionConflictError(
                "Artifacts can only be uploaded before a CI workflow starts.",
                ci_session_id=str(session.id),
                status=session.status.value,
            )

        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await file.read(1024 * 1024):
                yield chunk

        try:
            record = await agent.artifact_service.upload(
                chunks(),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=ci_session_id,
                name=name or file.filename or "artifact.bin",
                artifact_type=artifact_type,
                content_type=file.content_type,
                expected_sha256=sha256,
                idempotency_key=idempotency_key,
            )
        finally:
            await file.close()
        return _public_artifact(record)

    @router.get("/artifacts/{artifact_id}")
    async def artifact_metadata(
        artifact_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_read)],
    ) -> dict[str, object]:
        record = await agent.artifact_service.get(artifact_id)
        await _authorize_artifact(agent, record, token)
        return _public_artifact(record)

    @router.get(
        "/artifacts/{artifact_id}/content",
        response_class=FileResponse,
        responses={
            200: {
                "description": "Artifact file content.",
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def artifact_content(
        artifact_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_read)],
    ) -> FileResponse:
        record = await agent.artifact_service.get(artifact_id)
        await _authorize_artifact(agent, record, token)
        path = await agent.artifact_service.content_path(artifact_id)
        return FileResponse(
            path,
            media_type=record.content_type or "application/octet-stream",
            filename=record.name,
        )

    @router.get("/ci/sessions/{session_id}/artifacts")
    async def session_artifacts(
        session_id: UUID,
        token: Annotated[ApiToken, Depends(authorize_read)],
    ) -> dict[str, object]:
        session = await agent.ci_session_service.get(session_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        records = await agent.artifact_service.list_for_owner(
            ArtifactOwnerType.CI_SESSION,
            session_id,
        )
        return {"items": [_public_artifact(record) for record in records]}

    return router


async def _authorize_artifact(
    agent: "LabAgent",
    record: ArtifactRecord,
    token: ApiToken,
) -> None:
    if record.owner_type is ArtifactOwnerType.CI_SESSION:
        session = await agent.ci_session_service.get(record.owner_id, synchronize=False)
        _require_session_owner(session.requested_by, token)
        return
    if record.owner_type is ArtifactOwnerType.WORKFLOW_RUN:
        run = await agent.workflow_service.get_run(record.owner_id)
        if run.owner != token.owner:
            raise PermissionDeniedError("The workflow artifact belongs to another API token owner.")
        return
    raise PermissionDeniedError("This artifact is not available through the CI artifact API.")


def _require_session_owner(requested_by: str, token: ApiToken) -> None:
    if requested_by != token.owner:
        raise PermissionDeniedError("The CI session belongs to another API token owner.")


def _public_artifact(record: ArtifactRecord) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload.pop("path", None)
    return payload
