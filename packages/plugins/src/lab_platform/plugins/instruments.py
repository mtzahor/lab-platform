from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol, cast
from uuid import uuid4

from lab_platform.plugin_sdk import (
    ArtifactReference,
    Capability,
    CaptureRequest,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    Measurement,
    MeasureOptions,
    PluginMetadata,
    PluginOperationError,
    UnsupportedCapabilityError,
)
from lab_platform.plugins.hardware_base import StaticHardwarePlugin


class InstrumentTransport(Protocol):
    async def health(self) -> tuple[bool, str]: ...

    async def measure(self, quantity: str, options: MeasureOptions) -> Measurement: ...

    async def capture(self, request: CaptureRequest) -> ArtifactReference: ...


class PowerSupplyTransport(Protocol):
    async def health(self) -> tuple[bool, str]: ...

    async def set_output(self, channel: str, enabled: bool) -> None: ...

    async def measure(self, quantity: str, options: MeasureOptions) -> Measurement: ...


class MemoryInstrumentTransport:
    """Fake instrument useful to plugin authors and deterministic SimLab tests."""

    def __init__(self, measurements: dict[str, tuple[float, str]] | None = None) -> None:
        self.measurements = dict(measurements or {})
        self.outputs: dict[str, bool] = {}
        self.captures: list[CaptureRequest] = []

    async def health(self) -> tuple[bool, str]:
        return True, "Memory instrument is available"

    async def set_output(self, channel: str, enabled: bool) -> None:
        self.outputs[channel] = enabled

    async def measure(self, quantity: str, options: MeasureOptions) -> Measurement:
        try:
            value, unit = self.measurements[quantity.casefold()]
        except KeyError as exc:
            raise PluginOperationError(
                f"Measurement quantity {quantity!r} is unavailable.",
                quantity=quantity,
            ) from exc
        return Measurement(
            quantity=quantity.casefold(),
            value=value,
            unit=unit,
            metadata={"channel": options.channel} if options.channel else {},
        )

    async def capture(self, request: CaptureRequest) -> ArtifactReference:
        self.captures.append(request)
        return ArtifactReference(
            id=f"memory-capture-{uuid4()}",
            media_type="application/json",
            metadata={
                "duration_seconds": request.duration_seconds,
                "channels": request.channels,
                "format": request.format or "json",
            },
        )


class MeasureAdapter:
    def __init__(self, transport: InstrumentTransport | PowerSupplyTransport) -> None:
        self._transport = transport

    async def measure(self, quantity: str, options: MeasureOptions) -> Measurement:
        return await self._transport.measure(quantity, options)


class CaptureAdapter:
    def __init__(self, transport: InstrumentTransport) -> None:
        self._transport = transport

    async def capture(self, request: CaptureRequest) -> ArtifactReference:
        return await self._transport.capture(request)


class PowerSupplyCapability:
    def __init__(
        self,
        transport: PowerSupplyTransport,
        channel: str,
        *,
        cycle_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if cycle_delay_seconds < 0:
            raise ValueError("power-cycle delay cannot be negative")
        self._transport = transport
        self._channel = channel
        self._cycle_delay_seconds = cycle_delay_seconds
        self._sleep = sleep

    async def on(self) -> None:
        await self._transport.set_output(self._channel, True)

    async def off(self) -> None:
        await self._transport.set_output(self._channel, False)

    async def cycle(self) -> None:
        await self.off()
        await self._sleep(self._cycle_delay_seconds)
        await self.on()


class InstrumentDriver:
    def __init__(
        self,
        *,
        device_id: str,
        name: str,
        category: str,
        transport: InstrumentTransport,
    ) -> None:
        if category not in {"logic-analyser", "oscilloscope", "instrument"}:
            raise ValueError("unsupported generic instrument category")
        self._transport = transport
        self._descriptor = DeviceDescriptor(
            id=device_id,
            name=name,
            type=category,
            capabilities={"measure", "capture"},
            metadata={"category": category},
        )
        self._measure = MeasureAdapter(transport)
        self._capture = CaptureAdapter(transport)

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return {"measure", "capture"}

    async def health(self) -> DeviceHealth:
        healthy, message = await self._transport.health()
        return DeviceHealth(
            status=DeviceHealthStatus.HEALTHY if healthy else DeviceHealthStatus.UNHEALTHY,
            message=message,
        )

    async def get_capability(self, name: str) -> Capability:
        normalized = name.strip().casefold()
        if normalized == "measure":
            return cast(Capability, self._measure)
        if normalized == "capture":
            return cast(Capability, self._capture)
        raise UnsupportedCapabilityError(
            f"Instrument does not support capability {name!r}.",
            device_id=self._descriptor.id,
            capability=normalized,
        )


class ProgrammablePowerSupplyDriver:
    def __init__(
        self,
        *,
        device_id: str,
        name: str,
        channel: str,
        transport: PowerSupplyTransport,
        cycle_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._descriptor = DeviceDescriptor(
            id=device_id,
            name=name,
            type="programmable-power-supply-channel",
            capabilities={"power", "measure"},
            metadata={"channel": channel},
        )
        self._power = PowerSupplyCapability(
            transport,
            channel,
            cycle_delay_seconds=cycle_delay_seconds,
            sleep=sleep,
        )
        self._measure = MeasureAdapter(transport)

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return {"power", "measure"}

    async def health(self) -> DeviceHealth:
        healthy, message = await self._transport.health()
        return DeviceHealth(
            status=DeviceHealthStatus.HEALTHY if healthy else DeviceHealthStatus.UNHEALTHY,
            message=message,
        )

    async def get_capability(self, name: str) -> Capability:
        normalized = name.strip().casefold()
        if normalized == "power":
            return cast(Capability, self._power)
        if normalized == "measure":
            return cast(Capability, self._measure)
        raise UnsupportedCapabilityError(
            f"Power supply does not support capability {name!r}.",
            device_id=self._descriptor.id,
            capability=normalized,
        )


class InstrumentPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="instruments",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Generic measurement, capture, and programmable power abstractions.",
        supported_platforms=["any"],
        supported_devices=[
            "Programmable power supplies",
            "Logic analysers",
            "Oscilloscopes",
        ],
        capabilities=["power", "measure", "capture"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[InstrumentDriver | ProgrammablePowerSupplyDriver] = (),
    ) -> None:
        super().__init__(self.metadata_definition, drivers)


__all__ = [
    "CaptureAdapter",
    "InstrumentDriver",
    "InstrumentPlugin",
    "InstrumentTransport",
    "MeasureAdapter",
    "MemoryInstrumentTransport",
    "PowerSupplyCapability",
    "PowerSupplyTransport",
    "ProgrammablePowerSupplyDriver",
]
