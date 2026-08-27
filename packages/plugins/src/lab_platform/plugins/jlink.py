from __future__ import annotations

import shutil
from collections.abc import Iterable

from lab_platform.config import JLinkSettings
from lab_platform.plugin_sdk import DeviceDriver, DiagnosticCheck, DiagnosticStatus, PluginMetadata
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.targets import PhysicalTarget


class JLinkPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="jlink",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="SEGGER J-Link probe, flash, reset, and debugger integration.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["Configured SEGGER J-Link targets", "nRF52 development boards"],
        capabilities=["probe", "reset", "flash", "debug", "serial"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[DeviceDriver] = (),
        *,
        settings: JLinkSettings | None = None,
        executable_available: bool | None = None,
    ) -> None:
        resolved = settings or JLinkSettings()
        available = (
            shutil.which(resolved.executable) is not None
            if executable_available is None
            else executable_available
        )
        check = DiagnosticCheck(
            name="J-Link tools",
            status=DiagnosticStatus.PASS if available else DiagnosticStatus.FAIL,
            message=(
                f"J-Link is available as {resolved.executable}"
                if available
                else "J-Link tools not installed."
            ),
            remediation=(
                None
                if available
                else "Install SEGGER J-Link Software and Documentation Pack and update PATH."
            ),
        )
        super().__init__(self.metadata_definition, drivers, dependency_checks=(check,))

    @classmethod
    def from_targets(
        cls,
        targets: Iterable[PhysicalTarget],
        *,
        settings: JLinkSettings | None = None,
        executable_available: bool | None = None,
    ) -> JLinkPlugin:
        return cls(
            (PhysicalTargetDriver(target, device_type="jlink-target") for target in targets),
            settings=settings,
            executable_available=executable_available,
        )


__all__ = ["JLinkPlugin"]
