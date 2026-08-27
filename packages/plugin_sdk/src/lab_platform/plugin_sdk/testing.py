from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from lab_platform.plugin_sdk.errors import UnsupportedCapabilityError
from lab_platform.plugin_sdk.interfaces import PluginContext, PluginRegistration
from lab_platform.plugin_sdk.models import (
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    FirmwareArtifact,
    FlashOptions,
    PluginHealthStatus,
    ProgressUpdate,
)


@dataclass(slots=True)
class FakeFlashCapability:
    updates: list[ProgressUpdate] = field(
        default_factory=lambda: [ProgressUpdate(percent=100, message="complete")]
    )
    calls: list[tuple[FirmwareArtifact, FlashOptions]] = field(default_factory=list)

    async def flash(
        self,
        image: FirmwareArtifact,
        options: FlashOptions,
    ) -> AsyncIterator[ProgressUpdate]:
        self.calls.append((image, options))
        for update in self.updates:
            yield update


class FakeDeviceDriver:
    def __init__(
        self,
        descriptor: DeviceDescriptor,
        capabilities: dict[str, object] | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._capabilities = capabilities or {}

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return set(self._capabilities)

    async def health(self) -> DeviceHealth:
        return DeviceHealth(status=DeviceHealthStatus.HEALTHY, message="fake device ready")

    async def get_capability(self, name: str) -> Any:
        try:
            return self._capabilities[name.casefold()]
        except KeyError as exc:
            raise UnsupportedCapabilityError(
                f"Fake device does not support {name}", capability=name
            ) from exc


def test_context(agent_version: str = "1.0.0") -> PluginContext:
    return PluginContext(
        logger=logging.getLogger("lab-platform.plugin-contract"),
        agent_version=agent_version,
    )


async def assert_plugin_contract(
    registration: PluginRegistration[Any],
    raw_config: dict[str, Any] | None = None,
) -> None:
    """Exercise the lifecycle and discovery invariants shared by all plugins."""

    config = registration.validate_config(raw_config)
    plugin = registration.create(config, test_context())
    assert plugin.metadata == registration.metadata

    await plugin.initialize()
    try:
        health = await plugin.health()
        assert health.status in {
            PluginHealthStatus.HEALTHY,
            PluginHealthStatus.DEGRADED,
            PluginHealthStatus.UNHEALTHY,
        }
        descriptors = await plugin.discover()
        identifiers = [descriptor.id for descriptor in descriptors]
        assert len(identifiers) == len(set(identifiers)), "device IDs must be unique per plugin"
        for descriptor in descriptors:
            driver = await plugin.get_driver(descriptor.id)
            assert driver.descriptor == descriptor
            reported = {item.casefold() for item in driver.capabilities}
            described = {item.casefold() for item in descriptor.capabilities}
            assert reported == described
            await driver.health()
            try:
                await driver.get_capability("contract.unsupported")
            except UnsupportedCapabilityError:
                pass
            else:
                raise AssertionError(
                    "unsupported capabilities must raise UnsupportedCapabilityError"
                )
    finally:
        await plugin.shutdown()


async def assert_cancellation_propagates(
    operation: Callable[[], Awaitable[Any]],
) -> None:
    """Contract helper proving a plugin operation does not swallow cancellation."""

    async def run_operation() -> Any:
        return await operation()

    task: asyncio.Task[Any] = asyncio.create_task(run_operation())
    await asyncio.sleep(0)
    if task.done():
        raise AssertionError("operation completed before cancellation could be exercised")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("plugin operation swallowed asyncio cancellation")


async def assert_timeout_enforced(
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float = 0.01,
) -> None:
    """Contract helper proving an operation can be bounded by the Agent lifecycle timeout."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        await asyncio.wait_for(operation(), timeout=timeout_seconds)
    except TimeoutError:
        return
    raise AssertionError("plugin operation completed without exercising the timeout boundary")


__all__ = [
    "FakeDeviceDriver",
    "FakeFlashCapability",
    "assert_cancellation_propagates",
    "assert_plugin_contract",
    "assert_timeout_enforced",
    "test_context",
]
