from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.control_plane_core import (
    AgentAuthenticationFailedError,
    AgentEnrollmentService,
    AgentEnrollmentTokenExpiredError,
    AgentEnrollmentTokenInvalidError,
    AgentEnrollmentTokenUsedError,
    AgentIncompatibleError,
    AgentNotFoundError,
    EnrolledAgent,
    EnrollmentConsumeResult,
    EnrollmentConsumeStatus,
    EnrollmentTokenView,
)
from lab_platform.models import (
    AgentCredential,
    AgentEnrollmentToken,
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    EventRecord,
)

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
ENROLLMENT_SECRET = f"lpe_{'e' * 48}"
AGENT_SECRET = f"lpa_{'a' * 48}"
REPLACEMENT_SECRET = f"lpa_{'b' * 48}"
DEFAULT_REQUEST_ID = UUID(int=10_000)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class IdFactory:
    def __init__(self) -> None:
        self._next = 1

    def __call__(self) -> UUID:
        value = UUID(int=self._next)
        self._next += 1
        return value


class CredentialFactory:
    def __init__(self, *values: str) -> None:
        self._values = values or (AGENT_SECRET, REPLACEMENT_SECRET)
        self.calls = 0

    def __call__(self) -> str:
        value = self._values[min(self.calls, len(self._values) - 1)]
        self.calls += 1
        return value


