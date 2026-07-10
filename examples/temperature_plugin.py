from lab_platform.models import Capability, PluginMetadata
from lab_platform.plugins import BasePlugin


class TemperaturePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                name="temperature",
                version="0.1.0-alpha",
                author="Example Author",
                description="Example temperature-reading capability.",
                capabilities=["Temperature"],
            ),
            [Capability(name="Temperature", description="Read bench temperature")],
        )
