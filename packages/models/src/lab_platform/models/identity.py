from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from lab_platform.models.domain import LabModel, utc_now
from pydantic import Field, ValidationInfo, field_validator, model_validator


class OrganisationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    LOCKED = "LOCKED"
    DELETED = "DELETED"


class AuthenticationSource(StrEnum):
    LOCAL = "LOCAL"
    OIDC = "OIDC"


class ServiceAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    REVOKED = "REVOKED"


class PrincipalType(StrEnum):
    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


class OrganisationRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


class TeamRole(StrEnum):
    MANAGER = "MANAGER"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


class RoleName(StrEnum):
    ORGANISATION_OWNER = "ORGANISATION_OWNER"
    ORGANISATION_ADMIN = "ORGANISATION_ADMIN"
    LAB_ADMIN = "LAB_ADMIN"
    OPERATOR = "OPERATOR"
    WORKFLOW_RUNNER = "WORKFLOW_RUNNER"
    RESERVER = "RESERVER"
    VIEWER = "VIEWER"
    AUDITOR = "AUDITOR"


class RoleSubjectType(StrEnum):
    USER = "USER"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"
    TEAM = "TEAM"


class ResourceType(StrEnum):
    ORGANISATION = "ORGANISATION"
    AGENT = "AGENT"
    BENCH = "BENCH"
    WORKFLOW = "WORKFLOW"


class AuditOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"


class BenchVisibility(StrEnum):
    PRIVATE = "PRIVATE"
    ORGANISATION = "ORGANISATION"
    RESTRICTED = "RESTRICTED"


class WorkflowVisibility(StrEnum):
    ORGANISATION = "ORGANISATION"
    RESTRICTED = "RESTRICTED"
    ADMIN_ONLY = "ADMIN_ONLY"


class Organisation(LabModel):
    id: UUID = Field(default_factory=uuid4)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    status: OrganisationStatus = OrganisationStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _as_utc(value, "organisation")


