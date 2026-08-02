from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn, Protocol
from uuid import UUID, uuid4

from lab_platform.agent_protocol import negotiate_protocol_version
from lab_platform.control_plane_core.errors import (
    AgentAuthenticationFailedError,
    AgentEnrollmentTokenExpiredError,
    AgentEnrollmentTokenInvalidError,
    AgentEnrollmentTokenUsedError,
    AgentIncompatibleError,
    AgentNotFoundError,
)
from lab_platform.models import (
    AgentCredential,
    AgentCredentialKind,
    AgentEnrollmentToken,
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    EventRecord,
)
from pydantic import SecretStr

_MAX_SECRET_LENGTH = 512


class EnrollmentConsumeStatus(StrEnum):
    ENROLLED = "enrolled"
    REPLAYED = "replayed"
    INVALID = "invalid"
    EXPIRED = "expired"
    USED = "used"
    REVOKED = "revoked"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class EnrollmentConsumeResult:
    status: EnrollmentConsumeStatus
    token: AgentEnrollmentToken | None = None
    agent: AgentRecord | None = None
    credential: AgentCredential | None = None


class AgentEnrollmentRepository(Protocol):
    """Persistence boundary for atomic Agent identity operations.

    Implementations must commit each mutating operation and its audit event in one
    transaction. Authentication must verify Agent and credential state, compare the
    digest, and update ``last_used_at`` atomically.
    """

    async def create_token(
        self,
        token: AgentEnrollmentToken,
        audit_event: EventRecord,
    ) -> AgentEnrollmentToken: ...

    async def get_token(self, token_id: UUID) -> AgentEnrollmentToken | None: ...

    async def get_token_by_hash(self, token_hash: str) -> AgentEnrollmentToken | None: ...

    async def list_tokens(self, *, limit: int = 500) -> list[AgentEnrollmentToken]: ...

    async def revoke_token(
        self,
        token_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
    ) -> AgentEnrollmentToken | None: ...

    async def consume_token(
        self,
        *,
        token_hash: str,
        now: datetime,
        request_id: UUID,
        agent: AgentRecord,
        credential: AgentCredential,
        audit_event: EventRecord,
    ) -> EnrollmentConsumeResult: ...

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...

    async def list_agents(self, *, limit: int = 500) -> list[AgentRecord]: ...

    async def authenticate_agent(
        self,
        agent_id: UUID,
        credential_hash: str,
        used_at: datetime,
    ) -> tuple[AgentRecord, AgentCredential] | None: ...

    async def rotate_credential(
        self,
        *,
        agent_id: UUID,
        expected_credential_id: UUID,
        replacement: AgentCredential,
        rotated_at: datetime,
        audit_event: EventRecord,
    ) -> AgentCredential | None: ...

    async def revoke_agent(
        self,
        agent_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
    ) -> AgentRecord | None: ...


@dataclass(frozen=True, slots=True)
class EnrollmentTokenView:
    """Enrollment-token metadata safe to expose outside persistence."""

    id: UUID
    name: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    allowed_labels: dict[str, str]
    used_by_agent_id: UUID | None


@dataclass(frozen=True, slots=True)
class IssuedEnrollmentToken:
    token: EnrollmentTokenView
    plaintext: SecretStr


@dataclass(frozen=True, slots=True)
class EnrolledAgent:
    agent: AgentRecord
    credential_id: UUID
    credential_version: int
    plaintext: SecretStr


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    agent: AgentRecord
    credential_id: UUID
    credential_version: int
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedAgentCredential:
    agent_id: UUID
    credential_id: UUID
    credential_version: int
    plaintext: SecretStr


