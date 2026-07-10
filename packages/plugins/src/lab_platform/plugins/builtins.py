from __future__ import annotations

from lab_platform.models import Capability, PluginMetadata
from lab_platform.plugins.base import BasePlugin, PluginFactory


class PowerPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="power",
                version="0.1.0-alpha",
                author="Lab Platform",
                description="Power control capability provider.",
                capabilities=["Power"],
            ),
            [Capability(name="Power", description="Power rail control")],
        )


class SerialPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="serial",
                version="0.1.0-alpha",
                author="Lab Platform",
                description="Serial console capability provider.",
                capabilities=["Serial"],
            ),
            [Capability(name="Serial", description="Serial console access")],
        )


class FirmwarePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="firmware",
                version="0.1.0-alpha",
                author="Lab Platform",
                description="Firmware metadata capability provider.",
                capabilities=["Firmware"],
            ),
            [Capability(name="Firmware", description="Firmware inventory")],
        )


BUILTIN_PLUGINS: dict[str, PluginFactory] = {
    "firmware": FirmwarePlugin,
    "power": PowerPlugin,
    "serial": SerialPlugin,
}
