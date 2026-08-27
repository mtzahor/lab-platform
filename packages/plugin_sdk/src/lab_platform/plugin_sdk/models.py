from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from lab_platform.plugin_sdk.constants import PLUGIN_API_VERSION
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_PLUGIN_API_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_VERSION_PATTERN = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:(?:-|\.)?(?:a|alpha|b|beta|pre|preview|rc|dev|nightly)(?:[.-]?[0-9]+)?)?"
    r"(?:\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class SdkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CapabilityName(StrEnum):
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


class PluginHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class DeviceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class DiagnosticStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    SKIPPED = "skipped"


class PluginRuntimeStatus(StrEnum):
    DISCOVERED = "discovered"
    DISABLED = "disabled"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"
    STOPPED = "stopped"


class CompatibilityStatus(StrEnum):
    COMPATIBLE = "compatible"
    PLUGIN_API_INCOMPATIBLE = "plugin_api_incompatible"
    AGENT_VERSION_INCOMPATIBLE = "agent_version_incompatible"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    INVALID_METADATA = "invalid_metadata"


class PluginMetadata(SdkModel):
    """Metadata every Plugin API 1.x registration exposes.

    Defaults on the newly added compatibility fields keep pre-1.0 in-tree plugins
    loadable during the migration. New plugin scaffolds write every field explicitly.
    """

    name: str = Field(min_length=1, max_length=100, pattern=_NAME_PATTERN)
    version: str
    plugin_api_version: str = PLUGIN_API_VERSION
    vendor: str | None = Field(default=None, max_length=200)
    author: str | None = Field(default=None, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    supported_platforms: list[str] = Field(default_factory=lambda: ["any"])
    supported_devices: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    minimum_agent_version: str = "0.0.0"
    maximum_agent_version: str | None = None

    @field_validator("version", "minimum_agent_version", "maximum_agent_version")
    @classmethod
    def validate_application_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if _VERSION_PATTERN.fullmatch(normalized) is None:
            raise ValueError(f"unsupported semantic version: {value!r}")
        return normalized.removeprefix("v")

    @field_validator("plugin_api_version")
    @classmethod
    def validate_plugin_api_version(cls, value: str) -> str:
        normalized = value.strip()
        if _PLUGIN_API_PATTERN.fullmatch(normalized) is None:
            raise ValueError("plugin_api_version must use MAJOR.MINOR form")
        return normalized

    @field_validator("supported_platforms", mode="before")
    @classmethod
    def normalize_platforms(cls, value: object) -> object:
        return _normalize_unique_strings(value, field="supported platform", casefold=True)

    @field_validator("supported_devices", mode="before")
    @classmethod
    def normalize_devices(cls, value: object) -> object:
        return _normalize_unique_strings(value, field="supported device", casefold=False)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        normalized = _normalize_unique_strings(value, field="capability", casefold=True)
        if not isinstance(normalized, list):
            return normalized
        result: list[str] = []
        for item in normalized:
            capability = "flash" if item == "firmware" else item
            if _CAPABILITY_PATTERN.fullmatch(capability) is None:
                raise ValueError(f"invalid capability name: {item!r}")
            if capability in result:
                raise ValueError(f"duplicate capability: {capability}")
            result.append(capability)
        return result

    @model_validator(mode="after")
    def validate_agent_range(self) -> PluginMetadata:
        if self.maximum_agent_version is None:
            return self
        if _version_key(self.maximum_agent_version) < _version_key(self.minimum_agent_version):
            raise ValueError("maximum_agent_version cannot be older than minimum_agent_version")
        return self


class PluginCompatibility(SdkModel):
    compatible: bool
    status: CompatibilityStatus
    message: str
    agent_version: str
    supported_plugin_api_version: str


class PluginHealth(SdkModel):
    status: PluginHealthStatus
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DeviceDescriptor(SdkModel):
    id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    type: str = Field(min_length=1, max_length=200)
    serial_number: str | None = None
    capabilities: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        if not isinstance(value, (set, frozenset, list, tuple)):
            return value
        return {
            "flash" if str(item).strip().casefold() == "firmware" else str(item).strip().casefold()
            for item in value
        }


class DeviceHealth(SdkModel):
    status: DeviceHealthStatus
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticCheck(SdkModel):
    name: str = Field(min_length=1, max_length=200)
    status: DiagnosticStatus
    message: str
    remediation: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PluginDiagnosticReport(SdkModel):
    plugin: str
    status: PluginRuntimeStatus
    metadata: PluginMetadata | None = None
    checks: list[DiagnosticCheck] = Field(default_factory=list)
    devices: list[DeviceDescriptor] = Field(default_factory=list)


class PluginRuntimeInfo(SdkModel):
    name: str
    source: str
    status: PluginRuntimeStatus
    metadata: PluginMetadata | None = None
    error_code: str | None = None
    error_message: str | None = None
    device_count: int = Field(default=0, ge=0)


class PluginFailure(SdkModel):
    plugin: str
    stage: Literal[
        "discovery", "configuration", "compatibility", "initialize", "health", "shutdown"
    ]
    code: str
    message: str


class PluginLoadReport(SdkModel):
    loaded: list[str] = Field(default_factory=list)
    failures: list[PluginFailure] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


class OperationResult(SdkModel):
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResult(SdkModel):
    online: bool
    device_type: str | None = None
    serial_number: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class FirmwareArtifact(SdkModel):
    filename: str
    local_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    version: str | None = None


class FlashOptions(SdkModel):
    verify: bool = True
    reset_after: bool = True
    timeout_seconds: float = Field(default=120, gt=0, le=86_400)
    values: dict[str, Any] = Field(default_factory=dict)


class ProgressUpdate(SdkModel):
    percent: int = Field(ge=0, le=100)
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SerialOptions(SdkModel):
    baud_rate: int | None = Field(default=None, ge=300, le=12_000_000)
    timeout_seconds: float = Field(default=10, gt=0, le=86_400)
    until_pattern: str | None = None
    max_lines: int | None = Field(default=500, ge=1, le=1_000_000)


class SerialLine(SdkModel):
    timestamp: datetime = Field(default_factory=utc_now)
    text: str
    stream: Literal["serial", "stdout", "stderr"] = "serial"


class CanFrame(SdkModel):
    arbitration_id: int = Field(ge=0, le=0x1FFFFFFF)
    data: bytes = Field(default=b"", max_length=64)
    extended: bool = False
    remote: bool = False
    timestamp: datetime | None = None


class CanFilter(SdkModel):
    arbitration_id: int | None = Field(default=None, ge=0, le=0x1FFFFFFF)
    mask: int | None = Field(default=None, ge=0, le=0x1FFFFFFF)
    extended: bool | None = None


class TriggerDefinition(SdkModel):
    type: str
    channel: str | None = None
    value: float | str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class CaptureRequest(SdkModel):
    duration_seconds: float = Field(gt=0, le=86_400)
    channels: list[str] = Field(default_factory=list)
    sample_rate: float | None = Field(default=None, gt=0)
    trigger: TriggerDefinition | None = None
    format: str | None = None


class ArtifactReference(SdkModel):
    id: str
    uri: str | None = None
    media_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeasureOptions(SdkModel):
    channel: str | None = None
    samples: int = Field(default=1, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=10, gt=0, le=86_400)
    values: dict[str, Any] = Field(default_factory=dict)


class Measurement(SdkModel):
    quantity: str
    value: float
    unit: str
    timestamp: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandRequest(SdkModel):
    command: str
    arguments: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=86_400)


