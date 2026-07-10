from __future__ import annotations

from collections.abc import Iterable

from lab_platform.models import Capability


class CapabilityRegistry:
    """In-memory capability catalog used by the business-logic layer."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        current = self._capabilities.get(capability.name)
        if current is not None and current != capability:
            raise ValueError(f"Capability already registered: {capability.name}")
        self._capabilities[capability.name] = capability

    def register_many(self, capabilities: Iterable[Capability]) -> None:
        for capability in capabilities:
            self.register(capability)

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def capabilities(self) -> list[Capability]:
        return [self._capabilities[name] for name in sorted(self._capabilities)]

    def clear(self) -> None:
        self._capabilities.clear()