class MemoryEnrollmentRepository:
    """Small atomic repository double matching the Phase 5 enrollment port."""

    def __init__(self) -> None:
        self.tokens: dict[UUID, AgentEnrollmentToken] = {}
        self.token_ids_by_hash: dict[str, UUID] = {}
        self.agents: dict[UUID, AgentRecord] = {}
        self.credentials: dict[UUID, AgentCredential] = {}
        self.enrollment_request_ids: set[UUID] = set()
        self.events: list[EventRecord] = []

    async def create_token(
        self,
        token: AgentEnrollmentToken,
        audit_event: EventRecord,
    ) -> AgentEnrollmentToken:
        if token.token_hash in self.token_ids_by_hash:
            raise ValueError("duplicate enrollment token hash")
        self.tokens[token.id] = token
        self.token_ids_by_hash[token.token_hash] = token.id
        self.events.append(audit_event)
        return token

    async def get_token(self, token_id: UUID) -> AgentEnrollmentToken | None:
        return self.tokens.get(token_id)

    async def get_token_by_hash(self, token_hash: str) -> AgentEnrollmentToken | None:
        token_id = self.token_ids_by_hash.get(token_hash)
        return self.tokens.get(token_id) if token_id is not None else None

    async def list_tokens(self, *, limit: int = 500) -> list[AgentEnrollmentToken]:
        return sorted(
            self.tokens.values(),
            key=lambda token: (token.created_at, str(token.id)),
            reverse=True,
        )[:limit]

    async def revoke_token(
        self,
        token_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
    ) -> AgentEnrollmentToken | None:
        token = self.tokens.get(token_id)
        if token is None:
            return None
        if token.revoked_at is not None:
            return token
        revoked = token.model_copy(update={"revoked_at": revoked_at})
        self.tokens[token_id] = revoked
        self.events.append(audit_event)
        return revoked

    async def consume_token(
        self,
        *,
        token_hash: str,
        now: datetime,
        request_id: UUID,
        agent: AgentRecord,
        credential: AgentCredential,
        audit_event: EventRecord,
    ) -> EnrollmentConsumeResult:
        token = await self.get_token_by_hash(token_hash)
        if token is None:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.INVALID)
        if token.revoked_at is not None:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.REVOKED, token=token)
        if token.used_at is not None:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.USED, token=token)
        if token.expires_at <= now:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.EXPIRED, token=token)
        if agent.id in self.agents or credential.id in self.credentials:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.CONFLICT, token=token)

        used = token.model_copy(
            update={
                "used_at": now,
                "used_by_agent_id": agent.id,
                "enrollment_request_id": request_id,
            }
        )
        self.tokens[token.id] = used
        self.agents[agent.id] = agent
        self.credentials[credential.id] = credential
        self.enrollment_request_ids.add(request_id)
        self.events.append(audit_event)
        return EnrollmentConsumeResult(
            status=EnrollmentConsumeStatus.ENROLLED,
            token=used,
            agent=agent,
            credential=credential,
        )

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self.agents.get(agent_id)

    async def list_agents(self, *, limit: int = 500) -> list[AgentRecord]:
        return sorted(
            self.agents.values(),
            key=lambda agent: (agent.registered_at, str(agent.id)),
            reverse=True,
        )[:limit]

    async def get_active_credential(self, agent_id: UUID) -> AgentCredential | None:
        active = [
            credential
            for credential in self.credentials.values()
            if credential.agent_id == agent_id and credential.revoked_at is None
        ]
        return max(active, key=lambda credential: credential.version, default=None)

    async def authenticate_agent(
        self,
        agent_id: UUID,
        credential_hash: str,
        used_at: datetime,
    ) -> tuple[AgentRecord, AgentCredential] | None:
        agent = self.agents.get(agent_id)
        credential = await self.get_active_credential(agent_id)
        if (
            agent is None
            or agent.status is AgentStatus.REVOKED
            or agent.enrollment_status is EnrollmentStatus.REVOKED
            or used_at < agent.registered_at
            or credential is None
            or credential.revoked_at is not None
            or credential.credential_hash != credential_hash
            or credential.expires_at is not None
            and credential.expires_at <= used_at
        ):
            return None
        previous_use = credential.last_used_at
        effective_use = used_at if previous_use is None or used_at > previous_use else previous_use
        updated = credential.model_copy(update={"last_used_at": effective_use})
        self.credentials[credential.id] = updated
        return agent, updated

    async def rotate_credential(
        self,
        *,
        agent_id: UUID,
        expected_credential_id: UUID,
        replacement: AgentCredential,
        rotated_at: datetime,
        audit_event: EventRecord,
    ) -> AgentCredential | None:
        agent = self.agents.get(agent_id)
        current = await self.get_active_credential(agent_id)
        if (
            agent is None
            or agent.status is AgentStatus.REVOKED
            or current is None
            or current.id != expected_credential_id
            or replacement.id in self.credentials
        ):
            return None
        self.credentials[current.id] = current.model_copy(update={"revoked_at": rotated_at})
        self.credentials[replacement.id] = replacement
        self.events.append(audit_event)
        return replacement

    async def revoke_agent(
        self,
        agent_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
    ) -> AgentRecord | None:
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        if agent.status is AgentStatus.REVOKED:
            return agent
        revoked = agent.model_copy(
            update={
                "status": AgentStatus.REVOKED,
                "enrollment_status": EnrollmentStatus.REVOKED,
                "revoked_at": revoked_at,
            }
        )
        self.agents[agent_id] = revoked
        for credential_id, credential in tuple(self.credentials.items()):
            if credential.agent_id == agent_id and credential.revoked_at is None:
                self.credentials[credential_id] = credential.model_copy(
                    update={"revoked_at": revoked_at}
                )
        self.events.append(audit_event)
        return revoked


def _service(
    repository: MemoryEnrollmentRepository | None = None,
    clock: MutableClock | None = None,
    *,
    token: str = ENROLLMENT_SECRET,
    credentials: tuple[str, ...] = (AGENT_SECRET, REPLACEMENT_SECRET),
) -> tuple[AgentEnrollmentService, MemoryEnrollmentRepository, MutableClock]:
    resolved_repository = repository or MemoryEnrollmentRepository()
    resolved_clock = clock or MutableClock()
    return (
        AgentEnrollmentService(
            resolved_repository,
            clock=resolved_clock,
            token_factory=lambda: token,
            credential_factory=CredentialFactory(*credentials),
            id_factory=IdFactory(),
        ),
        resolved_repository,
        resolved_clock,
    )


