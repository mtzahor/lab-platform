from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, TypeVar

StateT = TypeVar("StateT")


class StateTransitionError(ValueError):
    pass


class StateMachine(Generic[StateT]):
    def __init__(
        self,
        initial_state: StateT,
        transitions: dict[StateT, Iterable[StateT]],
    ) -> None:
        self._state = initial_state
        self._transitions = {source: frozenset(targets) for source, targets in transitions.items()}

    @property
    def state(self) -> StateT:
        return self._state

    def can_transition(self, target: StateT) -> bool:
        return target in self._transitions.get(self._state, frozenset())

    def transition_to(self, target: StateT) -> None:
        if not self.can_transition(target):
            raise StateTransitionError(f"Cannot transition from {self._state} to {target}")
        self._state = target
