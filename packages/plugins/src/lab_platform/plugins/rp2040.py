from __future__ import annotations

import shutil
from collections.abc import Iterable

from lab_platform.config import Rp2040Settings
from lab_platform.plugin_sdk import DeviceDriver, DiagnosticCheck, DiagnosticStatus, PluginMetadata
from lab_platform.plugins.hardware_base import StaticHardwarePlugin
from lab_platform.plugins.target_adapter import PhysicalTargetDriver
from lab_platform.real_backend.targets import PhysicalTarget


class Rp2040Plugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="rp2040",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Raspberry Pi Pico/RP2040 flashing through picotool or a mounted UF2 volume.",
        supported_platforms=["linux", "macos", "windows"],
        supported_devices=["Raspberry Pi Pico", "RP2040 boards with compatible bootloaders"],
        capabilities=["probe", "reset", "flash", "serial"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        drivers: Iterable[DeviceDriver] = (),
        *,
        settings: Rp2040Settings | None = None,
        dependency_available: bool | None = None,
    ) -> None:
        resolved = settings or Rp2040Settings()
        if dependency_available is None:
            available = (
                resolved.mount_path is not None and resolved.mount_path.is_dir()
                if resolved.tool == "uf2"
                else shutil.which(resolved.executable) is not None
            )
        else:
            available = dependency_available
        dependency = "UF2 volume" if resolved.tool == "uf2" else "picotool"
        check = DiagnosticCheck(
            name=f"RP2040 {dependency}",
            status=DiagnosticStatus.PASS if available else DiagnosticStatus.FAIL,
            message=(
                f"{dependency} is available" if available else f"{dependency} is not available."
            ),
            remediation=(
                None if available else "Install picotool or mount the board's UF2 boot volume."
            ),
        )
        super().__init__(self.metadata_definition, drivers, dependency_checks=(check,))

    @classmethod
    def from_targets(
        cls,
        targets: Iterable[PhysicalTarget],
        *,
        settings: Rp2040Settings | None = None,
        dependency_available: bool | None = None,
    ) -> Rp2040Plugin:
        return cls(
            (PhysicalTargetDriver(target, device_type="rp2040") for target in targets),
            settings=settings,
            dependency_available=dependency_available,
        )


__all__ = ["Rp2040Plugin"]