async def _issue_and_enroll(
    service: AgentEnrollmentService,
    *,
    request_id: UUID = DEFAULT_REQUEST_ID,
) -> tuple[EnrollmentTokenView, EnrolledAgent]:
    issued = await service.issue_token(
        name="home-lab",
        expires_at=NOW + timedelta(minutes=30),
        allowed_labels={"environment": "development", "owner": "michael"},
    )
    enrolled = await service.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=request_id,
        agent_version="0.6.0-alpha",
        protocol_version="1.0",
        location="jerusalem-home",
    )
    return issued.token, enrolled


def test_enrollment_token_plaintext_is_one_time_and_repr_safe() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        issued = await service.issue_token(
            name="home-lab",
            expires_at=NOW + timedelta(minutes=30),
            allowed_labels={"environment": "development"},
        )

        assert issued.plaintext.get_secret_value() == ENROLLMENT_SECRET
        assert not hasattr(issued.token, "token_hash")
        stored = repository.tokens[issued.token.id]
        assert stored.token_hash == service.hash_secret(ENROLLMENT_SECRET)
        assert ENROLLMENT_SECRET not in stored.model_dump_json()
        assert ENROLLMENT_SECRET not in repr(issued)
        assert all(ENROLLMENT_SECRET not in event.model_dump_json() for event in repository.events)

        listed = await service.list_tokens()
        assert listed == [issued.token]
        assert all(not hasattr(token, "token_hash") for token in listed)

    asyncio.run(scenario())


def test_issue_token_requires_future_aware_expiry_and_high_entropy_secret() -> None:
    async def scenario() -> None:
        service, _repository, _clock = _service()
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.issue_token(
                name="home-lab",
                expires_at=(NOW + timedelta(minutes=1)).replace(tzinfo=None),
            )
        with pytest.raises(ValueError, match="future"):
            await service.issue_token(name="home-lab", expires_at=NOW)

        weak, _repository, _clock = _service(token="lpe_short")
        with pytest.raises(ValueError, match="entropy"):
            await weak.issue_token(
                name="home-lab",
                expires_at=NOW + timedelta(minutes=1),
            )
        oversized, _repository, _clock = _service(token=f"lpe_{'x' * 600}")
        with pytest.raises(ValueError, match="entropy"):
            await oversized.issue_token(
                name="home-lab",
                expires_at=NOW + timedelta(minutes=1),
            )

        weak_credential, _repository, _clock = _service(credentials=("lpa_short",))
        issued = await weak_credential.issue_token(
            name="home-lab",
            expires_at=NOW + timedelta(minutes=1),
        )
        with pytest.raises(ValueError, match="insufficient entropy"):
            await weak_credential.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=UUID(int=9_001),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

    asyncio.run(scenario())


def test_successful_enrollment_pins_labels_and_starts_offline() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        token, enrolled = await _issue_and_enroll(service)

        assert enrolled.agent.status is AgentStatus.OFFLINE
        assert enrolled.agent.enrollment_status is EnrollmentStatus.ENROLLED
        assert enrolled.agent.name == token.name == "home-lab"
        assert (
            enrolled.agent.labels
            == token.allowed_labels
            == {
                "environment": "development",
                "owner": "michael",
            }
        )
        assert enrolled.agent.location == "jerusalem-home"
        assert enrolled.agent.version == "0.6.0-alpha"
        assert enrolled.agent.protocol_version == "1.0"
        assert enrolled.agent.slug.endswith(enrolled.agent.id.hex)
        assert enrolled.credential_version == 1
        assert enrolled.plaintext.get_secret_value() == AGENT_SECRET
        assert not hasattr(enrolled, "credential_hash")
        assert AGENT_SECRET not in repr(enrolled)
        credential = repository.credentials[enrolled.credential_id]
        assert credential.agent_id == enrolled.agent.id
        assert credential.version == 1
        assert credential.credential_hash == service.hash_secret(AGENT_SECRET)
        assert AGENT_SECRET not in credential.model_dump_json()
        assert repository.tokens[token.id].used_at == NOW

    asyncio.run(scenario())


