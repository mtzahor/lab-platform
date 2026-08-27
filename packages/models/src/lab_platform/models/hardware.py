from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HardwareModel(BaseModel):
    """Immutable public models used by resource-aware hardware integrations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CoreCapability(StrEnum):
    PROBE = "probe"
    RESET = "reset"
    POWER = "power"
    SERIAL = "serial"
    FLASH = "flash"
    DEBUG = "debug"
    GPIO = "gpio"
    CAN = "can"
    CAPTURE = "capture"
    MEASURE = "measure"
    COMMAND = "command"


class ResourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class ResourceSharingMode(StrEnum):
    """How a physical resource participates in concurrent bench reservations."""

    EXCLUSIVE = "exclusive"
    SHARED = "shared"
    CHANNEL = "channel"


class ResourceBindingRole(StrEnum):
    TARGET = "target"
    POWER = "power"
    SERIAL = "serial"
    DEBUG = "debug"
    CAN = "can"
    INSTRUMENT = "instrument"
    AUXILIARY = "auxiliary"


class ResourceLockOwnerType(StrEnum):
    RESERVATION = "reservation"
    OPERATION = "operation"
    WORKFLOW = "workflow"
    MAINTENANCE = "maintenance"


class HardwareResource(HardwareModel):
    id: str = Field(min_length=1, max_length=300)
    agent_id: UUID
    plugin: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    health: ResourceHealthStatus = ResourceHealthStatus.UNKNOWN
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sharing: ResourceSharingMode = ResourceSharingMode.EXCLUSIVE
    channels: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        normalized: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("resource capabilities must be non-empty strings")
            normalized.add(item.strip().casefold())
        return frozenset(normalized)

    @field_validator("channels", mode="before")
    @classmethod
    def normalize_channels(cls, value: object) -> object:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        normalized: set[str] = set()
        for item in value:
            if not isinstance(item, (str, int)) or not str(item).strip():
                raise ValueError("resource channel identifiers must be non-empty")
            normalized.add(str(item).strip())
        return frozenset(normalized)

    @model_validator(mode="after")
    def validate_sharing(self) -> HardwareResource:
        if self.sharing is ResourceSharingMode.CHANNEL and not self.channels:
            raise ValueError("channel-shared resources must declare at least one channel")
        if self.sharing is not ResourceSharingMode.CHANNEL and self.channels:
            raise ValueError("only channel-shared resources may declare channels")
        return self


class BenchResourceBinding(HardwareModel):
    role: ResourceBindingRole
    resource_id: str = Field(min_length=1, max_length=300)
    channel: str | None = Field(default=None, min_length=1, max_length=100)
    required: bool = True
    capabilities: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        return HardwareResource.normalize_capabilities(value)


class BenchComposition(HardwareModel):
    id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    resources: tuple[BenchResourceBinding, ...] = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_bindings(self) -> BenchComposition:
        keys = [(binding.resource_id, binding.channel) for binding in self.resources]
        if len(keys) != len(set(keys)):
            raise ValueError("a resource/channel may be bound to a bench only once")
        if not any(binding.role is ResourceBindingRole.TARGET for binding in self.resources):
            raise ValueError("a composed bench requires a target resource")
        return self


class ResourceLock(HardwareModel):
    resource_id: str = Field(min_length=1, max_length=300)
    owner_type: ResourceLockOwnerType
    owner_id: UUID
    acquired_at: datetime
    expires_at: datetime | None = None
    channel: str | None = Field(default=None, min_length=1, max_length=100)
    fencing_token: int = Field(default=1, ge=1)

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("resource lock timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def expiry_after_acquisition(self) -> ResourceLock:
        if self.expires_at is not None and self.expires_at <= self.acquired_at:
            raise ValueError("resource lock expiry must be later than acquisition")
        return self


__all__ = [
    "BenchComposition",
    "BenchResourceBinding",
    "CoreCapability",
    "HardwareResource",
    "ResourceBindingRole",
    "ResourceHealthStatus",
    "ResourceLock",
    "ResourceLockOwnerType",
    "ResourceSharingMode",
]
