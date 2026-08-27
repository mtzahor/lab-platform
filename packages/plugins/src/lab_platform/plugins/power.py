from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from string import Formatter
from typing import Protocol, cast

from lab_platform.plugin_sdk import (
    Capability,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    PluginMetadata,
    UnsupportedCapabilityError,
)
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.real_backend.errors import (
    ExternalToolCommandFailedError,
    ExternalToolNotAvailableError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessRunner


class RelayTransport(Protocol):
    async def set_state(self, channel: str, enabled: bool) -> None: ...

    async def health(self) -> tuple[bool, str]: ...


class MemoryRelayTransport:
    """Deterministic relay transport for SimLab and plugin contract testing."""

    def __init__(self) -> None:
        self.states: dict[str, bool] = {}

    async def set_state(self, channel: str, enabled: bool) -> None:
        self.states[channel] = enabled

    async def health(self) -> tuple[bool, str]:
        return True, "Relay transport is available"


class NetworkRelayTransport:
    """Vendor-neutral boundary for an authenticated network relay client."""

    def __init__(
        self,
        setter: Callable[[str, bool], Awaitable[None]],
        *,
        health_check: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
    ) -> None:
        self._setter = setter
        self._health_check = health_check

    async def set_state(self, channel: str, enabled: bool) -> None:
        await self._setter(channel, enabled)

    async def health(self) -> tuple[bool, str]:
        if self._health_check is None:
            return True, "Network relay client is configured"
        return await self._health_check()


class CommandRelayTransport:
    """Invoke a relay utility with a fixed argv template and no shell."""

    def __init__(
        self,
        runner: ProcessRunner,
        argv_template: Sequence[str],
        *,
        timeout_seconds: float = 10,
    ) -> None:
        if not argv_template:
            raise ValueError("relay command argv template cannot be empty")
        allowed = {"{channel}", "{state}"}
        for argument in argv_template:
            fields = {
                field_name
                for _literal, field_name, _format, _conversion in Formatter().parse(argument)
                if field_name is not None
            }
            unsupported = fields.difference({item.strip("{}") for item in allowed})
            if unsupported:
                raise ValueError(
                    "unsupported relay command placeholder: " + ", ".join(sorted(unsupported))
                )
        self._runner = runner
        self._argv_template = tuple(argv_template)
        self._timeout_seconds = timeout_seconds

    async def set_state(self, channel: str, enabled: bool) -> None:
        safe = channel and all(character.isalnum() or character in "._-" for character in channel)
        if not safe:
            raise ValueError("relay channel contains unsupported characters")
        values = {"channel": channel, "state": "on" if enabled else "off"}
        args = [argument.format_map(values) for argument in self._argv_template]
        try:
            result = await self._runner.run(args, timeout_seconds=self._timeout_seconds)
        except ProcessExecutableNotFoundError as exc:
            raise ExternalToolNotAvailableError(
                f"Relay utility is not installed: {args[0]}"
            ) from exc
        except ProcessExecutionTimeoutError as exc:
            raise ExternalToolCommandFailedError("Relay utility timed out") from exc
        if result.returncode != 0:
            detail = next(
                (line for line in reversed((*result.stderr, *result.stdout)) if line.strip()),
                "relay command failed",
            )
            raise ExternalToolCommandFailedError(
                detail,
                return_code=result.returncode,
            )

    async def health(self) -> tuple[bool, str]:
        return True, f"Relay command configured: {self._argv_template[0]}"


class UsbRelayPowerCapability:
    def __init__(
        self,
        transport: RelayTransport,
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
        await self._transport.set_state(self._channel, True)

    async def off(self) -> None:
        await self._transport.set_state(self._channel, False)

    async def cycle(self) -> None:
        await self.off()
        await self._sleep(self._cycle_delay_seconds)
        await self.on()


class UsbRelayDriver:
    def __init__(
        self,
        *,
        device_id: str,
        name: str,
        channel: str,
        transport: RelayTransport,
        device_type: str = "usb-relay-channel",
        cycle_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._descriptor = DeviceDescriptor(
            id=device_id,
            name=name,
            type=device_type,
            capabilities={"power"},
            metadata={"channel": channel},
        )
        self._power = UsbRelayPowerCapability(
            transport,
            channel,
            cycle_delay_seconds=cycle_delay_seconds,
            sleep=sleep,
        )

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return {"power"}

    async def health(self) -> DeviceHealth:
        healthy, message = await self._transport.health()
        return DeviceHealth(
            status=DeviceHealthStatus.HEALTHY if healthy else DeviceHealthStatus.UNHEALTHY,
            message=message,
        )

    async def get_capability(self, name: str) -> Capability:
        if name.strip().casefold() == "power":
            return cast(Capability, self._power)
        raise UnsupportedCapabilityError(
            f"USB relay does not support capability {name!r}.",
            device_id=self._descriptor.id,
            capability=name,
        )


class UsbRelayPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="usb-relay",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Independent multi-channel USB relay power-control resources.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["Command-controlled USB relay"],
        capabilities=["power"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        channels: Mapping[str, RelayTransport] | None = None,
        *,
        cycle_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        resolved = channels or {}
        drivers = [
            UsbRelayDriver(
                device_id=f"usb-relay:{channel}",
                name=f"USB relay channel {channel}",
                channel=channel,
                transport=transport,
                cycle_delay_seconds=cycle_delay_seconds,
                sleep=sleep,
            )
            for channel, transport in sorted(resolved.items())
        ]
        super().__init__(self.metadata_definition, drivers)


class NetworkRelayPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="network-relay",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Vendor-neutral network-controlled relay power resources.",
        supported_platforms=["any"],
        supported_devices=["Network relay clients implemented through the transport contract"],
        capabilities=["power"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        channels: Mapping[str, RelayTransport] | None = None,
        *,
        cycle_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        drivers = [
            UsbRelayDriver(
                device_id=f"network-relay:{channel}",
                name=f"Network relay channel {channel}",
                channel=channel,
                transport=transport,
                device_type="network-relay-channel",
                cycle_delay_seconds=cycle_delay_seconds,
                sleep=sleep,
            )
            for channel, transport in sorted((channels or {}).items())
        ]
        super().__init__(self.metadata_definition, drivers)


__all__ = [
    "CommandRelayTransport",
    "MemoryRelayTransport",
    "NetworkRelayPlugin",
    "NetworkRelayTransport",
    "RelayTransport",
    "UsbRelayDriver",
    "UsbRelayPlugin",
    "UsbRelayPowerCapability",
]
