from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LabModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BenchStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    RESERVED = "reserved"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"


class Capability(LabModel):
    name: str
    description: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class Device(LabModel):
    name: str
    kind: str
    status: BenchStatus = BenchStatus.ONLINE


class Bench(LabModel):
    name: str
    status: BenchStatus = BenchStatus.ONLINE
    capabilities: list[str] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)


class Reservation(LabModel):
    id: str
    bench_name: str
    owner: str
    starts_at: datetime
    ends_at: datetime


class Firmware(LabModel):
    name: str
    version: str
    checksum: str | None = None


class Event(LabModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PluginMetadata(LabModel):
    name: str
    version: str
    author: str
    description: str
    capabilities: list[str] = Field(default_factory=list)


class HealthReport(LabModel):
    component: str
    status: HealthStatus
    message: str = ""
