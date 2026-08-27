from __future__ import annotations

import shutil
from collections.abc import Iterable

from lab_platform.config import JLinkSettings, Nrf52Settings
from lab_platform.plugin_sdk import DeviceDriver, DiagnosticCheck, DiagnosticStatus, PluginMetadata
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.targets import PhysicalTarget


class Nrf52Plugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="nrf52",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Nordic nRF52 probe, flash, reset, and serial integration.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["Configured nRF52 development boards"],
        capabilities=["probe", "reset", "flash", "serial", "debug"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[DeviceDriver] = (),
        *,
        settings: Nrf52Settings | None = None,
        jlink_settings: JLinkSettings | None = None,
        executable_available: bool | None = None,
    ) -> None:
        resolved = settings or Nrf52Settings()
        executable = (
            (jlink_settings or JLinkSettings()).executable
            if resolved.tool == "jlink"
            else resolved.executable
        )
        available = (
            shutil.which(executable) is not None
            if executable_available is None
            else executable_available
        )
        tool_name = "J-Link" if resolved.tool == "jlink" else "Nordic tooling"
        check = DiagnosticCheck(
            name=tool_name,
            status=DiagnosticStatus.PASS if available else DiagnosticStatus.FAIL,
            message=(
                f"{tool_name} is available as {executable}"
                if available
                else f"{tool_name} is not installed."
            ),
            remediation=(
                None if available else "Install the configured Nordic or SEGGER command-line tools."
            ),
        )
        super().__init__(self.metadata_definition, drivers, dependency_checks=(check,))

    @classmethod
    def from_targets(
        cls,
        targets: Iterable[PhysicalTarget],
        *,
        settings: Nrf52Settings | None = None,
        jlink_settings: JLinkSettings | None = None,
        executable_available: bool | None = None,
    ) -> Nrf52Plugin:
        return cls(
            (PhysicalTargetDriver(target, device_type="nrf52") for target in targets),
            settings=settings,
            jlink_settings=jlink_settings,
            executable_available=executable_available,
        )


__all__ = ["Nrf52Plugin"]
