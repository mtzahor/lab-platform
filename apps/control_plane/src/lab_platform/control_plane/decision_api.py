from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeAlias
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from lab_platform.core.decision_engine.serializer import serialize_operation
from lab_platform.models import (
    ApiToken,
    AuthenticationContext,
    DistributedOperationStatus,
    RemoteCommandType,
)
from lab_platform.models.decisions import DecisionRecord, FeedbackRecord, OperatorFeedback

if TYPE_CHECKING:
    from lab_platform.control_plane.runtime import ControlPlaneRuntime

Actor: TypeAlias = ApiToken | AuthenticationContext


def public_decision(record: DecisionRecord) -> dict[str, Any]:
    decision = record.decision
    return {
        "id": str(record.id),
        "run_id": str(record.run_id),
        "timestamp": record.timestamp.isoformat(),
        "status": record.status,
        "diagnosis": decision.classification if decision else None,
        "recommended_action": decision.recommended_action if decision else None,
        "severity": decision.severity if decision else None,
        "retry_safe_probability": decision.retry_safe_probability if decision else None,
        "confidence": decision.confidence.model_dump() if decision else None,
        "policy": record.policy,
        "policy_reason": record.policy_reason,
        "provider": decision.provider if decision else record.provider,
        "model": decision.model if decision else record.requested_model,
        "schema_version": record.schema_version,
        "evidence": record.state.text if record.state else None,
        "state_hash": record.state.sha256 if record.state else None,
        "error_code": record.error_code,
    }


def create_decision_router(
    runtime: ControlPlaneRuntime,
    *,
    read_auth: Callable[..., object],
    write_auth: Callable[..., object],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workflow-runs", tags=["decision-engine"])
    read_dependency, write_dependency = Depends(read_auth), Depends(write_auth)

    async def operation_for(operation_id: UUID, actor: Actor | None) -> Any:
        operation = await runtime.operational_access.get_operation(
            operation_id,
            authentication_context=actor if isinstance(actor, AuthenticationContext) else None,
            allow_legacy_authorisation=isinstance(actor, ApiToken),
        )
        command = await runtime.command_records.get(
            operation.remote_command_id, organisation_id=operation.organisation_id
        )
        if command is None or command.command_type is not RemoteCommandType.RUN_WORKFLOW:
            raise HTTPException(404, "Workflow run not found")
        return operation

    def require_recommendations() -> None:
        if not runtime.decision_settings.enabled or runtime.decision_settings.mode != "recommend":
            raise HTTPException(404, "Decision engine is unavailable")

    @router.get("/{operation_id:uuid}/diagnoses")
    async def list_diagnoses(
        operation_id: UUID, token: Actor | None = read_dependency
    ) -> dict[str, Any]:
        operation = await operation_for(operation_id, token)
        if not runtime.decision_settings.enabled or runtime.decision_settings.mode != "recommend":
            return {"enabled": False, "items": []}
        records = await runtime.decision_repository.list_for_run(
            operation.id, operation.organisation_id
        )
        return {
            "enabled": True,
            "items": [public_decision(r) for r in records if r.mode == "recommend"],
        }

    @router.post("/{operation_id:uuid}/diagnose")
    async def diagnose_run(
        operation_id: UUID, token: Actor | None = write_dependency
    ) -> dict[str, Any]:
        operation = await operation_for(operation_id, token)
        require_recommendations()
        if operation.status not in {
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }:
            raise HTTPException(409, "Diagnosis requires a completed run")
        record = await runtime.decision_service.diagnose(
            operation.id,
            operation.organisation_id,
            lambda: serialize_operation(
                operation, max_bytes=runtime.decision_settings.max_state_bytes
            ),
        )
        return public_decision(record)

    @router.post("/{operation_id:uuid}/diagnoses/{decision_id:uuid}/feedback", status_code=201)
    async def record_feedback(
        operation_id: UUID,
        decision_id: UUID,
        body: OperatorFeedback,
        token: Actor | None = write_dependency,
    ) -> dict[str, Any]:
        operation = await operation_for(operation_id, token)
        require_recommendations()
        record = await runtime.decision_repository.get(
            decision_id, operation.id, operation.organisation_id
        )
        if record is None or record.mode != "recommend" or record.status != "available":
            raise HTTPException(404, "Decision not found")
        actor_id = (
            str(token.principal.id)
            if isinstance(token, AuthenticationContext)
            else str(token.id)
            if token
            else "legacy"
        )
        feedback = FeedbackRecord(
            decision_id=decision_id,
            run_id=operation.id,
            organisation_id=operation.organisation_id,
            actor_id=actor_id,
            feedback=body,
        )
        await runtime.decision_repository.save_feedback(feedback)
        return {"id": str(feedback.id), "outcome": body.outcome}

    return router
