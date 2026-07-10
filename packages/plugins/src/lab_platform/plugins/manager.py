from __future__ import annotations

import importlib
from collections.abc import Mapping
from importlib.metadata import entry_points
from typing import Any, cast

from lab_platform.plugins.base import Plugin, PluginFactory, PluginLoadError
from lab_platform.plugins.builtins import BUILTIN_PLUGINS

ENTRY_POINT_GROUP = "lab_platform.plugins"


class PluginManager:
    def __init__(
        self,
        available_plugins: Mapping[str, PluginFactory] | None = None,
    ) -> None:
        self._available_plugins: dict[str, PluginFactory] = dict(BUILTIN_PLUGINS)
        if available_plugins is not None:
            self._available_plugins.update(available_plugins)
        self._plugins: dict[str, Plugin] = {}

    def discover(self) -> dict[str, PluginFactory]:
        discovered = dict(self._available_plugins)
        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            loaded = entry_point.load()
            discovered[entry_point.name] = _coerce_factory(loaded)
        return discovered

    async def load(self, plugin_names: list[str]) -> list[Plugin]:
        discovered = self.discover()
        loaded: list[Plugin] = []
        try:
            for plugin_name in plugin_names:
                plugin = self._create_plugin(plugin_name, discovered)
                if plugin.metadata.name in self._plugins:
                    raise PluginLoadError(f"Duplicate plugin: {plugin.metadata.name}")
                await plugin.initialize()
                self._plugins[plugin.metadata.name] = plugin
                loaded.append(plugin)
        except Exception as exc:
            await self.shutdown()
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(f"Could not initialize plugin {plugin_name}") from exc
        return loaded

    async def shutdown(self) -> None:
        for plugin in reversed(list(self._plugins.values())):
            await plugin.shutdown()
        self._plugins.clear()

    def plugins(self) -> list[Plugin]:
        return [self._plugins[name] for name in sorted(self._plugins)]

    def _create_plugin(
        self,
        plugin_name: str,
        discovered: Mapping[str, PluginFactory],
    ) -> Plugin:
        factory = discovered.get(plugin_name)
        if factory is None:
            factory = _load_import_path(plugin_name)
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            raise PluginLoadError(f"Could not initialize plugin {plugin_name}") from exc


def _load_import_path(plugin_name: str) -> PluginFactory:
    if ":" not in plugin_name:
        raise PluginLoadError(f"Unknown plugin: {plugin_name}")
    module_name, attribute_name = plugin_name.split(":", 1)
    module = importlib.import_module(module_name)
    loaded = getattr(module, attribute_name)
    return _coerce_factory(loaded)


def _coerce_factory(loaded: Any) -> PluginFactory:
    if isinstance(loaded, type):
        return cast(PluginFactory, loaded)
    if callable(loaded):
        return cast(PluginFactory, loaded)
    return lambda: cast(Plugin, loaded)
