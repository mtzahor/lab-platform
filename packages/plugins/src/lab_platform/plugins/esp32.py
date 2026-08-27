from __future__ import annotations

from collections.abc import Iterable

from lab_platform.plugin_sdk import DeviceDriver, PluginMetadata
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.targets import PhysicalTarget


class Esp32Plugin(StaticHardwarePlugin):
    """Reference Plugin API 1.0 implementation for configured ESP32 targets."""

    metadata_definition = PluginMetadata(
        name="esp32",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="ESP32 probe, flash, reset, and serial integration through esptool.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["ESP32 DevKit V1", "ESP32 development boards supported by esptool"],
        capabilities=["probe", "reset", "flash", "serial"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[DeviceDriver] = (),
    ) -> None:
        super().__init__(self.metadata_definition, drivers)

    @classmethod
    def from_targets(cls, targets: Iterable[PhysicalTarget]) -> Esp32Plugin:
        return cls(PhysicalTargetDriver(target, device_type="esp32") for target in targets)


__all__ = ["Esp32Plugin"]
