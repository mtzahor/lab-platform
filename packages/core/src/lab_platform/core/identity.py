from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from lab_platform.core.errors import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    InvalidCredentialsError,
    LoginRateLimitExceededError,
    OrganisationSuspendedError,
    ServiceAccountDisabledError,
    SessionExpiredError,
    SessionRevokedError,
    TokenExpiredError,
    TokenRevokedError,
    UserDisabledError,
    UserLockedError,
)
from lab_platform.models import (
    ApiCredential,
    AuditEvent,
    AuditOutcome,
    AuthenticationContext,
    AuthenticationSource,
    LoginAttempt,
    Organisation,
    OrganisationStatus,
    PasswordCredential,
    Principal,
    PrincipalType,
    ServiceAccount,
    ServiceAccountStatus,
    User,
    UserSession,
    UserStatus,
)

_PASSWORD_SCHEME = "scrypt"
_PASSWORD_VERSION = "v1"
_SESSION_PREFIX = "lps"
_CREDENTIAL_PREFIX = "lp"
_SECRET_BYTES = 32
_AUDIT_METADATA_LIMIT = 8 * 1024
_SENSITIVE_METADATA_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "firmware",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "serial_log",
        "token",
    }
)


class IdentityAuthenticationRepository(Protocol):
    async def get_organisation(self, organisation_id: UUID) -> Organisation | None: ...

    async def get_organisation_by_slug(self, slug: str) -> Organisation | None: ...

    async def get_user(self, organisation_id: UUID, user_id: UUID) -> User | None: ...

    async def get_user_by_username(
        self,
        organisation_id: UUID,
        username: str,
    ) -> User | None: ...

    async def update_user(self, user: User) -> User: ...

    async def get_password_credential(self, user_id: UUID) -> PasswordCredential | None: ...

    async def create_session(self, session: UserSession) -> UserSession: ...

    async def get_session(self, session_id: UUID) -> UserSession | None: ...

    async def touch_session(
        self,
        session_id: UUID,
        *,
        expected_secret_hash: str,
        last_seen_at: datetime,
    ) -> bool: ...

    async def rotate_session(
        self,
        session_id: UUID,
        *,
        expected_secret_hash: str,
        secret_hash: str,
        expires_at: datetime,
        last_seen_at: datetime,
    ) -> UserSession | None: ...

    async def revoke_session(
        self,
        session_id: UUID,
        *,
        revoked_at: datetime,
        expected_secret_hash: str | None = None,
    ) -> UserSession | None: ...

    async def list_sessions(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> list[UserSession]: ...

    async def get_service_account(
        self,
        organisation_id: UUID,
        service_account_id: UUID,
    ) -> ServiceAccount | None: ...

    async def update_service_account(
        self,
        service_account: ServiceAccount,
    ) -> ServiceAccount: ...

    async def create_api_credential(self, credential: ApiCredential) -> ApiCredential: ...

    async def get_api_credential_by_id(self, credential_id: UUID) -> ApiCredential | None: ...

    async def update_api_credential(self, credential: ApiCredential) -> ApiCredential: ...

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    async def record_login_attempt(self, attempt: LoginAttempt) -> LoginAttempt: ...

    async def count_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
        since: datetime,
    ) -> int: ...

    async def clear_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: UserSession
    access_token: str
    principal: Principal
    organisation: Organisation


@dataclass(frozen=True, slots=True)
class IssuedApiCredential:
    credential: ApiCredential
    token: str