class User(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    status: UserStatus = UserStatus.ACTIVE
    authentication_source: AuthenticationSource = AuthenticationSource.LOCAL
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_login_at: datetime | None = None

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("created_at", "updated_at", "last_login_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_optional_utc(value, "user")


class PasswordCredential(LabModel):
    user_id: UUID
    password_hash: str = Field(min_length=32, repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _as_utc(value, "password credential")


class ServiceAccount(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: ServiceAccountStatus = ServiceAccountStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None

    @field_validator("created_at", "updated_at", "last_used_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_optional_utc(value, "service account")


class Principal(LabModel):
    id: UUID
    type: PrincipalType
    organisation_id: UUID
    display_name: str = Field(min_length=1, max_length=200)


class ActorContext(LabModel):
    principal_id: UUID
    principal_type: PrincipalType
    display_name: str = Field(min_length=1, max_length=200)
    organisation_id: UUID
    authorisation_snapshot_id: UUID | None = None


class OrganisationMembership(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    user_id: UUID
    role: OrganisationRole
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value, "organisation membership")


class Team(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _as_utc(value, "team")


class TeamMembership(LabModel):
    id: UUID = Field(default_factory=uuid4)
    team_id: UUID
    user_id: UUID
    role: TeamRole = TeamRole.MEMBER
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value, "team membership")


class RoleAssignment(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    subject_type: RoleSubjectType
    subject_id: UUID
    role: RoleName
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=500)
    created_by: UUID
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_optional_utc(value, "role assignment")

    @model_validator(mode="after")
    def validate_expiry(self) -> RoleAssignment:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class AuthorisationResource(LabModel):
    type: ResourceType
    id: str = Field(min_length=1, max_length=500)
    organisation_id: UUID
    parent_agent_id: UUID | None = None


class UserSession(LabModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    organisation_id: UUID
    secret_hash: str = Field(min_length=64, max_length=64, repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    maximum_expires_at: datetime
    last_seen_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None
    user_agent: str | None = Field(default=None, max_length=1000)
    ip_address: str | None = Field(default=None, max_length=64)

    @field_validator("user_agent", mode="before")
    @classmethod
    def sanitize_user_agent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _truncate_audit_text(_redact_audit_text(value), 1000)

    @field_validator(
        "created_at",
        "expires_at",
        "maximum_expires_at",
        "last_seen_at",
        "revoked_at",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_optional_utc(value, "user session")

    @model_validator(mode="after")
    def validate_lifetime(self) -> UserSession:
        if not (self.created_at < self.expires_at <= self.maximum_expires_at):
            raise ValueError("session expiry must follow creation and not exceed its maximum")
        if not self.created_at <= self.last_seen_at <= self.maximum_expires_at:
            raise ValueError("last_seen_at must fall within the session lifetime")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at must not precede session creation")
        return self


class ApiCredential(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    principal_id: UUID
    principal_type: PrincipalType
    name: str = Field(min_length=1, max_length=200)
    secret_hash: str = Field(min_length=64, max_length=64, repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    allowed_ip_ranges: list[str] = Field(default_factory=list, max_length=64)
    permission_restrictions: set[str] | None = None

    @field_validator("created_at", "expires_at", "revoked_at", "last_used_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_optional_utc(value, "API credential")

    @field_validator("allowed_ip_ranges")
    @classmethod
    def validate_ip_ranges(cls, values: list[str]) -> list[str]:
        try:
            return [str(ipaddress.ip_network(value, strict=False)) for value in values]
        except ValueError as exc:
            raise ValueError("allowed_ip_ranges must contain valid IPv4 or IPv6 CIDRs") from exc

    @model_validator(mode="after")
    def validate_expiry(self) -> ApiCredential:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at must not precede creation")
        if self.last_used_at is not None and self.last_used_at < self.created_at:
            raise ValueError("last_used_at must not precede creation")
        return self


class AuthenticationContext(LabModel):
    principal: Principal
    session_id: UUID | None = None
    credential_id: UUID | None = None
    permission_restrictions: set[str] | None = None
    authorisation_snapshot_id: UUID | None = None


class AuthorisationSnapshot(LabModel):
    id: UUID = Field(default_factory=uuid4)
    principal_id: UUID
    permission: str = Field(min_length=1, max_length=200)
    resource_type: ResourceType | Literal["CI_SESSION"]
    resource_id: str = Field(min_length=1, max_length=500)
    granted_by_assignments: list[UUID] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utc_now)

    @field_validator("evaluated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value, "authorisation snapshot")


class ReservationOwner(LabModel):
    principal_id: UUID
    principal_type: PrincipalType
    display_name: str = Field(min_length=1, max_length=200)


class BenchAccessPolicy(LabModel):
    bench_id: str = Field(min_length=1, max_length=500)
    visibility: BenchVisibility = BenchVisibility.ORGANISATION
    reservation_role: RoleName | None = None
    operation_role: RoleName | None = None
    allowed_team_ids: set[UUID] = Field(default_factory=set)


class WorkflowAccessPolicy(LabModel):
    workflow_id: str = Field(min_length=1, max_length=500)
    visibility: WorkflowVisibility = WorkflowVisibility.ORGANISATION


_SENSITIVE_AUDIT_METADATA_PARTS = frozenset(
    {
        "access_key",
        "accesskey",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "firmware",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "privatekey",
        "pwd",
        "refresh_token",
        "secret",
        "serial_log",
        "token",
    }
)
_LAB_SECRET_PATTERN = re.compile(r"(?i)\b(?:lp|lps|lpe|lpa|lpt)_[A-Za-z0-9_-]{20,}\b")
_BEARER_SECRET_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")
_JWT_SECRET_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_NAMED_AUDIT_VALUE_PATTERN = re.compile(
    r"(?ix)"
    r"(?<![A-Za-z0-9_-])"
    r"(?P<prefix>"
    r"(?P<quote>[\"']?)"
    r"(?P<key>"
    r"[A-Za-z0-9_-]*"
    r"(?:access[-_]?key|api[-_]?key|authorization|cookie|firmware|passphrase|passwd|password|pwd|"
    r"private[-_]?key|secret|serial[-_]log|token)"
    r"[A-Za-z0-9_-]*"
    r")"
    r"(?P=quote)\s*[:=]\s*"
    r")"
    r"(?P<value>"
    r"\[REDACTED\]"
    r"|\"(?:\\.|[^\"\\])*\""
    r"|'(?:\\.|[^'\\])*'"
    r"|[^&;,\r\n}\]]+"
    r")"
)
_PRIVATE_KEY_MARKER = re.compile(r"(?i)-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY(?: [A-Z0-9]+)*-----")
_AUDIT_REDACTION = "[REDACTED]"
_AUDIT_TEXT_LIMITS = {
    "actor_display_name": 200,
    "action": 200,
    "resource_type": 100,
    "resource_id": 500,
    "source_ip": 64,
    "user_agent": 1000,
    "reason": 2000,
}


class AuditEvent(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID
    timestamp: datetime = Field(default_factory=utc_now)
    actor_type: PrincipalType | None = None
    actor_id: UUID | None = None
    actor_display_name: str | None = Field(default=None, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: str | None = Field(default=None, max_length=500)
    outcome: AuditOutcome
    request_id: UUID | None = None
    source_ip: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator(
        "actor_display_name",
        "action",
        "resource_type",
        "resource_id",
        "source_ip",
        "user_agent",
        "reason",
        mode="before",
    )
    @classmethod
    def redact_secret_shaped_text(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return None
        redacted = _redact_audit_text(value)
        field_name = info.field_name
        if field_name is None:
            raise AssertionError("Audit text validation requires a field name")
        return _truncate_audit_text(redacted, _AUDIT_TEXT_LIMITS[field_name])

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _contains_sensitive_audit_metadata_key(value):
            raise ValueError("audit metadata must not contain secrets")
        redacted = _redact_audit_metadata_value(value)
        assert isinstance(redacted, dict)
        encoded = json.dumps(redacted, separators=(",", ":"), default=str)
        if len(encoded.encode("utf-8")) > 8 * 1024:
            raise ValueError("audit metadata must not exceed 8 KiB")
        return redacted


def _contains_sensitive_audit_metadata_key(value: object) -> bool:
    """Inspect nested metadata so containers cannot conceal credential fields."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_AUDIT_METADATA_PARTS):
                return True
            if _contains_sensitive_audit_metadata_key(item):
                return True
        return False
    if isinstance(value, list | tuple | set | frozenset):
        return any(_contains_sensitive_audit_metadata_key(item) for item in value)
    return False


def _redact_audit_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_audit_text(value)
    if isinstance(value, dict):
        return {key: _redact_audit_metadata_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_audit_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_audit_metadata_value(item) for item in value)
    if isinstance(value, set):
        return {_redact_audit_metadata_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_redact_audit_metadata_value(item) for item in value)
    return value


def _redact_audit_text(value: str) -> str:
    if _PRIVATE_KEY_MARKER.search(value):
        return _AUDIT_REDACTION
    redacted = _LAB_SECRET_PATTERN.sub(_AUDIT_REDACTION, value)
    redacted = _BEARER_SECRET_PATTERN.sub(f"Bearer {_AUDIT_REDACTION}", redacted)
    redacted = _JWT_SECRET_PATTERN.sub(_AUDIT_REDACTION, redacted)
    return _NAMED_AUDIT_VALUE_PATTERN.sub(_redact_named_audit_value, redacted)


def _redact_named_audit_value(match: re.Match[str]) -> str:
    """Redact explicit key/value forms while leaving ordinary prose untouched."""

    normalized_key = match.group("key").casefold().replace("-", "_")
    if not any(part in normalized_key for part in _SENSITIVE_AUDIT_METADATA_PARTS):
        return match.group(0)
    if match.group("value") == _AUDIT_REDACTION:
        return match.group(0)
    return f"{match.group('prefix')}{_AUDIT_REDACTION}"


def _truncate_audit_text(value: str, maximum_length: int) -> str:
    """Bound already-redacted text without cutting through a redaction marker."""

    if len(value) <= maximum_length:
        return value
    prefix_length = maximum_length - 1
    marker_start = value.rfind(
        _AUDIT_REDACTION,
        0,
        maximum_length + len(_AUDIT_REDACTION),
    )
    if 0 <= marker_start < prefix_length and marker_start + len(_AUDIT_REDACTION) > prefix_length:
        preserved_prefix = maximum_length - len(_AUDIT_REDACTION) - 1
        return f"{value[:preserved_prefix]}…{_AUDIT_REDACTION}"
    return f"{value[:prefix_length]}…"


class LoginAttempt(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_slug: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=100)
    ip_address: str | None = Field(default=None, max_length=64)
    attempted_at: datetime = Field(default_factory=utc_now)
    succeeded: bool = False

    @field_validator("organisation_slug", "username", mode="before")
    @classmethod
    def normalize_identity(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("attempted_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value, "login attempt")


def _as_utc(value: datetime, model: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{model} timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _as_optional_utc(value: datetime | None, model: str) -> datetime | None:
    return _as_utc(value, model) if value is not None else None
