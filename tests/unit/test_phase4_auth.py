from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.core.auth import ApiTokenService
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    InvalidApiTokenError,
    PermissionDeniedError,
)
from lab_platform.models import ApiToken, ApiTokenScope, EventRecord


class TokenRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, ApiToken] = {}

    async def create(self, token: ApiToken) -> ApiToken:
        self.records[token.id] = token
        return token

    async def get(self, token_id: UUID) -> ApiToken | None:
        return self.records.get(token_id)

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        return next((item for item in self.records.values() if item.token_hash == token_hash), None)

    async def list(self) -> list[ApiToken]:
        return sorted(self.records.values(), key=lambda item: item.name)

    async def update(self, token: ApiToken) -> ApiToken:
        self.records[token.id] = token
        return token

    async def update_last_used(self, token_id: UUID, used_at: datetime) -> ApiToken | None:
        token = self.records.get(token_id)
        if token is None or token.revoked_at is not None:
            return None
        if token.expires_at is not None and token.expires_at <= used_at:
            return None
        updated = token.model_copy(update={"last_used_at": used_at})
        self.records[token_id] = updated
        return updated


class Events:
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.records.append(event)
        return event


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 23, 10, tzinfo=UTC)


async def _token_plaintext_is_returned_once_and_only_hash_is_persisted(now: datetime) -> None:
    repository = TokenRepository()
    events = Events()
    service = ApiTokenService(
        repository,
        events,
        clock=lambda: now,
        token_factory=lambda: "lp_abcdefghijklmnopqrstuvwxyz0123456789",
    )

    issued = await service.issue(
        name="github",
        owner="github-actions",
        scopes={ApiTokenScope.CI_SESSIONS, "artifacts:write"},
        expires_at=now + timedelta(days=1),
    )

    assert issued.plaintext.startswith("lp_")
    assert issued.plaintext not in issued.record.model_dump_json()
    assert issued.record.token_hash == service.hash_token(issued.plaintext)
    assert [event.type for event in events.records] == ["API_TOKEN_CREATED"]
    authenticated = await service.authenticate(issued.plaintext, ["ci:sessions"])
    assert authenticated.last_used_at == now
    assert events.records[-1].type == "API_TOKEN_USED"


async def _authentication_rejects_missing_invalid_and_missing_scope(now: datetime) -> None:
    service = ApiTokenService(
        TokenRepository(),
        clock=lambda: now,
        token_factory=lambda: "lp_abcdefghijklmnopqrstuvwxyz0123456789",
    )
    issued = await service.issue(name="ci", owner="ci", scopes={"ci:sessions"})

    with pytest.raises(AuthenticationRequiredError):
        await service.authenticate(None)
    with pytest.raises(InvalidApiTokenError):
        await service.authenticate("not-the-token")
    with pytest.raises(PermissionDeniedError) as denied:
        await service.authenticate(issued.plaintext, ["artifacts:read"])
    assert denied.value.details == {"missing_scopes": ["artifacts:read"]}


async def _expired_and_revoked_tokens_are_invalid_and_revoke_is_idempotent(
    now: datetime,
) -> None:
    current = now
    repository = TokenRepository()
    plaintexts = iter(
        (
            "lp_abcdefghijklmnopqrstuvwxyz0123456789",
            "lp_0123456789abcdefghijklmnopqrstuvwxyz",
        )
    )
    service = ApiTokenService(
        repository,
        clock=lambda: current,
        token_factory=lambda: next(plaintexts),
    )
    issued = await service.issue(
        name="short",
        owner="ci",
        scopes={"ci:sessions"},
        expires_at=now + timedelta(seconds=1),
    )
    current += timedelta(seconds=1)
    with pytest.raises(InvalidApiTokenError, match="expired"):
        await service.authenticate(issued.plaintext)

    current = now
    live = await service.issue(name="live", owner="ci", scopes={"ci:sessions"})
    revoked = await service.revoke(live.record.id)
    assert await service.revoke(live.record.id) == revoked
    with pytest.raises(InvalidApiTokenError, match="revoked"):
        await service.authenticate(live.plaintext)
    with pytest.raises(InvalidApiTokenError, match="does not exist"):
        await service.revoke(UUID("00000000-0000-0000-0000-000000000000"))


async def _token_issue_validates_expiry_and_generator_entropy(now: datetime) -> None:
    service = ApiTokenService(TokenRepository(), clock=lambda: now, token_factory=lambda: "short")
    with pytest.raises(ValueError, match="entropy"):
        await service.issue(name="x", owner="x", scopes=[])

    service = ApiTokenService(
        TokenRepository(),
        clock=lambda: now,
        token_factory=lambda: "lp_abcdefghijklmnopqrstuvwxyz0123456789",
    )
    with pytest.raises(ValueError, match="future"):
        await service.issue(name="x", owner="x", scopes=[], expires_at=now)


def test_token_plaintext_is_returned_once_and_only_hash_is_persisted(now: datetime) -> None:
    asyncio.run(_token_plaintext_is_returned_once_and_only_hash_is_persisted(now))


def test_authentication_rejects_missing_invalid_and_missing_scope(now: datetime) -> None:
    asyncio.run(_authentication_rejects_missing_invalid_and_missing_scope(now))


def test_expired_and_revoked_tokens_are_invalid_and_revoke_is_idempotent(
    now: datetime,
) -> None:
    asyncio.run(_expired_and_revoked_tokens_are_invalid_and_revoke_is_idempotent(now))


def test_token_issue_validates_expiry_and_generator_entropy(now: datetime) -> None:
    asyncio.run(_token_issue_validates_expiry_and_generator_entropy(now))
