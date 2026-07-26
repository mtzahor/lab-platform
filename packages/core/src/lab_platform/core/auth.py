from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from lab_platform.core.errors import (
    AuthenticationRequiredError,
    InvalidApiTokenError,
    PermissionDeniedError,
)
from lab_platform.models import ApiToken, ApiTokenScope, EventRecord


class ApiTokenRepository(Protocol):
    async def create(self, token: ApiToken) -> ApiToken: ...

    async def get(self, token_id: UUID) -> ApiToken | None: ...

    async def get_by_hash(self, token_hash: str) -> ApiToken | None: ...

    async def list(self) -> list[ApiToken]: ...

    async def update(self, token: ApiToken) -> ApiToken: ...

    async def update_last_used(self, token_id: UUID, used_at: datetime) -> ApiToken | None: ...


class TokenEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...


@dataclass(frozen=True, slots=True)
class IssuedApiToken:
    record: ApiToken
    plaintext: str


class ApiTokenService:
    """Issue and validate high-entropy bearer tokens without retaining plaintext."""

    def __init__(
        self,
        repository: ApiTokenRepository,
        events: TokenEventRepository | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = lambda: f"lp_{secrets.token_urlsafe(32)}",
    ) -> None:
        self._repository = repository
        self._events = events
        self._clock = clock
        self._token_factory = token_factory

    async def issue(
        self,
        *,
        name: str,
        owner: str,
        scopes: Iterable[ApiTokenScope | str],
        expires_at: datetime | None = None,
    ) -> IssuedApiToken:
        plaintext = self._token_factory()
        if len(plaintext) < 32:
            raise ValueError("API token generator returned insufficient entropy")
        normalized_scopes = {ApiTokenScope(scope) for scope in scopes}
        now = self._clock()
        if expires_at is not None and expires_at <= now:
            raise ValueError("API token expiry must be in the future")
        record = ApiToken(
            id=uuid4(),
            name=name,
            token_hash=self.hash_token(plaintext),
            owner=owner,
            scopes=normalized_scopes,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
            last_used_at=None,
        )
        created = await self._repository.create(record)
        await self._emit(
            "API_TOKEN_CREATED",
            actor=owner,
            payload={
                "api_token_id": str(created.id),
                "name": created.name,
                "scopes": sorted(scope.value for scope in created.scopes),
            },
        )
        return IssuedApiToken(record=created, plaintext=plaintext)

    async def authenticate(
        self,
        plaintext: str | None,
        required_scopes: Iterable[ApiTokenScope | str] = (),
    ) -> ApiToken:
        if plaintext is None or not plaintext.strip():
            raise AuthenticationRequiredError("A bearer API token is required.")
        digest = self.hash_token(plaintext)
        token = await self._repository.get_by_hash(digest)
        if token is None or not hmac.compare_digest(token.token_hash, digest):
            raise InvalidApiTokenError("The bearer API token is invalid.")
        now = self._clock()
        if token.revoked_at is not None:
            raise InvalidApiTokenError("The bearer API token has been revoked.")
        if token.expires_at is not None and token.expires_at <= now:
            raise InvalidApiTokenError("The bearer API token has expired.")
        required = {ApiTokenScope(scope) for scope in required_scopes}
        missing = required.difference(token.scopes)
        if missing:
            raise PermissionDeniedError(
                "The bearer API token does not grant the required scopes.",
                missing_scopes=sorted(scope.value for scope in missing),
            )
        updated = await self._repository.update_last_used(token.id, now)
        if updated is None:
            raise InvalidApiTokenError(
                "The bearer API token became unavailable, expired, or revoked."
            )
        await self._emit(
            "API_TOKEN_USED",
            actor=updated.owner,
            payload={"api_token_id": str(updated.id)},
        )
        return updated

    async def list(self) -> list[ApiToken]:
        return await self._repository.list()

    async def revoke(self, token_id: UUID) -> ApiToken:
        token = await self._repository.get(token_id)
        if token is None:
            raise InvalidApiTokenError("The API token does not exist.", token_id=str(token_id))
        if token.revoked_at is not None:
            return token
        revoked = token.model_copy(update={"revoked_at": self._clock()})
        updated = await self._repository.update(revoked)
        await self._emit(
            "API_TOKEN_REVOKED",
            actor=updated.owner,
            payload={"api_token_id": str(updated.id)},
        )
        return updated

    @staticmethod
    def hash_token(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    async def _emit(self, event_type: str, *, actor: str, payload: dict[str, object]) -> None:
        if self._events is None:
            return
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type=event_type,
                source="authentication",
                actor=actor,
                payload=payload,
            )
        )
