from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from lab_platform.core.errors import (
    AuthenticationFailedError,
    InvalidCredentialsError,
    LoginRateLimitExceededError,
    OrganisationSuspendedError,
    ServiceAccountDisabledError,
    SessionExpiredError,
    SessionRevokedError,
    TokenRevokedError,
    UserDisabledError,
    UserLockedError,
)
from lab_platform.core.identity import (
    IdentityAuthenticationService,
    ScryptPasswordHasher,
    sanitize_audit_metadata,
)
from lab_platform.models import (
    ApiCredential,
    AuditEvent,
    AuditOutcome,
    AuthenticationSource,
    LoginAttempt,
    Organisation,
    OrganisationStatus,
    PasswordCredential,
    PrincipalType,
    ServiceAccount,
    ServiceAccountStatus,
    User,
    UserSession,
    UserStatus,
)


class IdentityRepository:
    def __init__(self) -> None:
        self.organisations: dict[UUID, Organisation] = {}
        self.users: dict[UUID, User] = {}
        self.passwords: dict[UUID, PasswordCredential] = {}
        self.sessions: dict[UUID, UserSession] = {}
        self.accounts: dict[UUID, ServiceAccount] = {}
        self.credentials: dict[UUID, ApiCredential] = {}
        self.audit_events: list[AuditEvent] = []
        self.login_attempts: list[LoginAttempt] = []

    async def get_organisation(self, organisation_id: UUID) -> Organisation | None:
        return self.organisations.get(organisation_id)

    async def get_organisation_by_slug(self, slug: str) -> Organisation | None:
        return next((item for item in self.organisations.values() if item.slug == slug), None)

    async def get_user(self, organisation_id: UUID, user_id: UUID) -> User | None:
        user = self.users.get(user_id)
        return user if user is not None and user.organisation_id == organisation_id else None

    async def get_user_by_username(
        self,
        organisation_id: UUID,
        username: str,
    ) -> User | None:
        return next(
            (
                user
                for user in self.users.values()
                if user.organisation_id == organisation_id and user.username == username
            ),
            None,
        )

    async def update_user(self, user: User) -> User:
        self.users[user.id] = user
        return user

    async def get_password_credential(self, user_id: UUID) -> PasswordCredential | None:
        return self.passwords.get(user_id)

    async def create_session(self, session: UserSession) -> UserSession:
        self.sessions[session.id] = session
        return session

    async def get_session(self, session_id: UUID) -> UserSession | None:
        return self.sessions.get(session_id)

    async def update_session(self, session: UserSession) -> UserSession:
        self.sessions[session.id] = session
        return session

    async def list_sessions(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> list[UserSession]:
        return [
            session
            for session in self.sessions.values()
            if session.organisation_id == organisation_id and session.user_id == user_id
        ]

    async def get_service_account(
        self,
        organisation_id: UUID,
        service_account_id: UUID,
    ) -> ServiceAccount | None:
        account = self.accounts.get(service_account_id)
        return (
            account if account is not None and account.organisation_id == organisation_id else None
        )

    async def update_service_account(self, account: ServiceAccount) -> ServiceAccount:
        self.accounts[account.id] = account
        return account

    async def create_api_credential(self, credential: ApiCredential) -> ApiCredential:
        self.credentials[credential.id] = credential
        return credential

    async def get_api_credential_by_id(self, credential_id: UUID) -> ApiCredential | None:
        return self.credentials.get(credential_id)

    async def update_api_credential(self, credential: ApiCredential) -> ApiCredential:
        self.credentials[credential.id] = credential
        return credential

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent:
        self.audit_events.append(event)
        return event

    async def record_login_attempt(self, attempt: LoginAttempt) -> LoginAttempt:
        self.login_attempts.append(attempt)
        return attempt

    async def count_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
        since: datetime,
    ) -> int:
        return sum(
            1
            for attempt in self.login_attempts
            if attempt.organisation_slug == organisation_slug
            and attempt.username == username
            and attempt.ip_address == ip_address
            and attempt.attempted_at >= since
            and not attempt.succeeded
        )

    async def clear_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
    ) -> None:
        self.login_attempts = [
            attempt
            for attempt in self.login_attempts
            if not (
                attempt.organisation_slug == organisation_slug
                and attempt.username == username
                and attempt.ip_address == ip_address
                and not attempt.succeeded
            )
        ]