class CommandResult(SdkModel):
    exit_code: int
    stdout: list[str] = Field(default_factory=list)
    stderr: list[str] = Field(default_factory=list)


class DebugStatus(SdkModel):
    available: bool
    connected: bool = False
    endpoint: str | None = None
    message: str = ""


class GpioRequest(SdkModel):
    pin: str
    value: bool | None = None


class GpioResult(SdkModel):
    pin: str
    value: bool


def _normalize_unique_strings(value: object, *, field: str, casefold: bool) -> object:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return value
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field} names must be non-empty strings")
        item = raw.strip().casefold() if casefold else raw.strip()
        if item in result:
            raise ValueError(f"duplicate {field}: {item}")
        result.append(item)
    return result


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:  # pragma: no cover - fields validate first
        raise ValueError(value)
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    lowered = value.casefold()
    rank = 4
    for marker, candidate in (
        ("dev", 0),
        ("nightly", 0),
        ("alpha", 1),
        ("a", 1),
        ("beta", 2),
        ("b", 2),
        ("preview", 2),
        ("pre", 2),
        ("rc", 3),
    ):
        if re.search(rf"(?:[-.]|(?<=\d)){marker}", lowered):
            rank = candidate
            break
    suffix = re.search(r"(?:alpha|beta|preview|nightly|dev|pre|rc|a|b)[.-]?([0-9]+)", lowered)
    return major, minor, patch, rank, int(suffix.group(1)) if suffix else 0


__all__ = [
    "ArtifactReference",
    "CanFilter",
    "CanFrame",
    "CapabilityName",
    "CaptureRequest",
    "CommandRequest",
    "CommandResult",
    "CompatibilityStatus",
    "DebugStatus",
    "DeviceDescriptor",
    "DeviceHealth",
    "DeviceHealthStatus",
    "DiagnosticCheck",
    "DiagnosticStatus",
    "FirmwareArtifact",
    "FlashOptions",
    "GpioRequest",
    "GpioResult",
    "MeasureOptions",
    "Measurement",
    "OperationResult",
    "PluginCompatibility",
    "PluginDiagnosticReport",
    "PluginFailure",
    "PluginHealth",
    "PluginHealthStatus",
    "PluginLoadReport",
    "PluginMetadata",
    "PluginRuntimeInfo",
    "PluginRuntimeStatus",
    "ProbeResult",
    "ProgressUpdate",
    "SerialLine",
    "SerialOptions",
    "TriggerDefinition",
]