class AgentEnrollmentService:
    """Issue one-time enrollment tokens and control-plane-generated credentials.

    Plaintext enrollment tokens and Agent credentials are returned only at issuance.
    Persistence receives SHA-256 verifiers. A retry after successful enrollment is
    intentionally reported as ``TOKEN_USED`` because the original credential cannot
    be recovered without retaining secret material.
    """

    def __init__(
        self,
        repository: AgentEnrollmentRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = lambda: f"lpe_{secrets.token_urlsafe(32)}",
        credential_factory: Callable[[], str] = lambda: f"lpa_{secrets.token_urlsafe(32)}",
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._token_factory = token_factory
        self._credential_factory = credential_factory
        self._id_factory = id_factory

    async def issue_token(
        self,
        *,
        name: str,
        expires_at: datetime,
        allowed_labels: Mapping[str, str] | None = None,
    ) -> IssuedEnrollmentToken:
        now = self._now()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("Enrollment token expiry must be timezone-aware")
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= now:
            raise ValueError("Enrollment token expiry must be in the future")
        plaintext = self._token_factory()
        self._validate_generated_enrollment_secret(plaintext)
        record = AgentEnrollmentToken(
            id=self._id_factory(),
            name=name,
            token_hash=self.hash_secret(plaintext),
            created_at=now,
            expires_at=expires_at,
            allowed_labels=dict(allowed_labels or {}),
        )
        event = EventRecord(
            id=self._id_factory(),
            timestamp=now,
            type="AGENT_ENROLLMENT_TOKEN_CREATED",
            source="agent_registry",
            actor="control-plane-admin",
            payload={"enrollment_token_id": str(record.id), "name": record.name},
            deduplication_key=f"agent-enrollment-token-created:{record.id}",
        )
        created = await self._repository.create_token(record, event)
        return IssuedEnrollmentToken(
            token=_token_view(created),
            plaintext=SecretStr(plaintext),
        )

    async def revoke_token(self, token_id: UUID) -> EnrollmentTokenView:
        now = self._now()
        event = EventRecord(
            id=self._id_factory(),
            timestamp=now,
            type="AGENT_ENROLLMENT_TOKEN_REVOKED",
            source="agent_registry",
            actor="control-plane-admin",
            payload={"enrollment_token_id": str(token_id)},
            deduplication_key=f"agent-enrollment-token-revoked:{token_id}",
        )
        token = await self._repository.revoke_token(token_id, now, event)
        if token is None:
            raise AgentEnrollmentTokenInvalidError("The Agent enrollment token does not exist.")
        return _token_view(token)

    async def enroll(
        self,
        *,
        plaintext_token: str,
        request_id: UUID,
        agent_version: str,
        protocol_version: str,
        location: str | None = None,
    ) -> EnrolledAgent:
        if (
            not plaintext_token
            or not plaintext_token.strip()
            or len(plaintext_token) > _MAX_SECRET_LENGTH
        ):
            raise AgentEnrollmentTokenInvalidError("The Agent enrollment token is invalid.")
        try:
            negotiated_protocol_version = negotiate_protocol_version(protocol_version)
        except ValueError as exc:
            raise AgentIncompatibleError(
                "The Agent protocol version is incompatible.",
                protocol_version=protocol_version,
            ) from exc

        now = self._now()
        token_hash = self.hash_secret(plaintext_token)
        token = await self._repository.get_token_by_hash(token_hash)
        self._validate_token_state(token, now)
        assert token is not None

        credential_secret = self._credential_factory()
        self._validate_generated_agent_credential(credential_secret)
        agent_id = self._id_factory()
        agent = AgentRecord(
            id=agent_id,
            slug=_stable_agent_slug(token.name, agent_id),
            name=token.name,
            status=AgentStatus.OFFLINE,
            version=agent_version,
            protocol_version=negotiated_protocol_version,
            location=location,
            labels=token.allowed_labels,
            registered_at=now,
            enrollment_status=EnrollmentStatus.ENROLLED,
        )
        credential = AgentCredential(
            id=self._id_factory(),
            agent_id=agent.id,
            kind=AgentCredentialKind.OPAQUE_TOKEN,
            credential_hash=self.hash_secret(credential_secret),
            version=1,
            created_at=now,
        )
        event = EventRecord(
            id=self._id_factory(),
            timestamp=now,
            type="AGENT_ENROLLED",
            source="agent_registry",
            actor=agent.name,
            payload={
                "agent_id": str(agent.id),
                "enrollment_token_id": str(token.id),
                "enrollment_request_id": str(request_id),
            },
            deduplication_key=f"agent-enrolled:{request_id}",
        )
        result = await self._repository.consume_token(
            token_hash=token_hash,
            now=now,
            request_id=request_id,
            agent=agent,
            credential=credential,
            audit_event=event,
        )
        if result.status is EnrollmentConsumeStatus.ENROLLED:
            if result.agent is None or result.credential is None:
                raise RuntimeError("Enrollment repository returned an incomplete success result")
            return EnrolledAgent(
                agent=result.agent,
                credential_id=result.credential.id,
                credential_version=result.credential.version,
                plaintext=SecretStr(credential_secret),
            )
        self._raise_consumption_error(result.status)

    async def authenticate(
        self,
        agent_id: UUID,
        credential_secret: str | None,
    ) -> AuthenticatedAgent:
        if not self._is_valid_agent_credential(credential_secret):
            raise AgentAuthenticationFailedError("Agent authentication failed.")
        assert credential_secret is not None
        now = self._now()
        authenticated = await self._repository.authenticate_agent(
            agent_id,
            self.hash_secret(credential_secret),
            now,
        )
        if authenticated is None:
            raise AgentAuthenticationFailedError("Agent authentication failed.")
        agent, credential = authenticated
        if credential.last_used_at is None:
            raise RuntimeError("Agent repository did not record credential use")
        return AuthenticatedAgent(
            agent=agent,
            credential_id=credential.id,
            credential_version=credential.version,
            last_used_at=credential.last_used_at,
        )

    async def rotate_credential(
        self,
        agent_id: UUID,
        *,
        current_secret: str,
    ) -> IssuedAgentCredential:
        authenticated = await self.authenticate(agent_id, current_secret)
        replacement_secret = self._credential_factory()
        self._validate_generated_agent_credential(replacement_secret)
        if hmac.compare_digest(current_secret, replacement_secret):
            raise ValueError("Agent credential generator repeated the current credential")
        now = self._now()
        replacement = AgentCredential(
            id=self._id_factory(),
            agent_id=agent_id,
            kind=AgentCredentialKind.OPAQUE_TOKEN,
            credential_hash=self.hash_secret(replacement_secret),
            version=authenticated.credential_version + 1,
            created_at=now,
        )
        event = EventRecord(
            id=self._id_factory(),
            timestamp=now,
            type="AGENT_CREDENTIAL_ROTATED",
            source="agent_registry",
            actor=str(agent_id),
            payload={"agent_id": str(agent_id), "credential_version": replacement.version},
            deduplication_key=f"agent-credential-rotated:{replacement.id}",
        )
        rotated = await self._repository.rotate_credential(
            agent_id=agent_id,
            expected_credential_id=authenticated.credential_id,
            replacement=replacement,
            rotated_at=now,
            audit_event=event,
        )
        if rotated is None:
            raise AgentAuthenticationFailedError("Agent credential rotation failed.")
        return IssuedAgentCredential(
            agent_id=agent_id,
            credential_id=rotated.id,
            credential_version=rotated.version,
            plaintext=SecretStr(replacement_secret),
        )

    async def revoke_agent(self, agent_id: UUID) -> AgentRecord:
        now = self._now()
        event = EventRecord(
            id=self._id_factory(),
            timestamp=now,
            type="AGENT_REVOKED",
            source="agent_registry",
            actor="control-plane-admin",
            payload={"agent_id": str(agent_id)},
            deduplication_key=f"agent-revoked:{agent_id}",
        )
        agent = await self._repository.revoke_agent(agent_id, now, event)
        if agent is None:
            raise AgentNotFoundError("The Agent does not exist.", agent_id=str(agent_id))
        return agent

    async def list_agents(self) -> list[AgentRecord]:
        return await self._repository.list_agents()

    async def list_tokens(self) -> list[EnrollmentTokenView]:
        return [_token_view(token) for token in await self._repository.list_tokens()]

    @staticmethod
    def hash_secret(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Agent enrollment clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_generated_enrollment_secret(value: str) -> None:
        if not value.startswith("lpe_") or len(value) < 47 or len(value) > _MAX_SECRET_LENGTH:
            raise ValueError("Enrollment token generator returned insufficient entropy")

    @staticmethod
    def _validate_generated_agent_credential(value: str) -> None:
        if not AgentEnrollmentService._is_valid_agent_credential(value):
            raise ValueError("Agent credential generator returned insufficient entropy")

    @staticmethod
    def _is_valid_agent_credential(value: str | None) -> bool:
        return (
            value is not None
            and value.startswith("lpa_")
            and 47 <= len(value) <= _MAX_SECRET_LENGTH
        )

    @staticmethod
    def _validate_token_state(
        token: AgentEnrollmentToken | None,
        now: datetime,
    ) -> None:
        if token is None or token.revoked_at is not None:
            raise AgentEnrollmentTokenInvalidError("The Agent enrollment token is invalid.")
        if token.used_at is not None:
            raise AgentEnrollmentTokenUsedError("The Agent enrollment token has already been used.")
        if token.expires_at <= now:
            raise AgentEnrollmentTokenExpiredError("The Agent enrollment token has expired.")

    @staticmethod
    def _raise_consumption_error(status: EnrollmentConsumeStatus) -> NoReturn:
        if status is EnrollmentConsumeStatus.EXPIRED:
            raise AgentEnrollmentTokenExpiredError("The Agent enrollment token has expired.")
        if status in {EnrollmentConsumeStatus.REPLAYED, EnrollmentConsumeStatus.USED}:
            raise AgentEnrollmentTokenUsedError("The Agent enrollment token has already been used.")
        raise AgentEnrollmentTokenInvalidError("The Agent enrollment token is invalid.")


def _token_view(token: AgentEnrollmentToken) -> EnrollmentTokenView:
    return EnrollmentTokenView(
        id=token.id,
        name=token.name,
        created_at=token.created_at,
        expires_at=token.expires_at,
        used_at=token.used_at,
        revoked_at=token.revoked_at,
        allowed_labels=dict(token.allowed_labels),
        used_by_agent_id=token.used_by_agent_id,
    )


def _stable_agent_slug(name: str, agent_id: UUID) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "agent"
    return f"{base[:54].rstrip('-')}-{agent_id.hex}"