def authentication_fixture(
    *,
    user_status: UserStatus = UserStatus.ACTIVE,
    organisation_status: OrganisationStatus = OrganisationStatus.ACTIVE,
) -> tuple[
    IdentityRepository,
    IdentityAuthenticationService,
    Organisation,
    User,
    datetime,
]:
    now = datetime(2026, 8, 2, 8, tzinfo=UTC)
    repository = IdentityRepository()
    organisation = Organisation(
        slug="Example-Team",
        name="Example Team",
        status=organisation_status,
        created_at=now,
        updated_at=now,
    )
    user = User(
        organisation_id=organisation.id,
        username="Alice",
        display_name="Alice Example",
        status=user_status,
        created_at=now,
        updated_at=now,
    )
    hasher = ScryptPasswordHasher(salt_factory=lambda size: b"s" * size)
    repository.organisations[organisation.id] = organisation
    repository.users[user.id] = user
    repository.passwords[user.id] = PasswordCredential(
        user_id=user.id,
        password_hash=hasher.hash("correct horse battery staple"),
        created_at=now,
        updated_at=now,
    )
    secret_counter = 0

    def next_secret(size: int) -> bytes:
        nonlocal secret_counter
        secret_counter += 1
        return bytes([secret_counter]) * size

    service = IdentityAuthenticationService(
        repository,
        password_hasher=hasher,
        login_rate_limit_attempts=2,
        clock=lambda: now,
        secret_factory=next_secret,
    )
    return repository, service, organisation, user, now


def test_password_hash_is_salted_and_malformed_hashes_fail_closed() -> None:
    salts = iter((b"a" * 16, b"b" * 16))
    hasher = ScryptPasswordHasher(salt_factory=lambda _size: next(salts))

    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert "correct horse" not in first
    assert hasher.verify("correct horse battery staple", first)
    assert not hasher.verify("wrong password", first)
    assert not hasher.verify("password", "invalid")


