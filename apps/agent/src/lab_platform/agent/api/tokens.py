from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from lab_platform.agent.api.auth import require_legacy_scopes
from lab_platform.core.errors import PermissionDeniedError
from lab_platform.models import ApiToken, ApiTokenScope
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent


# Agent administration scopes belong to the Phase 5 control-plane API.  Keep the
# local Agent's bootstrap/admin contract compatible with the Phase 4 scope set so
# adding control-plane-only scopes cannot invalidate existing administrators.
_LOCAL_AGENT_ADMIN_SCOPES = frozenset(
    scope
    for scope in ApiTokenScope
    if scope not in {ApiTokenScope.AGENTS_READ, ApiTokenScope.AGENTS_ADMIN}
)


class TokenApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateTokenRequest(TokenApiModel):
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    scopes: set[ApiTokenScope] = Field(min_length=1)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must include a timezone")
        return value


def create_token_router(agent: "LabAgent") -> APIRouter:
    router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"])
    authorize_admin = require_legacy_scopes(agent, *_LOCAL_AGENT_ADMIN_SCOPES)

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_token(
        request: CreateTokenRequest,
        administrator: Annotated[ApiToken | None, Depends(authorize_admin)],
    ) -> dict[str, object]:
        if administrator is None and not _LOCAL_AGENT_ADMIN_SCOPES.issubset(request.scopes):
            raise PermissionDeniedError(
                "The first bootstrap token must grant every supported scope."
            )
        issued = await agent.token_service.issue(
            name=request.name,
            owner=request.owner,
            scopes=request.scopes,
            expires_at=request.expires_at,
        )
        return {**_public_token(issued.record), "token": issued.plaintext}

    @router.get("")
    async def list_tokens(
        _administrator: Annotated[ApiToken | None, Depends(authorize_admin)],
    ) -> dict[str, object]:
        return {"items": [_public_token(token) for token in await agent.token_service.list()]}

    @router.post("/{token_id}/revoke")
    async def revoke_token(
        token_id: UUID,
        _administrator: Annotated[ApiToken | None, Depends(authorize_admin)],
    ) -> dict[str, object]:
        records = await agent.token_service.list()
        target = next((record for record in records if record.id == token_id), None)
        if target is not None and _is_active_administrator(target):
            remaining = [
                record
                for record in records
                if record.id != token_id and _is_active_administrator(record)
            ]
            if not remaining:
                raise PermissionDeniedError(
                    "The last active token administrator cannot be revoked."
                )
        return _public_token(await agent.token_service.revoke(token_id))

    return router


def _public_token(token: ApiToken) -> dict[str, object]:
    payload = token.model_dump(mode="json")
    payload.pop("token_hash", None)
    return payload


def _is_active_administrator(token: ApiToken) -> bool:
    now = datetime.now(UTC)
    return (
        token.revoked_at is None
        and (token.expires_at is None or token.expires_at > now)
        and _LOCAL_AGENT_ADMIN_SCOPES.issubset(token.scopes)
    )
