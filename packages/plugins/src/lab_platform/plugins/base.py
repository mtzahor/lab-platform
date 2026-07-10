from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from lab_platform.models import Capability, PluginMetadata


class Plugin(Protocol):
    @property
    def metadata(self) -> PluginMetadata: ...

    async def initialize(self) -> None: ...

    async def shutdown(self) -> None: ...

    def capabilities(self) -> Sequence[Capability]: ...


PluginFactory = Callable[[], Plugin]


class PluginLoadError(RuntimeError):
    pass


class BasePlugin:
    def __init__(
        self,
        metadata: PluginMetadata,
        capabilities: Sequence[Capability],
    ) -> None:
        self._metadata = metadata
        self._capabilities = tuple(capabilities)
        self.initialized = False

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def initialize(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.initialized = False

    def capabilities(self) -> Sequence[Capability]:
        return self._capabilities
