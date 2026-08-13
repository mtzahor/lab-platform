from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from lab_platform.models.domain import LEGACY_ORGANISATION_ID, LabModel, utc_now
from pydantic import Field, field_validator, model_validator

_AGENT_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_LABELS = 128
_MAX_LABEL_NAME_LENGTH = 200
_MAX_LABEL_VALUE_LENGTH = 500


class AgentStatus(StrEnum):
    PENDING = "PENDING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"
    DRAINED = "DRAINED"
    REVOKED = "REVOKED"
    INCOMPATIBLE = "INCOMPATIBLE"


class EnrollmentStatus(StrEnum):
    PENDING = "PENDING"
    ENROLLED = "ENROLLED"
    REVOKED = "REVOKED"


class AgentCredentialKind(StrEnum):
    OPAQUE_TOKEN = "OPAQUE_TOKEN"


class AgentRecord(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID = LEGACY_ORGANISATION_ID
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    status: AgentStatus = AgentStatus.PENDING
    version: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(min_length=1, max_length=100)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    labels: dict[str, str] = Field(default_factory=dict, max_length=_MAX_LABELS)
    registered_at: datetime = Field(default_factory=utc_now)
    last_connected_at: datetime | None = None
    last_seen_at: datetime | None = None
    disconnected_at: datetime | None = None
    certificate_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    enrollment_status: EnrollmentStatus = EnrollmentStatus.PENDING
    revoked_at: datetime | None = None

    @field_validator("slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if _AGENT_SLUG_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "Agent slug must contain only lowercase letters, digits, and internal hyphens"
            )
        return normalized

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        return _normalized_labels(value, field="Agent labels")

    @field_validator(
        "registered_at",
        "last_connected_at",
        "last_seen_at",
        "disconnected_at",
        "revoked_at",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Agent timestamp")

    @model_validator(mode="after")
    def validate_state_and_timestamps(self) -> AgentRecord:
        for field, value in (
            ("last_connected_at", self.last_connected_at),
            ("last_seen_at", self.last_seen_at),
            ("disconnected_at", self.disconnected_at),
            ("revoked_at", self.revoked_at),
        ):
            if value is not None and value < self.registered_at:
                raise ValueError(f"{field} cannot be earlier than registered_at")

        if self.last_seen_at is not None:
            if self.last_connected_at is None:
                raise ValueError("last_seen_at requires last_connected_at")
            if self.last_seen_at < self.last_connected_at:
                raise ValueError("last_seen_at cannot be earlier than last_connected_at")

        if self.disconnected_at is not None:
            if self.last_connected_at is None:
                raise ValueError("disconnected_at requires last_connected_at")
            if self.disconnected_at < self.last_connected_at:
                raise ValueError("disconnected_at cannot be earlier than last_connected_at")
            if self.last_seen_at is not None and self.disconnected_at < self.last_seen_at:
                raise ValueError("disconnected_at cannot be earlier than last_seen_at")

        revoked = self.status is AgentStatus.REVOKED
        enrollment_revoked = self.enrollment_status is EnrollmentStatus.REVOKED
        if revoked != enrollment_revoked or revoked != (self.revoked_at is not None):
            raise ValueError(
                "REVOKED status, REVOKED enrollment status, and revoked_at must be set together"
            )
        return self


class AgentEnrollmentToken(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID = LEGACY_ORGANISATION_ID
    name: str = Field(min_length=1, max_length=200)
    token_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None
    allowed_labels: dict[str, str] = Field(default_factory=dict, max_length=_MAX_LABELS)
    used_by_agent_id: UUID | None = None
    enrollment_request_id: UUID | None = None

    @field_validator("token_hash", mode="before")
    @classmethod
    def normalize_token_hash(cls, value: object) -> object:
        return _normalized_sha256(value, field="token_hash")

    @field_validator("allowed_labels", mode="before")
    @classmethod
    def normalize_allowed_labels(cls, value: object) -> object:
        return _normalized_labels(value, field="allowed labels")

    @field_validator("created_at", "expires_at", "used_at", "revoked_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Enrollment token timestamp")

    @model_validator(mode="after")
    def validate_timestamps_and_usage(self) -> AgentEnrollmentToken:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.used_at is not None:
            if self.used_at < self.created_at:
                raise ValueError("used_at cannot be earlier than created_at")
            if self.used_at >= self.expires_at:
                raise ValueError("used_at must be earlier than expires_at")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at cannot be earlier than created_at")
        if (
            self.used_at is not None
            and self.revoked_at is not None
            and self.revoked_at < self.used_at
        ):
            raise ValueError("revoked_at cannot be earlier than used_at")

        usage_fields_present = (
            self.used_at is not None,
            self.used_by_agent_id is not None,
            self.enrollment_request_id is not None,
        )
        if any(usage_fields_present) and not all(usage_fields_present):
            raise ValueError(
                "used_at, used_by_agent_id, and enrollment_request_id must be set together"
            )
        return self


class AgentCredential(LabModel):
    id: UUID = Field(default_factory=uuid4)
    organisation_id: UUID = LEGACY_ORGANISATION_ID
    agent_id: UUID
    kind: AgentCredentialKind = AgentCredentialKind.OPAQUE_TOKEN
    credential_hash: str = Field(min_length=64, max_length=64)
    version: int = Field(default=1, ge=1, strict=True)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @field_validator("credential_hash", mode="before")
    @classmethod
    def normalize_credential_hash(cls, value: object) -> object:
        return _normalized_sha256(value, field="credential_hash")

    @field_validator("created_at", "expires_at", "revoked_at", "last_used_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value, field="Agent credential timestamp")

    @model_validator(mode="after")
    def validate_timestamps(self) -> AgentCredential:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at cannot be earlier than created_at")
        if self.last_used_at is not None:
            if self.last_used_at < self.created_at:
                raise ValueError("last_used_at cannot be earlier than created_at")
            if self.expires_at is not None and self.last_used_at >= self.expires_at:
                raise ValueError("last_used_at must be earlier than expires_at")
            if self.revoked_at is not None and self.last_used_at > self.revoked_at:
                raise ValueError("last_used_at cannot be later than revoked_at")
        return self


def _as_utc(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _normalized_sha256(value: object, *, field: str) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be a 64-character hexadecimal SHA-256 hash")
    return normalized


def _normalized_labels(value: object, *, field: str) -> object:
    if not isinstance(value, dict):
        return value
    if len(value) > _MAX_LABELS:
        raise ValueError(f"{field} cannot contain more than {_MAX_LABELS} entries")

    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"{field} names must be non-empty strings")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"{field} values must be non-empty strings")
        key = raw_key.strip()
        label_value = raw_value.strip()
        if len(key) > _MAX_LABEL_NAME_LENGTH:
            raise ValueError(f"{field} names cannot exceed {_MAX_LABEL_NAME_LENGTH} characters")
        if len(label_value) > _MAX_LABEL_VALUE_LENGTH:
            raise ValueError(f"{field} values cannot exceed {_MAX_LABEL_VALUE_LENGTH} characters")
        if key in normalized:
            raise ValueError(f"duplicate {field.lower()} name after normalization: {key}")
        normalized[key] = label_value
    return normalized