class ScryptPasswordHasher:
    """Versioned, salted stdlib scrypt password hashing.

    The encoded form contains only public algorithm parameters, salt and derived
    key. Password material is never retained by this object.
    """

    def __init__(
        self,
        *,
        n: int = 2**14,
        r: int = 8,
        p: int = 1,
        salt_bytes: int = 16,
        key_bytes: int = 32,
        salt_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if n < 2 or n & (n - 1):
            raise ValueError("scrypt n must be a power of two")
        if min(r, p, salt_bytes, key_bytes) <= 0:
            raise ValueError("scrypt parameters must be positive")
        self._n = n
        self._r = r
        self._p = p
        self._salt_bytes = salt_bytes
        self._key_bytes = key_bytes
        self._salt_factory = salt_factory

    def hash(self, password: str) -> str:
        if not password:
            raise ValueError("password must not be empty")
        salt = self._salt_factory(self._salt_bytes)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=self._n,
            r=self._r,
            p=self._p,
            dklen=self._key_bytes,
        )
        return "$".join(
            (
                _PASSWORD_SCHEME,
                _PASSWORD_VERSION,
                str(self._n),
                str(self._r),
                str(self._p),
                _encode_bytes(salt),
                _encode_bytes(derived),
            )
        )

    def verify(self, password: str, encoded: str) -> bool:
        try:
            scheme, version, n, r, p, salt, expected = encoded.split("$", 6)
            if scheme != _PASSWORD_SCHEME or version != _PASSWORD_VERSION:
                return False
            expected_bytes = _decode_bytes(expected)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_decode_bytes(salt),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected_bytes),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual, expected_bytes)


