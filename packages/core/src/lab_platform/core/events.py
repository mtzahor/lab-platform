from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable

from lab_platform.models import Event

EventHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> Callable[[], None]:
        handlers = self._handlers.setdefault(event_type, [])
        handlers.append(handler)

        def unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        handlers = list(self._handlers.get(event.type, []))
        if event.type != "*":
            handlers.extend(self._handlers.get("*", []))
        for handler in handlers:
            result = handler(event)
            if isawaitable(result):
                await result
