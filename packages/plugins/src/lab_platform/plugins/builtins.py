from __future__ import annotations

from typing import cast

from lab_platform.models import Capability, PluginMetadata
from lab_platform.plugins.base import BasePlugin, PluginFactory
from lab_platform.plugins.can import SocketCanPlugin
from lab_platform.plugins.esp32 import Esp32Plugin
from lab_platform.plugins.instruments import InstrumentPlugin
from lab_platform.plugins.jlink import JLinkPlugin
from lab_platform.plugins.nrf52 import Nrf52Plugin
from lab_platform.plugins.openocd import OpenOcdPlugin
from lab_platform.plugins.power import NetworkRelayPlugin, UsbRelayPlugin
from lab_platform.plugins.rp2040 import Rp2040Plugin


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


class FlashPlugin(BasePlugin):
    """Stable capability-name bridge for the in-tree pre-1.0 plugin interface."""

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="flash",
                version="1.0.0",
                plugin_api_version="1.0",
                author="Lab Platform Contributors",
                description="Stable flash capability provider.",
                supported_platforms=["any"],
                supported_devices=["backend-provided targets"],
                capabilities=["flash"],
                minimum_agent_version="0.9.0-beta",
            ),
            [Capability(name="flash", description="Firmware programming")],
        )


BUILTIN_PLUGINS: dict[str, PluginFactory] = {
    "flash": FlashPlugin,
    "firmware": FirmwarePlugin,
    "power": PowerPlugin,
    "serial": SerialPlugin,
    "esp32": cast(PluginFactory, Esp32Plugin),
    "openocd": cast(PluginFactory, OpenOcdPlugin),
    "jlink": cast(PluginFactory, JLinkPlugin),
    "rp2040": cast(PluginFactory, Rp2040Plugin),
    "nrf52": cast(PluginFactory, Nrf52Plugin),
    "usb-relay": cast(PluginFactory, UsbRelayPlugin),
    "network-relay": cast(PluginFactory, NetworkRelayPlugin),
    "socketcan": cast(PluginFactory, SocketCanPlugin),
    "instruments": cast(PluginFactory, InstrumentPlugin),
}