def test_same_enrollment_request_cannot_recover_the_original_plaintext() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        request_id = UUID(int=20_000)
        issued = await service.issue_token(
            name="home-lab",
            expires_at=NOW + timedelta(minutes=30),
        )
        first = await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=request_id,
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        with pytest.raises(AgentEnrollmentTokenUsedError):
            await service.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=request_id,
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

        assert first.plaintext.get_secret_value() == AGENT_SECRET
        assert len(repository.agents) == 1
        assert len(repository.credentials) == 1
        assert [event.type for event in repository.events].count("AGENT_ENROLLED") == 1

    asyncio.run(scenario())


def test_enrollment_reports_used_revoked_expired_and_invalid_tokens() -> None:
    async def scenario() -> None:
        service, _repository, clock = _service()
        issued = await service.issue_token(
            name="used",
            expires_at=NOW + timedelta(minutes=30),
        )
        await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=30_001),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        with pytest.raises(AgentEnrollmentTokenUsedError):
            await service.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=UUID(int=30_002),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

        revoked_service, _repository, _clock = _service()
        revoked = await revoked_service.issue_token(
            name="revoked",
            expires_at=NOW + timedelta(minutes=30),
        )
        revoked_view = await revoked_service.revoke_token(revoked.token.id)
        assert revoked_view.revoked_at == NOW
        assert not hasattr(revoked_view, "token_hash")
        with pytest.raises(AgentEnrollmentTokenInvalidError):
            await revoked_service.enroll(
                plaintext_token=revoked.plaintext.get_secret_value(),
                request_id=UUID(int=30_003),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

        expiring_service, _repository, expiring_clock = _service()
        expiring = await expiring_service.issue_token(
            name="expiring",
            expires_at=NOW + timedelta(seconds=1),
        )
        expiring_clock.value = NOW + timedelta(seconds=1)
        with pytest.raises(AgentEnrollmentTokenExpiredError):
            await expiring_service.enroll(
                plaintext_token=expiring.plaintext.get_secret_value(),
                request_id=UUID(int=30_004),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

        clock.value = NOW
        with pytest.raises(AgentEnrollmentTokenInvalidError):
            await service.enroll(
                plaintext_token=f"lpe_{'x' * 40}",
                request_id=UUID(int=30_005),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )
        with pytest.raises(AgentEnrollmentTokenInvalidError):
            await service.enroll(
                plaintext_token=f"lpe_{'x' * 600}",
                request_id=UUID(int=30_006),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )

    asyncio.run(scenario())


def test_enrollment_rejects_incompatible_protocol_before_consuming_token() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        issued = await service.issue_token(
            name="home-lab",
            expires_at=NOW + timedelta(minutes=30),
        )
        with pytest.raises(AgentIncompatibleError) as incompatible:
            await service.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=UUID(int=40_000),
                agent_version="0.6.0-alpha",
                protocol_version="2.0",
            )

        assert incompatible.value.code == "AGENT_INCOMPATIBLE"
        assert repository.tokens[issued.token.id].used_at is None

    asyncio.run(scenario())


def test_agent_authentication_has_generic_failures_and_updates_last_use() -> None:
    async def scenario() -> None:
        service, repository, clock = _service()
        _token, enrolled = await _issue_and_enroll(service)

        for agent_id, secret in (
            (enrolled.agent.id, None),
            (UUID(int=99_999), AGENT_SECRET),
            (enrolled.agent.id, f"lpa_{'z' * 40}"),
            (enrolled.agent.id, f"lpa_{'z' * 600}"),
        ):
            with pytest.raises(AgentAuthenticationFailedError) as failure:
                await service.authenticate(agent_id, secret)
            assert failure.value.code == "AGENT_AUTHENTICATION_FAILED"
            assert failure.value.details == {}

        clock.value = NOW + timedelta(seconds=5)
        authenticated = await service.authenticate(enrolled.agent.id, AGENT_SECRET)
        assert authenticated.agent == enrolled.agent
        assert authenticated.credential_id == enrolled.credential_id
        assert authenticated.credential_version == 1
        assert authenticated.last_used_at == clock.value
        assert not hasattr(authenticated, "credential_hash")
        assert repository.credentials[authenticated.credential_id].last_used_at == clock.value

    asyncio.run(scenario())


def test_credential_rotation_invalidates_old_secret_and_increments_version() -> None:
    async def scenario() -> None:
        service, repository, clock = _service()
        _token, enrolled = await _issue_and_enroll(service)
        clock.value = NOW + timedelta(minutes=1)

        replacement = await service.rotate_credential(
            enrolled.agent.id,
            current_secret=AGENT_SECRET,
        )

        assert replacement.agent_id == enrolled.agent.id
        assert replacement.credential_version == 2
        assert replacement.plaintext.get_secret_value() == REPLACEMENT_SECRET
        assert not hasattr(replacement, "credential_hash")
        assert REPLACEMENT_SECRET not in repr(replacement)
        stored_replacement = repository.credentials[replacement.credential_id]
        assert stored_replacement.credential_hash == service.hash_secret(REPLACEMENT_SECRET)
        assert repository.credentials[enrolled.credential_id].revoked_at == clock.value
        with pytest.raises(AgentAuthenticationFailedError):
            await service.authenticate(enrolled.agent.id, AGENT_SECRET)
        authenticated = await service.authenticate(enrolled.agent.id, REPLACEMENT_SECRET)
        assert authenticated.credential_id == replacement.credential_id

    asyncio.run(scenario())


def test_agent_revocation_is_idempotent_and_invalidates_authentication() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        _token, enrolled = await _issue_and_enroll(service)

        revoked = await service.revoke_agent(enrolled.agent.id)
        repeated = await service.revoke_agent(enrolled.agent.id)

        assert repeated == revoked
        assert revoked.status is AgentStatus.REVOKED
        assert revoked.enrollment_status is EnrollmentStatus.REVOKED
        assert [event.type for event in repository.events].count("AGENT_REVOKED") == 1
        assert all(
            credential.revoked_at is not None
            for credential in repository.credentials.values()
            if credential.agent_id == enrolled.agent.id
        )
        with pytest.raises(AgentAuthenticationFailedError):
            await service.authenticate(enrolled.agent.id, AGENT_SECRET)
        with pytest.raises(AgentNotFoundError):
            await service.revoke_agent(UUID(int=88_888))

    asyncio.run(scenario())


def test_enrollment_audit_events_do_not_contain_secret_material() -> None:
    async def scenario() -> None:
        service, repository, _clock = _service()
        _token, enrolled = await _issue_and_enroll(service)
        await service.authenticate(enrolled.agent.id, AGENT_SECRET)
        await service.rotate_credential(
            enrolled.agent.id,
            current_secret=AGENT_SECRET,
        )
        await service.revoke_agent(enrolled.agent.id)

        serialized_events = "\n".join(event.model_dump_json() for event in repository.events)
        for secret in (ENROLLMENT_SECRET, AGENT_SECRET, REPLACEMENT_SECRET):
            assert secret not in serialized_events
        for secret_hash in (
            service.hash_secret(ENROLLMENT_SECRET),
            service.hash_secret(AGENT_SECRET),
            service.hash_secret(REPLACEMENT_SECRET),
        ):
            assert secret_hash not in serialized_events

    asyncio.run(scenario())
