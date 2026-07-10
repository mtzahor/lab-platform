from __future__ import annotations

import asyncio
from collections.abc import Callable

import lab_platform.plugins.manager as manager_module
import pytest
from lab_platform.models import Capability, PluginMetadata
from lab_platform.plugins import BasePlugin, PluginLoadError, PluginManager


class TestPlugin(BasePlugin):
    __test__ = False

    def __init__(self, name: str = "test") -> None:
        super().__init__(
            PluginMetadata(
                name=name,
                version="1.2.3",
                author="tests",
                description="test plugin",
                capabilities=["Test"],
            ),
            [Capability(name="Test")],
        )


class FailingPlugin(TestPlugin):
    async def initialize(self) -> None:
        raise RuntimeError("boom")


def test_load_builtin_plugins_and_shutdown_in_reverse() -> None:
    async def scenario() -> None:
        manager = PluginManager()
        plugins = await manager.load(["power", "serial", "firmware"])

        assert [plugin.metadata.name for plugin in plugins] == ["power", "serial", "firmware"]
        assert [plugin.metadata.name for plugin in manager.plugins()] == [
            "firmware",
            "power",
            "serial",
        ]
        assert plugins[0].capabilities()[0].name == "Power"
        await manager.shutdown()
        assert manager.plugins() == []
        assert all(isinstance(plugin, BasePlugin) and not plugin.initialized for plugin in plugins)

    asyncio.run(scenario())


@pytest.mark.parametrize("plugin_name", ["missing", "bad.import.path:NoPlugin"])
def test_unknown_plugins_fail(plugin_name: str) -> None:
    async def scenario() -> None:
        manager = PluginManager()
        with pytest.raises((PluginLoadError, ModuleNotFoundError)):
            await manager.load([plugin_name])

    asyncio.run(scenario())


def test_import_path_plugin_loads() -> None:
    async def scenario() -> None:
        manager = PluginManager()
        plugins = await manager.load(["lab_platform.plugins.builtins:PowerPlugin"])
        assert plugins[0].metadata.name == "power"
        await manager.shutdown()

    asyncio.run(scenario())


def test_duplicate_and_initialization_failure_roll_back() -> None:
    async def scenario() -> None:
        first = TestPlugin("first")
        manager = PluginManager(
            {
                "first": lambda: first,
                "bad": FailingPlugin,
            }
        )
        with pytest.raises(PluginLoadError, match="Could not initialize"):
            await manager.load(["first", "bad"])
        assert not first.initialized
        assert manager.plugins() == []

        with pytest.raises(PluginLoadError, match="Duplicate"):
            await PluginManager().load(["power", "power"])

        def broken_factory() -> TestPlugin:
            raise RuntimeError("factory failed")

        with pytest.raises(PluginLoadError, match="Could not initialize"):
            await PluginManager({"broken": broken_factory}).load(["broken"])

    asyncio.run(scenario())


class FakeEntryPoint:
    name = "discovered"

    def load(self) -> Callable[[], TestPlugin]:
        return TestPlugin


def test_discovery_reads_package_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        assert group == manager_module.ENTRY_POINT_GROUP
        return [FakeEntryPoint()]

    monkeypatch.setattr(manager_module, "entry_points", fake_entry_points)
    discovered = PluginManager().discover()

    assert discovered["discovered"]().metadata.name == "test"