def test_local_login_session_refresh_and_revocation() -> None:
    repository, service, organisation, user, _now = authentication_fixture()

    issued = asyncio.run(
        service.login(
            organisation_slug="EXAMPLE-TEAM",
            username="ALICE",
            password="correct horse battery staple",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
    )

    assert issued.principal.id == user.id
    assert issued.organisation == organisation
    assert issued.access_token.startswith(f"lps_{issued.session.id.hex}_")
    authenticated = asyncio.run(service.authenticate_session(issued.access_token))
    assert authenticated.principal.display_name == "Alice Example"
    assert asyncio.run(service.list_sessions(authenticated)) == [issued.session]

    refreshed = asyncio.run(service.refresh_session(issued.access_token))
    assert refreshed.access_token != issued.access_token
    with pytest.raises(AuthenticationFailedError):
        asyncio.run(service.authenticate_session(issued.access_token))
    asyncio.run(service.logout(refreshed.access_token))
    with pytest.raises(SessionRevokedError):
        asyncio.run(service.authenticate_session(refreshed.access_token))

    assert [event.action for event in repository.audit_events] == [
        "USER_LOGIN_SUCCEEDED",
        "USER_LOGOUT",
    ]


def test_oidc_user_cannot_use_a_stale_local_password_record() -> None:
    repository, service, _organisation, user, _now = authentication_fixture()
    repository.users[user.id] = user.model_copy(
        update={"authentication_source": AuthenticationSource.OIDC},
    )

    with pytest.raises(InvalidCredentialsError):
        asyncio.run(
            service.login(
                organisation_slug="example-team",
                username="alice",
                password="correct horse battery staple",
            )
        )


def test_invalid_login_is_generic_and_rate_limited() -> None:
    repository, service, _organisation, _user, _now = authentication_fixture()

    for _ in range(2):
        with pytest.raises(InvalidCredentialsError, match="username or password"):
            asyncio.run(
                service.login(
                    organisation_slug="example-team",
                    username="alice",
                    password="incorrect password",
                    ip_address="192.0.2.10",
                )
            )
    with pytest.raises(LoginRateLimitExceededError):
        asyncio.run(
            service.login(
                organisation_slug="example-team",
                username="alice",
                password="correct horse battery staple",
                ip_address="192.0.2.10",
            )
        )

    assert len(repository.login_attempts) == 2
    assert all(event.action == "USER_LOGIN_FAILED" for event in repository.audit_events)


def test_disabled_user_and_expired_session_are_rejected() -> None:
    _repository, service, _organisation, _user, _now = authentication_fixture(
        user_status=UserStatus.DISABLED
    )
    with pytest.raises(UserDisabledError):
        asyncio.run(
            service.login(
                organisation_slug="example-team",
                username="alice",
                password="correct horse battery staple",
            )
        )

    repository, service, _organisation, _user, now = authentication_fixture()
    issued = asyncio.run(
        service.login(
            organisation_slug="example-team",
            username="alice",
            password="correct horse battery staple",
        )
    )
    repository.sessions[issued.session.id] = issued.session.model_copy(
        update={"expires_at": now - timedelta(seconds=1)}
    )
    with pytest.raises(SessionExpiredError):
        asyncio.run(service.authenticate_session(issued.access_token))


@pytest.mark.parametrize(
    ("user_status", "organisation_status", "expected_error", "reason"),
    (
        (
            UserStatus.DISABLED,
            OrganisationStatus.ACTIVE,
            UserDisabledError,
            "The user account is disabled.",
        ),
        (
            UserStatus.LOCKED,
            OrganisationStatus.ACTIVE,
            UserLockedError,
            "The user account is locked.",
        ),
        (
            UserStatus.ACTIVE,
            OrganisationStatus.SUSPENDED,
            OrganisationSuspendedError,
            "The organisation is not active.",
        ),
    ),
)
def test_inactive_local_identity_login_failures_are_audited_without_secrets(
    user_status: UserStatus,
    organisation_status: OrganisationStatus,
    expected_error: type[Exception],
    reason: str,
) -> None:
    repository, service, _organisation, user, _now = authentication_fixture(
        user_status=user_status,
        organisation_status=organisation_status,
    )
    password = "correct horse battery staple"
    injected_token = f"lps_{uuid4().hex}_{'x' * 43}"

    with pytest.raises(expected_error):
        asyncio.run(
            service.login(
                organisation_slug="example-team",
                username="alice",
                password=password,
                ip_address="192.0.2.50",
                user_agent=f"pytest Bearer {injected_token}",
            )
        )

    assert len(repository.login_attempts) == 1
    assert not repository.login_attempts[0].succeeded
    assert len(repository.audit_events) == 1
    event = repository.audit_events[0]
    assert event.action == "USER_LOGIN_FAILED"
    assert event.outcome is AuditOutcome.FAILED
    assert event.resource_id == str(user.id)
    assert event.reason == reason
    assert event.metadata == {"authentication_source": AuthenticationSource.LOCAL.value}
    serialized = event.model_dump_json()
    assert password not in serialized
    assert injected_token not in serialized


def test_inactive_oidc_identity_login_failure_is_audited() -> None:
    repository, service, organisation, user, _now = authentication_fixture(
        user_status=UserStatus.LOCKED,
    )
    oidc_user = user.model_copy(update={"authentication_source": AuthenticationSource.OIDC})
    repository.users[user.id] = oidc_user

    with pytest.raises(UserLockedError):
        asyncio.run(
            service.login_oidc_user(
                user=oidc_user,
                organisation=organisation,
                user_agent="pytest-oidc",
            )
        )

    assert len(repository.audit_events) == 1
    event = repository.audit_events[0]
    assert event.action == "USER_LOGIN_FAILED"
    assert event.resource_id == str(user.id)
    assert event.reason == "The user account is locked."
    assert event.metadata == {"authentication_source": AuthenticationSource.OIDC.value}


def test_service_credential_lookup_ip_narrowing_and_revocation() -> None:
    repository, service, organisation, _user, now = authentication_fixture()
    account = ServiceAccount(
        organisation_id=organisation.id,
        name="github-ci",
        created_at=now,
        updated_at=now,
    )
    repository.accounts[account.id] = account

    issued = asyncio.run(
        service.issue_api_credential(
            service_account=account,
            name="main",
            allowed_ip_ranges=["192.0.2.18/24"],
            permission_restrictions={"workflows:run", "artifacts:write"},
        )
    )
    assert issued.token.startswith(f"lp_{issued.credential.id.hex}_")
    with pytest.raises(AuthenticationFailedError, match="address"):
        asyncio.run(service.authenticate_api_credential(issued.token, source_ip="198.51.100.1"))

    context = asyncio.run(
        service.authenticate_api_credential(
            issued.token,
            source_ip="192.0.2.44",
        )
    )
    assert context.principal.type is PrincipalType.SERVICE_ACCOUNT
    assert context.permission_restrictions == {"workflows:run", "artifacts:write"}
    assert repository.credentials[issued.credential.id].last_used_at == now

    revoked = asyncio.run(
        service.revoke_api_credential(
            repository.credentials[issued.credential.id],
            actor=context.principal,
        )
    )
    assert revoked.revoked_at == now
    with pytest.raises(TokenRevokedError):
        asyncio.run(service.authenticate_api_credential(issued.token, source_ip="192.0.2.44"))
    failures = [
        event
        for event in repository.audit_events
        if event.action == "CREDENTIAL_AUTHENTICATION_FAILED"
    ]
    assert len(failures) == 2
    assert {event.reason for event in failures} == {
        "Source address was outside the credential allow-list.",
        "Credential was revoked.",
    }
    assert all("token" not in event.metadata for event in failures)


@pytest.mark.parametrize(
    ("account_status", "organisation_status", "expected_error", "reason"),
    (
        (
            ServiceAccountStatus.DISABLED,
            OrganisationStatus.ACTIVE,
            ServiceAccountDisabledError,
            "The service account is disabled.",
        ),
        (
            ServiceAccountStatus.ACTIVE,
            OrganisationStatus.SUSPENDED,
            OrganisationSuspendedError,
            "The organisation is not active.",
        ),
    ),
)
def test_inactive_service_credential_failures_are_audited_without_secrets(
    account_status: ServiceAccountStatus,
    organisation_status: OrganisationStatus,
    expected_error: type[Exception],
    reason: str,
) -> None:
    repository, service, organisation, _user, now = authentication_fixture()
    account = ServiceAccount(
        organisation_id=organisation.id,
        name="inactive-ci",
        created_at=now,
        updated_at=now,
    )
    repository.accounts[account.id] = account
    issued = asyncio.run(
        service.issue_api_credential(
            service_account=account,
            name="inactive-state-test",
        )
    )
    repository.accounts[account.id] = account.model_copy(update={"status": account_status})
    repository.organisations[organisation.id] = organisation.model_copy(
        update={"status": organisation_status}
    )

    with pytest.raises(expected_error):
        asyncio.run(
            service.authenticate_api_credential(
                issued.token,
                source_ip="192.0.2.60",
            )
        )

    failures = [
        event
        for event in repository.audit_events
        if event.action == "CREDENTIAL_AUTHENTICATION_FAILED"
    ]
    assert len(failures) == 1
    event = failures[0]
    assert event.resource_id == str(issued.credential.id)
    assert event.outcome is AuditOutcome.FAILED
    assert event.reason == reason
    serialized = event.model_dump_json()
    assert issued.token not in serialized
    assert issued.credential.secret_hash not in serialized


def test_model_and_audit_safety_validation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="CIDR"):
        ApiCredential(
            organisation_id=uuid4(),
            principal_id=uuid4(),
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            name="bad",
            secret_hash="a" * 64,
            created_at=now,
            allowed_ip_ranges=["not-a-network"],
        )

    assert sanitize_audit_metadata(
        {
            "token": "do-not-record",
            "password_hint": "do-not-record",
            "workflow": "smoke-test",
            "nested": {"private_key": "do-not-record", "safe": 1},
        }
    ) == {"workflow": "smoke-test", "nested": {"safe": 1}}

    with pytest.raises(ValueError, match="must not contain secrets"):
        AuditEvent(
            organisation_id=uuid4(),
            action="UNSAFE_NESTED_METADATA",
            resource_type="TEST",
            outcome=AuditOutcome.SUCCEEDED,
            metadata={"details": {"token": "must-not-be-persisted"}},
        )
    with pytest.raises(ValueError, match="must not contain secrets"):
        AuditEvent(
            organisation_id=uuid4(),
            action="UNSAFE_LIST_METADATA",
            resource_type="TEST",
            outcome=AuditOutcome.SUCCEEDED,
            metadata={"details": [{"serial-log": "must-not-be-persisted"}]},
        )
    for sensitive_key in ("api_key", "x-api-key", "accessKey", "privateKey", "passphrase"):
        with pytest.raises(ValueError, match="must not contain secrets"):
            AuditEvent(
                organisation_id=uuid4(),
                action="UNSAFE_CREDENTIAL_METADATA",
                resource_type="TEST",
                outcome=AuditOutcome.SUCCEEDED,
                metadata={"details": [{sensitive_key: "must-not-be-persisted"}]},
            )

    lab_token = f"lps_{uuid4().hex}_{'x' * 43}"
    oidc_token = f"eyJ{'a' * 16}.{'b' * 16}.{'c' * 16}"
    redacted = AuditEvent(
        organisation_id=uuid4(),
        action="CLIENT_TEXT_REDACTED",
        resource_type="TEST",
        outcome=AuditOutcome.SUCCEEDED,
        user_agent=f"test-client Bearer {lab_token}",
        reason=f"provider returned id_token={oidc_token}",
        metadata={"details": {"client_text": lab_token}},
    )
    serialized = redacted.model_dump_json()
    assert lab_token not in serialized
    assert oidc_token not in serialized
    assert serialized.count("[REDACTED]") >= 3

    named_secrets = {
        "authorization": "Basic-opaque-auth-material",
        "x_api_key": "opaque-api-key",
        "accessKey": "opaque-access-key",
        "cookie": "opaque-session-cookie",
        "firmware": "opaque-firmware-bytes",
        "passphrase": "opaque-passphrase",
        "password": "swordfish",
        "privateKey": "opaque-private-key-material",
        "refresh_token": "opaque-refresh-value",
        "client_secret": "opaque-client-secret",
        "serial-log": "opaque-serial-output",
        "csrf_token": "opaque-csrf-value",
    }
    named_text = ", ".join(
        f'"{key}": "{secret}"' if index % 2 else f"{key}={secret}"
        for index, (key, secret) in enumerate(named_secrets.items())
    )
    named_redacted = AuditEvent(
        organisation_id=uuid4(),
        action="NAMED_CLIENT_TEXT_REDACTED",
        resource_type="TEST",
        outcome=AuditOutcome.FAILED,
        user_agent='payload={"client_secret":"nested-opaque-value","status":"failed"}',
        reason="password: swordfish; status: failed",
        metadata={"details": {"client_text": f"{named_text}, status=failed"}},
    )
    named_serialized = named_redacted.model_dump_json()
    assert named_redacted.reason == "password: [REDACTED]; status: failed"
    assert "status=failed" in named_serialized
    assert "nested-opaque-value" not in named_serialized
    assert named_redacted.user_agent is not None
    assert '"status":"failed"' in named_redacted.user_agent
    for secret in named_secrets.values():
        assert secret not in named_serialized
    assert named_serialized.count("[REDACTED]") == len(named_secrets) + 2

    pgp_private_key = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "opaque-private-key-payload\n"
        "-----END PGP PRIVATE KEY BLOCK-----"
    )
    private_key_redacted = AuditEvent(
        organisation_id=uuid4(),
        action="PRIVATE_KEY_TEXT_REDACTED",
        resource_type="TEST",
        outcome=AuditOutcome.FAILED,
        reason=pgp_private_key,
        metadata={"details": {"client_text": pgp_private_key}},
    )
    assert private_key_redacted.reason == "[REDACTED]"
    assert private_key_redacted.metadata == {"details": {"client_text": "[REDACTED]"}}
    assert "opaque-private-key-payload" not in private_key_redacted.model_dump_json()

    oversized = AuditEvent(
        organisation_id=uuid4(),
        actor_display_name="a" * 250,
        action="ACTION_" + "a" * 250,
        resource_type="RESOURCE_" + "r" * 150,
        resource_id="resource/" + "r" * 600,
        outcome=AuditOutcome.FAILED,
        source_ip="192.0.2.1" + "0" * 100,
        user_agent=f"{'u' * 994} {oidc_token}{'z' * 100}",
        reason=f"provider failure id_token={oidc_token} {'r' * 2100}",
    )
    assert len(oversized.actor_display_name or "") == 200
    assert len(oversized.action) == 200
    assert len(oversized.resource_type) == 100
    assert len(oversized.resource_id or "") == 500
    assert len(oversized.source_ip or "") == 64
    assert len(oversized.user_agent or "") == 1000
    assert len(oversized.reason or "") == 2000
    assert oversized.user_agent is not None
    assert oversized.user_agent.endswith("…[REDACTED]")
    oversized_json = oversized.model_dump_json()
    assert oidc_token not in oversized_json
    assert "eyJ" not in oversized_json


def test_disabled_service_account_cannot_receive_credentials() -> None:
    _repository, service, organisation, _user, now = authentication_fixture()
    account = ServiceAccount(
        organisation_id=organisation.id,
        name="disabled",
        status=ServiceAccountStatus.DISABLED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ServiceAccountDisabledError):
        asyncio.run(service.issue_api_credential(service_account=account, name="never"))
