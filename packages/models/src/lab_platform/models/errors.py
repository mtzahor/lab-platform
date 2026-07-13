from __future__ import annotations

from lab_platform.models.domain import OperationStatus


class InvalidOperationTransition(ValueError):
    def __init__(self, source: OperationStatus, target: OperationStatus) -> None:
        super().__init__(f"Cannot transition operation from {source.value} to {target.value}")
        self.source = source
        self.target = target