class IdentityAuthenticationService:
    """Authenticate local users and identity-bound service credentials."""

    def __init__(
        self,
        repository: IdentityAuthenticationRepository,
        *,
        password_hasher: ScryptPasswordHasher | None = None,
        minimum_password_length: int = 12,
        access_token_minutes: int = 15,
        session_hours: int = 12,
        maximum_session_days: int = 7,
        login_rate_limit_attempts: int = 10,
        login_rate_limit_window_minutes: int = 15,
        audit_enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        secret_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if minimum_password_length < 12:
            raise ValueError("minimum password length must be at least 12")
        if access_token_minutes <= 0 or session_hours <= 0 or maximum_session_days <= 0:
            raise ValueError("session lifetimes must be positive")
        if access_token_minutes >= session_hours * 60:
            raise ValueError("access token lifetime must be shorter than session lifetime")
        if session_hours > maximum_session_days * 24:
            raise ValueError("session lifetime must not exceed maximum session lifetime")
        self._repository = repository
        self._password_hasher = password_hasher or ScryptPasswordHasher()
        self._minimum_password_length = minimum_password_length
        self._access_lifetime = timedelta(minutes=access_token_minutes)
        self._session_lifetime = timedelta(hours=session_hours)
        self._maximum_session_lifetime = timedelta(days=maximum_session_days)
        self._rate_limit_attempts = login_rate_limit_attempts
        self._rate_limit_window = timedelta(minutes=login_rate_limit_window_minutes)
        self._audit_enabled = audit_enabled
        self._clock = clock
        self._secret_factory = secret_factory

    def hash_password(self, password: str) -> str:
        if len(password) < self._minimum_password_length:
            raise ValueError(
                f"password must contain at least {self._minimum_password_length} characters"
            )
        return self._password_hasher.hash(password)

    async def login(
        self,
        *,
        organisation_slug: str,
        username: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> IssuedSession:
        now = self._clock()
        slug = organisation_slug.strip().casefold()
        normalized_username = username.strip().casefold()
        failures = await self._repository.count_failed_login_attempts(
            slug,
            normalized_username,
            ip_address,
            now - self._rate_limit_window,
        )
        if failures >= self._rate_limit_attempts:
            raise LoginRateLimitExceededError(
                "Too many login attempts. Try again later.",
                retry_after_seconds=int(self._rate_limit_window.total_seconds()),
            )

        organisation = await self._repository.get_organisation_by_slug(slug)
        user = (
            await self._repository.get_user_by_username(organisation.id, normalized_username)
            if organisation is not None
            else None
        )
        password_record = (
            await self._repository.get_password_credential(user.id) if user is not None else None
        )
        valid_password = (
            user is not None
            and user.authentication_source is AuthenticationSource.LOCAL
            and password_record is not None
            and self._password_hasher.verify(
                password,
                password_record.password_hash,
            )
        )
        if organisation is None or user is None or not valid_password:
            await self._record_login_attempt(slug, normalized_username, ip_address, False)
            if organisation is not None:
                await self._audit(
                    organisation_id=organisation.id,
                    action="USER_LOGIN_FAILED",
                    resource_type="USER",
                    resource_id=str(user.id) if user is not None else None,
                    outcome=AuditOutcome.FAILED,
                    request_id=request_id,
                    source_ip=ip_address,
                    user_agent=user_agent,
                    reason="Invalid username or password.",
                )
            raise InvalidCredentialsError("The username or password is invalid.")

        try:
            self._require_organisation_active(organisation)
            self._require_user_active(user)
        except (OrganisationSuspendedError, UserDisabledError, UserLockedError) as exc:
            await self._record_login_attempt(slug, normalized_username, ip_address, False)
            await self._audit(
                organisation_id=organisation.id,
                action="USER_LOGIN_FAILED",
                resource_type="USER",
                resource_id=str(user.id),
                outcome=AuditOutcome.FAILED,
                request_id=request_id,
                source_ip=ip_address,
                user_agent=user_agent,
                reason=exc.message,
                metadata={"authentication_source": AuthenticationSource.LOCAL.value},
            )
            raise
        await self._record_login_attempt(slug, normalized_username, ip_address, True)
        await self._repository.clear_failed_login_attempts(
            slug,
            normalized_username,
            ip_address,
        )
        updated_user = user.model_copy(update={"last_login_at": now, "updated_at": now})
        await self._repository.update_user(updated_user)
        issued = await self._issue_session(
            updated_user,
            organisation,
            now=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._audit(
            organisation_id=organisation.id,
            action="USER_LOGIN_SUCCEEDED",
            resource_type="SESSION",
            resource_id=str(issued.session.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=issued.principal,
            request_id=request_id,
            source_ip=ip_address,
            user_agent=user_agent,
        )
        return issued

    async def login_oidc_user(
        self,
        *,
        user: User,
        organisation: Organisation,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> IssuedSession:
        """Issue a platform session after an OIDC service has verified the provider token."""

        if user.organisation_id != organisation.id:
            raise AuthenticationFailedError("The OIDC user organisation is invalid.")
        if user.authentication_source is not AuthenticationSource.OIDC:
            raise AuthenticationFailedError("The user is not configured for OIDC authentication.")
        try:
            self._require_organisation_active(organisation)
            self._require_user_active(user)
        except (OrganisationSuspendedError, UserDisabledError, UserLockedError) as exc:
            await self._audit(
                organisation_id=organisation.id,
                action="USER_LOGIN_FAILED",
                resource_type="USER",
                resource_id=str(user.id),
                outcome=AuditOutcome.FAILED,
                request_id=request_id,
                source_ip=ip_address,
                user_agent=user_agent,
                reason=exc.message,
                metadata={"authentication_source": AuthenticationSource.OIDC.value},
            )
            raise
        now = self._clock()
        updated_user = await self._repository.update_user(
            user.model_copy(update={"last_login_at": now, "updated_at": now})
        )
        issued = await self._issue_session(
            updated_user,
            organisation,
            now=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._audit(
            organisation_id=organisation.id,
            action="USER_LOGIN_SUCCEEDED",
            resource_type="SESSION",
            resource_id=str(issued.session.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=issued.principal,
            request_id=request_id,
            source_ip=ip_address,
            user_agent=user_agent,
            metadata={"authentication_source": AuthenticationSource.OIDC.value},
        )
        return issued

    async def record_oidc_login_failure(
        self,
        *,
        organisation_slug: str,
        reason: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: UUID | None = None,
    ) -> None:
        """Record a provider/mapping failure once an OIDC tenant is known."""

        organisation = await self._repository.get_organisation_by_slug(
            organisation_slug.strip().casefold()
        )
        if organisation is None:
            return
        await self._audit(
            organisation_id=organisation.id,
            action="USER_LOGIN_FAILED",
            resource_type="USER",
            resource_id=None,
            outcome=AuditOutcome.FAILED,
            request_id=request_id,
            source_ip=ip_address,
            user_agent=user_agent,
            reason=reason,
            metadata={"authentication_source": AuthenticationSource.OIDC.value},
        )

    async def authenticate_session(self, token: str | None) -> AuthenticationContext:
        session_id, secret = _parse_token(token, _SESSION_PREFIX)
        secret_hash = _hash_secret(secret)
        session = await self._repository.get_session(session_id)
        if session is None or not hmac.compare_digest(session.secret_hash, secret_hash):
            raise AuthenticationFailedError("The session token is invalid.")
        now = self._clock()
        if session.revoked_at is not None:
            raise SessionRevokedError("The session has been revoked.")
        if session.expires_at <= now:
            raise SessionExpiredError("The session access token has expired.")
        principal, _organisation = await self._session_principal(session)
        if now > session.last_seen_at:
            await self._repository.touch_session(
                session.id,
                expected_secret_hash=secret_hash,
                last_seen_at=now,
            )
        return AuthenticationContext(principal=principal, session_id=session.id)

    async def refresh_session(self, token: str | None) -> IssuedSession:
        session_id, secret = _parse_token(token, _SESSION_PREFIX)
        secret_hash = _hash_secret(secret)
        session = await self._repository.get_session(session_id)
        if session is None or not hmac.compare_digest(session.secret_hash, secret_hash):
            raise AuthenticationFailedError("The session token is invalid.")
        now = self._clock()
        if session.revoked_at is not None:
            raise SessionRevokedError("The session has been revoked.")
        if session.maximum_expires_at <= now:
            raise SessionExpiredError("The session has expired.")
        principal, organisation = await self._session_principal(session)
        new_secret = _encode_bytes(self._secret_factory(_SECRET_BYTES))
        updated = await self._repository.rotate_session(
            session.id,
            expected_secret_hash=secret_hash,
            secret_hash=_hash_secret(new_secret),
            expires_at=min(now + self._access_lifetime, session.maximum_expires_at),
            last_seen_at=now,
        )
        if updated is None:
            current = await self._repository.get_session(session.id)
            if current is not None and current.revoked_at is not None:
                raise SessionRevokedError("The session has been revoked.")
            raise AuthenticationFailedError("The session token is invalid.")
        return IssuedSession(
            session=updated,
            access_token=_format_token(_SESSION_PREFIX, updated.id, new_secret),
            principal=principal,
            organisation=organisation,
        )

    async def logout(
        self,
        token: str | None,
        *,
        request_id: UUID | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> UserSession:
        session_id, secret = _parse_token(token, _SESSION_PREFIX)
        secret_hash = _hash_secret(secret)
        session = await self._repository.get_session(session_id)
        if session is None or not hmac.compare_digest(session.secret_hash, secret_hash):
            raise AuthenticationFailedError("The session token is invalid.")
        principal, _organisation = await self._session_principal(session)
        revoked = await self._repository.revoke_session(
            session.id,
            revoked_at=self._clock(),
            expected_secret_hash=secret_hash,
        )
        if revoked is None:
            raise AuthenticationFailedError("The session token is invalid.")
        session = revoked
        await self._audit(
            organisation_id=session.organisation_id,
            action="USER_LOGOUT",
            resource_type="SESSION",
            resource_id=str(session.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=principal,
            request_id=request_id,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return session

    async def revoke_session(
        self,
        context: AuthenticationContext,
        session_id: UUID,
        *,
        request_id: UUID | None = None,
    ) -> UserSession:
        session = await self._repository.get_session(session_id)
        if (
            session is None
            or session.organisation_id != context.principal.organisation_id
            or (
                context.principal.type is PrincipalType.USER
                and session.user_id != context.principal.id
            )
        ):
            raise AuthenticationFailedError("The session does not exist.")
        revoked = await self._repository.revoke_session(
            session.id,
            revoked_at=self._clock(),
        )
        if revoked is None:
            raise AuthenticationFailedError("The session does not exist.")
        session = revoked
        await self._audit(
            organisation_id=session.organisation_id,
            action="SESSION_REVOKED",
            resource_type="SESSION",
            resource_id=str(session.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=context.principal,
            request_id=request_id,
        )
        return session

    async def list_sessions(self, context: AuthenticationContext) -> list[UserSession]:
        if context.principal.type is not PrincipalType.USER:
            return []
        return await self._repository.list_sessions(
            context.principal.organisation_id,
            context.principal.id,
        )

    async def issue_api_credential(
        self,
        *,
        service_account: ServiceAccount,
        name: str,
        expires_at: datetime | None = None,
        allowed_ip_ranges: Iterable[str] = (),
        permission_restrictions: set[str] | None = None,
        actor: Principal | None = None,
    ) -> IssuedApiCredential:
        self._require_service_account_active(service_account)
        now = self._clock()
        if expires_at is not None and expires_at <= now:
            raise ValueError("credential expiry must be in the future")
        ranges = [str(ipaddress.ip_network(value, strict=False)) for value in allowed_ip_ranges]
        secret = _encode_bytes(self._secret_factory(_SECRET_BYTES))
        credential = ApiCredential(
            organisation_id=service_account.organisation_id,
            principal_id=service_account.id,
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            name=name,
            secret_hash=_hash_secret(secret),
            created_at=now,
            expires_at=expires_at,
            allowed_ip_ranges=ranges,
            permission_restrictions=permission_restrictions,
        )
        created = await self._repository.create_api_credential(credential)
        await self._audit(
            organisation_id=created.organisation_id,
            action="CREDENTIAL_CREATED",
            resource_type="API_CREDENTIAL",
            resource_id=str(created.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=actor
            or Principal(
                id=service_account.id,
                type=PrincipalType.SERVICE_ACCOUNT,
                organisation_id=service_account.organisation_id,
                display_name=service_account.name,
            ),
            metadata={"name": created.name},
        )
        return IssuedApiCredential(
            credential=created,
            token=_format_token(_CREDENTIAL_PREFIX, created.id, secret),
        )

    async def authenticate_api_credential(
        self,
        token: str | None,
        *,
        source_ip: str | None = None,
    ) -> AuthenticationContext:
        credential_id, secret = _parse_token(token, _CREDENTIAL_PREFIX)
        credential = await self._repository.get_api_credential_by_id(credential_id)
        if credential is None:
            raise AuthenticationFailedError("The API credential is invalid.")
        if not hmac.compare_digest(credential.secret_hash, _hash_secret(secret)):
            await self._audit_credential_failure(
                credential,
                reason="Credential secret did not match.",
                source_ip=source_ip,
            )
            raise AuthenticationFailedError("The API credential is invalid.")
        now = self._clock()
        if credential.revoked_at is not None:
            await self._audit_credential_failure(
                credential,
                reason="Credential was revoked.",
                source_ip=source_ip,
            )
            raise TokenRevokedError("The API credential has been revoked.")
        if credential.expires_at is not None and credential.expires_at <= now:
            await self._audit_credential_failure(
                credential,
                reason="Credential was expired.",
                source_ip=source_ip,
            )
            raise TokenExpiredError("The API credential has expired.")
        if credential.allowed_ip_ranges and not _ip_is_allowed(
            source_ip,
            credential.allowed_ip_ranges,
        ):
            await self._audit_credential_failure(
                credential,
                reason="Source address was outside the credential allow-list.",
                source_ip=source_ip,
            )
            raise AuthenticationFailedError("The API credential cannot be used from this address.")
        organisation = await self._repository.get_organisation(credential.organisation_id)
        if organisation is None:
            raise AuthenticationFailedError("The API credential organisation is unavailable.")
        try:
            self._require_organisation_active(organisation)
        except OrganisationSuspendedError as exc:
            await self._audit_credential_failure(
                credential,
                reason=exc.message,
                source_ip=source_ip,
            )
            raise
        if credential.principal_type is not PrincipalType.SERVICE_ACCOUNT:
            raise AuthenticationFailedError("Unsupported API credential principal type.")
        account = await self._repository.get_service_account(
            credential.organisation_id,
            credential.principal_id,
        )
        if account is None:
            await self._audit_credential_failure(
                credential,
                reason="The service account was unavailable.",
                source_ip=source_ip,
            )
            raise AuthenticationFailedError("The API credential principal is unavailable.")
        try:
            self._require_service_account_active(account)
        except ServiceAccountDisabledError as exc:
            await self._audit_credential_failure(
                credential,
                reason=exc.message,
                source_ip=source_ip,
            )
            raise
        await self._repository.update_api_credential(
            credential.model_copy(update={"last_used_at": now})
        )
        await self._repository.update_service_account(
            account.model_copy(update={"last_used_at": now})
        )
        return AuthenticationContext(
            principal=Principal(
                id=account.id,
                type=PrincipalType.SERVICE_ACCOUNT,
                organisation_id=account.organisation_id,
                display_name=account.name,
            ),
            credential_id=credential.id,
            permission_restrictions=credential.permission_restrictions,
        )

    async def revoke_api_credential(
        self,
        credential: ApiCredential,
        *,
        actor: Principal,
        request_id: UUID | None = None,
    ) -> ApiCredential:
        if credential.organisation_id != actor.organisation_id:
            raise AuthenticationFailedError("The API credential does not exist.")
        if credential.revoked_at is None:
            credential = await self._repository.update_api_credential(
                credential.model_copy(update={"revoked_at": self._clock()})
            )
        await self._audit(
            organisation_id=credential.organisation_id,
            action="CREDENTIAL_REVOKED",
            resource_type="API_CREDENTIAL",
            resource_id=str(credential.id),
            outcome=AuditOutcome.SUCCEEDED,
            actor=actor,
            request_id=request_id,
        )
        return credential

    async def _issue_session(
        self,
        user: User,
        organisation: Organisation,
        *,
        now: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> IssuedSession:
        secret = _encode_bytes(self._secret_factory(_SECRET_BYTES))
        maximum = min(
            now + self._session_lifetime,
            now + self._maximum_session_lifetime,
        )
        session = UserSession(
            user_id=user.id,
            organisation_id=user.organisation_id,
            secret_hash=_hash_secret(secret),
            created_at=now,
            expires_at=min(now + self._access_lifetime, maximum),
            maximum_expires_at=maximum,
            last_seen_at=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        created = await self._repository.create_session(session)
        principal = _user_principal(user)
        return IssuedSession(
            session=created,
            access_token=_format_token(_SESSION_PREFIX, created.id, secret),
            principal=principal,
            organisation=organisation,
        )

    async def _session_principal(
        self,
        session: UserSession,
    ) -> tuple[Principal, Organisation]:
        organisation = await self._repository.get_organisation(session.organisation_id)
        user = await self._repository.get_user(session.organisation_id, session.user_id)
        if organisation is None or user is None:
            raise AuthenticationFailedError("The session principal is unavailable.")
        self._require_organisation_active(organisation)
        self._require_user_active(user)
        return _user_principal(user), organisation

    async def _record_login_attempt(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
        succeeded: bool,
    ) -> None:
        await self._repository.record_login_attempt(
            LoginAttempt(
                organisation_slug=organisation_slug,
                username=username,
                ip_address=ip_address,
                attempted_at=self._clock(),
                succeeded=succeeded,
            )
        )

    async def _audit_credential_failure(
        self,
        credential: ApiCredential,
        *,
        reason: str,
        source_ip: str | None,
    ) -> None:
        await self._audit(
            organisation_id=credential.organisation_id,
            action="CREDENTIAL_AUTHENTICATION_FAILED",
            resource_type="API_CREDENTIAL",
            resource_id=str(credential.id),
            outcome=AuditOutcome.FAILED,
            source_ip=source_ip,
            reason=reason,
        )

    async def _audit(
        self,
        *,
        organisation_id: UUID,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: AuditOutcome,
        actor: Principal | None = None,
        request_id: UUID | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
        reason: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not self._audit_enabled:
            return
        await self._repository.create_audit_event(
            AuditEvent(
                organisation_id=organisation_id,
                timestamp=self._clock(),
                actor_type=actor.type if actor is not None else None,
                actor_id=actor.id if actor is not None else None,
                actor_display_name=actor.display_name if actor is not None else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                request_id=request_id,
                source_ip=source_ip,
                user_agent=user_agent,
                reason=reason,
                metadata=sanitize_audit_metadata(metadata or {}),
            )
        )

    @staticmethod
    def _require_organisation_active(organisation: Organisation) -> None:
        if organisation.status is not OrganisationStatus.ACTIVE:
            raise OrganisationSuspendedError("The organisation is not active.")

    @staticmethod
    def _require_user_active(user: User) -> None:
        if user.status is UserStatus.LOCKED:
            raise UserLockedError("The user account is locked.")
        if user.status is not UserStatus.ACTIVE:
            raise UserDisabledError("The user account is disabled.")

    @staticmethod
    def _require_service_account_active(account: ServiceAccount) -> None:
        if account.status is not ServiceAccountStatus.ACTIVE:
            raise ServiceAccountDisabledError("The service account is disabled.")


def sanitize_audit_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return bounded, JSON-safe metadata with credential-bearing keys removed."""

    return _sanitize_metadata_mapping(metadata, depth=0)


def _sanitize_metadata_mapping(
    metadata: dict[str, object],
    *,
    depth: int,
) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for raw_key, value in list(metadata.items())[:50]:
        key = str(raw_key)[:100]
        normalized = key.casefold().replace("-", "_")
        if any(part in normalized for part in _SENSITIVE_METADATA_PARTS):
            continue
        sanitized[key] = _sanitize_metadata_value(value, depth=depth)
        if len(json.dumps(sanitized, separators=(",", ":"), default=str)) > _AUDIT_METADATA_LIMIT:
            sanitized.pop(key)
            sanitized["truncated"] = True
            break
    return sanitized


def _sanitize_metadata_value(value: object, *, depth: int) -> object:
    if depth >= 3:
        return "[truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, UUID | datetime):
        return str(value)
    if isinstance(value, dict):
        return _sanitize_metadata_mapping(
            {str(key): item for key, item in list(value.items())[:50]},
            depth=depth + 1,
        )
    if isinstance(value, list | tuple | set | frozenset):
        return [_sanitize_metadata_value(item, depth=depth + 1) for item in list(value)[:50]]
    return str(value)[:1000]


def _parse_token(token: str | None, expected_prefix: str) -> tuple[UUID, str]:
    if token is None or not token.strip():
        raise AuthenticationRequiredError("An authentication token is required.")
    prefix, separator, remainder = token.strip().partition("_")
    identifier, secret_separator, secret = remainder.partition("_")
    if (
        not separator
        or not secret_separator
        or prefix != expected_prefix
        or not identifier
        or not secret
    ):
        raise AuthenticationFailedError("The authentication token has an invalid format.")
    try:
        token_id = UUID(hex=identifier)
    except ValueError as exc:
        raise AuthenticationFailedError(
            "The authentication token has an invalid identifier."
        ) from exc
    if len(secret) < 32:
        raise AuthenticationFailedError("The authentication token secret is invalid.")
    return token_id, secret


def _format_token(prefix: str, token_id: UUID, secret: str) -> str:
    return f"{prefix}_{token_id.hex}_{secret}"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _user_principal(user: User) -> Principal:
    return Principal(
        id=user.id,
        type=PrincipalType.USER,
        organisation_id=user.organisation_id,
        display_name=user.display_name,
    )


def _ip_is_allowed(source_ip: str | None, allowed_ranges: list[str]) -> bool:
    if source_ip is None:
        return False
    try:
        address = ipaddress.ip_address(source_ip)
        return any(address in ipaddress.ip_network(value, strict=False) for value in allowed_ranges)
    except ValueError:
        return False
