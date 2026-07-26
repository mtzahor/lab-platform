from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from lab_platform.agent.api.auth import require_scopes
from lab_platform.core.errors import PermissionDeniedError
from lab_platform.core.results import build_test_results, render_junit_xml
from lab_platform.models import ApiToken, ApiTokenScope

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


def create_results_router(agent: "LabAgent") -> APIRouter:
    router = APIRouter(prefix="/api/v1/workflow-runs", tags=["workflow-results"])
    authorize = require_scopes(agent, ApiTokenScope.OPERATIONS_READ)

    @router.get("/{workflow_run_id}/results")
    async def workflow_results(
        workflow_run_id: UUID,
        token: Annotated[ApiToken, Depends(authorize)],
    ) -> dict[str, object]:
        run = await agent.workflow_service.get_run(workflow_run_id)
        _require_workflow_owner(run.owner, token)
        steps = await agent.workflow_service.list_step_results(workflow_run_id)
        results = build_test_results(steps)
        return {
            "workflow_run_id": str(run.id),
            "workflow_name": run.workflow_name,
            "status": run.status.value,
            "results": [item.model_dump(mode="json") for item in results],
        }

    @router.get(
        "/{workflow_run_id}/results/junit",
        response_class=Response,
        responses={
            200: {
                "description": "JUnit XML test report.",
                "content": {"application/xml": {"schema": {"type": "string"}}},
            }
        },
    )
    async def workflow_results_junit(
        workflow_run_id: UUID,
        token: Annotated[ApiToken, Depends(authorize)],
    ) -> Response:
        run = await agent.workflow_service.get_run(workflow_run_id)
        _require_workflow_owner(run.owner, token)
        steps = await agent.workflow_service.list_step_results(workflow_run_id)
        xml = render_junit_xml(run.workflow_name, build_test_results(steps))
        return Response(content=xml, media_type="application/xml")

    return router


def _require_workflow_owner(owner: str, token: ApiToken) -> None:
    if owner != token.owner:
        raise PermissionDeniedError("The workflow run belongs to another API token owner.")
