from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from lab_platform.plugin_sdk.models import (
    ArtifactReference,
    CanFilter,
    CanFrame,
    CaptureRequest,
    CommandRequest,
    CommandResult,
    DebugStatus,
    DeviceDescriptor,
    DeviceHealth,
    DiagnosticCheck,
    FirmwareArtifact,
    FlashOptions,
    GpioRequest,
    GpioResult,
    Measurement,
    MeasureOptions,
    OperationResult,
    PluginHealth,
    PluginMetadata,
    ProbeResult,
    ProgressUpdate,
    SerialLine,
    SerialOptions,
)
from pydantic import BaseModel


class ProbeCapability(Protocol):
    async def probe(self) -> ProbeResult: ...


class ResetCapability(Protocol):
    async def reset(self) -> OperationResult: ...


class FlashCapability(Protocol):
    def flash(
        self,
        image: FirmwareArtifact,
        options: FlashOptions,
    ) -> AsyncIterator[ProgressUpdate]: ...


class SerialCapability(Protocol):
    def stream(self, options: SerialOptions) -> AsyncIterator[SerialLine]: ...


class PowerCapability(Protocol):
    async def on(self) -> None: ...

    async def off(self) -> None: ...

    async def cycle(self) -> None: ...


class CanCapability(Protocol):
    async def send(self, frame: CanFrame) -> None: ...

    def receive(self, filter: CanFilter) -> AsyncIterator[CanFrame]: ...


class CaptureCapability(Protocol):
    async def capture(self, request: CaptureRequest) -> ArtifactReference: ...


class MeasureCapability(Protocol):
    async def measure(self, quantity: str, options: MeasureOptions) -> Measurement: ...


class CommandCapability(Protocol):
    async def execute(self, request: CommandRequest) -> CommandResult: ...


class DebugCapability(Protocol):
    async def status(self) -> DebugStatus: ...


class GpioCapability(Protocol):
    async def read(self, request: GpioRequest) -> GpioResult: ...

    async def write(self, request: GpioRequest) -> GpioResult: ...


Capability = (
    ProbeCapability
    | ResetCapability
    | FlashCapability
    | SerialCapability
    | PowerCapability
    | CanCapability
    | CaptureCapability
    | MeasureCapability
    | CommandCapability
    | DebugCapability
    | GpioCapability
)


class DeviceDriver(Protocol):
    @property
    def descriptor(self) -> DeviceDescriptor: ...

    @property
    def capabilities(self) -> set[str]: ...

    async def health(self) -> DeviceHealth: ...

    async def get_capability(self, name: str) -> Capability: ...


class HardwarePlugin(Protocol):
    @property
    def metadata(self) -> PluginMetadata: ...

    async def initialize(self) -> None: ...

    async def discover(self) -> list[DeviceDescriptor]: ...

    async def health(self) -> PluginHealth: ...

    async def get_driver(self, device_id: str) -> DeviceDriver: ...

    async def shutdown(self) -> None: ...


class DiagnosticProvider(Protocol):
    async def diagnostics(self) -> list[DiagnosticCheck]: ...


class ArtifactSink(Protocol):
    async def store(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference: ...


@dataclass(frozen=True, slots=True)
class PluginContext:
    logger: logging.Logger
    agent_version: str
    artifact_sink: ArtifactSink | None = None


ConfigT = TypeVar("ConfigT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class PluginRegistration(Generic[ConfigT]):
    """Lazy, typed entry-point registration for a hardware plugin."""

    metadata: PluginMetadata
    config_model: type[ConfigT]
    factory: Callable[[ConfigT, PluginContext], HardwarePlugin]

    def validate_config(self, raw: dict[str, Any] | None = None) -> ConfigT:
        return self.config_model.model_validate(raw or {})

    def create(self, config: ConfigT, context: PluginContext) -> HardwarePlugin:
        plugin = self.factory(config, context)
        if plugin.metadata != self.metadata:
            raise ValueError("plugin instance metadata does not match its registration")
        return plugin


class BaseHardwarePlugin:
    """Small lifecycle base suitable for templates and simple plugins."""

    def __init__(self, metadata: PluginMetadata) -> None:
        self._metadata = metadata
        self._initialized = False

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def initialize(self) -> None:
        self._initialized = True

    async def discover(self) -> list[DeviceDescriptor]:
        return []

    async def health(self) -> PluginHealth:
        from lab_platform.plugin_sdk.models import PluginHealthStatus

        return PluginHealth(
            status=(
                PluginHealthStatus.HEALTHY if self._initialized else PluginHealthStatus.UNKNOWN
            ),
            message="Plugin initialized" if self._initialized else "Plugin is not initialized",
        )

    async def get_driver(self, device_id: str) -> DeviceDriver:
        from lab_platform.plugin_sdk.errors import DeviceUnavailableError

        raise DeviceUnavailableError(
            f"Device {device_id!r} was not discovered",
            device_id=device_id,
        )

    async def shutdown(self) -> None:
        self._initialized = False


HealthCallback = Callable[[], Awaitable[PluginHealth]]


__all__ = [
    "ArtifactSink",
    "BaseHardwarePlugin",
    "CanCapability",
    "Capability",
    "CaptureCapability",
    "CommandCapability",
    "DebugCapability",
    "DeviceDriver",
    "DiagnosticProvider",
    "FlashCapability",
    "GpioCapability",
    "HardwarePlugin",
    "MeasureCapability",
    "PluginContext",
    "PluginRegistration",
    "PowerCapability",
    "ProbeCapability",
    "ResetCapability",
    "SerialCapability",
]
