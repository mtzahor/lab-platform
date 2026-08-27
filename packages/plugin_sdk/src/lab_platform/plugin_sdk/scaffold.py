from __future__ import annotations

import re
from pathlib import Path

_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def create_plugin_scaffold(name: str, destination: Path | None = None) -> Path:
    slug = name.strip().casefold()
    if _PLUGIN_NAME.fullmatch(slug) is None:
        raise ValueError("plugin name must start with a letter and contain letters, digits, or '-'")
    module = slug.replace("-", "_")
    root = (destination or Path.cwd()) / slug
    root.mkdir(parents=True, exist_ok=False)
    files = _template_files(slug, module)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _template_files(slug: str, module: str) -> dict[Path, str]:
    title = " ".join(part.capitalize() for part in slug.split("-"))
    class_prefix = "".join(part.capitalize() for part in slug.split("-"))
    return {
        Path("pyproject.toml"): f'''[build-system]
requires = ["setuptools>=70", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{slug}"
version = "0.1.0"
description = "{title} hardware plugin for Lab Platform."
requires-python = ">=3.11"
dependencies = ["lab-platform-plugin-sdk>=1.0,<2", "pydantic>=2.8,<3"]

[project.optional-dependencies]
dev = ["pytest>=8,<9"]

[project.entry-points."lab_platform.plugins"]
{slug} = "{module}.plugin:registration"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
markers = ["plugin_contract: Lab Platform plugin contract"]
''',
        Path("README.md"): f"""# {title}

Generated Lab Platform Plugin API 1.0 project.

```console
python -m pip install -e ".[dev]"
pytest
```
""",
        Path("src") / module / "__init__.py": (
            'from .plugin import registration\n\n__all__ = ["registration"]\n'
        ),
        Path("src") / module / "config.py": """from pydantic import BaseModel, ConfigDict


class PluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
""",
        Path("src") / module / "errors.py": """\
from lab_platform.plugin_sdk import PluginOperationError


class ExampleDeviceError(PluginOperationError):
    code = "EXAMPLE_DEVICE_ERROR"
""",
        Path("src") / module / "driver.py": f'''from lab_platform.plugin_sdk import (
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    UnsupportedCapabilityError,
)


class {class_prefix}Driver:
    def __init__(self, device_id: str) -> None:
        self._descriptor = DeviceDescriptor(
            id=device_id,
            name="{title} device",
            type="{slug}",
            capabilities=set(),
        )

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return set(self._descriptor.capabilities)

    async def health(self) -> DeviceHealth:
        return DeviceHealth(status=DeviceHealthStatus.HEALTHY, message="Device ready")

    async def get_capability(self, name: str):
        raise UnsupportedCapabilityError(f"Capability {{name}} is not supported", capability=name)
''',
        Path("src") / module / "plugin.py": f'''from lab_platform.plugin_sdk import (
    BaseHardwarePlugin,
    DeviceDescriptor,
    DeviceUnavailableError,
    PLUGIN_API_VERSION,
    PluginContext,
    PluginMetadata,
    PluginRegistration,
)

from .config import PluginConfig
from .driver import {class_prefix}Driver


METADATA = PluginMetadata(
    name="{slug}",
    version="0.1.0",
    plugin_api_version=PLUGIN_API_VERSION,
    vendor=None,
    description="{title} hardware plugin.",
    supported_platforms=["any"],
    supported_devices=["example-device"],
    capabilities=[],
    minimum_agent_version="0.9.0-beta",
)


class {class_prefix}Plugin(BaseHardwarePlugin):
    def __init__(self, config: PluginConfig, context: PluginContext) -> None:
        super().__init__(METADATA)
        self._config = config
        self._context = context
        self._drivers: dict[str, {class_prefix}Driver] = {{}}

    async def discover(self) -> list[DeviceDescriptor]:
        return [driver.descriptor for driver in self._drivers.values()]

    async def get_driver(self, device_id: str) -> {class_prefix}Driver:
        try:
            return self._drivers[device_id]
        except KeyError as exc:
            raise DeviceUnavailableError(f"Device {{device_id!r}} was not discovered") from exc


def _create(config: PluginConfig, context: PluginContext) -> {class_prefix}Plugin:
    return {class_prefix}Plugin(config, context)


registration = PluginRegistration(
    metadata=METADATA,
    config_model=PluginConfig,
    factory=_create,
)
''',
        Path("tests") / "test_contract.py": f"""import asyncio

import pytest
from lab_platform.plugin_sdk.testing import assert_plugin_contract

from {module}.plugin import registration


@pytest.mark.plugin_contract
def test_plugin_contract() -> None:
    asyncio.run(assert_plugin_contract(registration))
""",
        Path("tests") / "test_driver.py": f"""import asyncio

from {module}.driver import {class_prefix}Driver


def test_driver_health() -> None:
    driver = {class_prefix}Driver("example-01")
    assert asyncio.run(driver.health()).status.value == "healthy"
""",
    }


__all__ = ["create_plugin_scaffold"]
