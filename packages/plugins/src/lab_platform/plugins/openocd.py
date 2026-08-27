from __future__ import annotations

import shutil
from collections.abc import Iterable

from lab_platform.config import OpenOcdSettings
from lab_platform.plugin_sdk import (
    DeviceDriver,
    DiagnosticCheck,
    DiagnosticStatus,
    PluginMetadata,
)
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.targets import PhysicalTarget


class OpenOcdPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="openocd",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Generic OpenOCD probe, flash, reset, and debugger integration.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["STM32 Nucleo F446RE", "OpenOCD-supported targets"],
        capabilities=["probe", "reset", "flash", "debug", "serial"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[DeviceDriver] = (),
        *,
        settings: OpenOcdSettings | None = None,
        executable_available: bool | None = None,
    ) -> None:
        resolved = settings or OpenOcdSettings()
        available = (
            shutil.which(resolved.executable) is not None
            if executable_available is None
            else executable_available
        )
        check = DiagnosticCheck(
            name="OpenOCD executable",
            status=DiagnosticStatus.PASS if available else DiagnosticStatus.FAIL,
            message=(
                f"OpenOCD is available as {resolved.executable}"
                if available
                else "OpenOCD tools not installed."
            ),
            remediation=(
                None
                if available
                else "Install OpenOCD and ensure the configured executable is on PATH."
            ),
        )
        super().__init__(self.metadata_definition, drivers, dependency_checks=(check,))

    @classmethod
    def from_targets(
        cls,
        targets: Iterable[PhysicalTarget],
        *,
        settings: OpenOcdSettings | None = None,
        executable_available: bool | None = None,
    ) -> OpenOcdPlugin:
        return cls(
            (PhysicalTargetDriver(target, device_type="stm32") for target in targets),
            settings=settings,
            executable_available=executable_available,
        )


__all__ = ["OpenOcdPlugin"]
