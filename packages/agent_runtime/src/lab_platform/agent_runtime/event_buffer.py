from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol, TypeAlias
from uuid import UUID, uuid4

from lab_platform.models import BufferedAgentEvent, BufferedEventPriority

EVENT_BUFFER_OVERFLOW = "EVENT_BUFFER_OVERFLOW"

Clock: TypeAlias = Callable[[], datetime]
EventIdFactory: TypeAlias = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class EventBufferStats:
    buffered_events: int
    buffered_data_events: int
    last_issued_sequence: int
    last_acknowledged_sequence: int
    dropped_progress: int
    dropped_state: int
    dropped_failure: int
    dropped_terminal: int
    coalesced_progress: int

    @property
    def dropped_total(self) -> int:
        return (
            self.dropped_progress
            + self.dropped_state
            + self.dropped_failure
            + self.dropped_terminal
        )


class AgentEventBufferRepository(Protocol):
    """Persistent boundary for ordered, bounded Agent-to-control-plane events."""

    async def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        priority: BufferedEventPriority = BufferedEventPriority.STATE,
        event_id: UUID | None = None,
        created_at: datetime | None = None,
        coalesce_key: str | None = None,
    ) -> BufferedAgentEvent | None: ...

    async def peek(self, *, limit: int = 100) -> tuple[BufferedAgentEvent, ...]: ...

    async def acknowledge_through(self, sequence_number: int) -> int: ...

    async def stats(self) -> EventBufferStats: ...


class InMemoryAgentEventBuffer:
    """Thread-safe reference buffer with one bounded overflow-control record.

    ``capacity`` limits ordinary events. A single coalesced overflow marker is held in a dedicated
    control slot, so the absolute bound is ``capacity + 1`` and high-priority terminal/failure
    records never need to be discarded merely to describe an overflow.
    """

    def __init__(
        self,
        agent_id: UUID,
        *,
        capacity: int = 10_000,
        clock: Clock | None = None,
        event_id_factory: EventIdFactory | None = None,
    ) -> None:
        if isinstance(capacity, bool) or capacity < 1:
            raise ValueError("event buffer capacity must be a positive integer")
        self._agent_id = agent_id
        self._capacity = capacity
        self._clock = clock or _utc_now
        self._event_id_factory = event_id_factory or uuid4
        self._events: dict[int, BufferedAgentEvent] = {}
        self._coalesce_sequences: dict[str, int] = {}
        self._overflow_marker: BufferedAgentEvent | None = None
        self._known: dict[UUID, BufferedAgentEvent | None] = {}
        self._next_sequence = 1
        self._last_acknowledged_sequence = 0
        self._last_peeked_sequence = 0
        self._dropped: dict[BufferedEventPriority, int] = {
            priority: 0 for priority in BufferedEventPriority
        }
        self._coalesced_progress = 0
        self._lock = RLock()

    @property
    def agent_id(self) -> UUID:
        return self._agent_id

    @property
    def capacity(self) -> int:
        return self._capacity

    async def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        priority: BufferedEventPriority = BufferedEventPriority.STATE,
        event_id: UUID | None = None,
        created_at: datetime | None = None,
        coalesce_key: str | None = None,
    ) -> BufferedAgentEvent | None:
        identifier = event_id or self._event_id_factory()
        with self._lock:
            if identifier in self._known:
                return self._known[identifier]
            event = self._new_event(
                identifier,
                event_type,
                payload,
                priority=priority,
                created_at=created_at,
            )
            key = (
                coalesce_key or _default_progress_key(event_type, payload)
                if priority is BufferedEventPriority.PROGRESS
                else None
            )
            if key is not None:
                previous_sequence = self._coalesce_sequences.get(key)
                if previous_sequence is not None and previous_sequence in self._events:
                    self._events.pop(previous_sequence)
                    self._coalesced_progress += 1

            if len(self._events) >= self._capacity:
                candidate = min(
                    self._events.values(),
                    key=lambda item: (int(item.priority), item.sequence_number),
                )
                if candidate.priority < priority:
                    self._discard_buffered(candidate)
                else:
                    self._known[identifier] = None
                    self._record_overflow(priority)
                    return None

            self._events[event.sequence_number] = event
            self._known[identifier] = event
            if key is not None:
                self._coalesce_sequences[key] = event.sequence_number
            return event

    async def peek(self, *, limit: int = 100) -> tuple[BufferedAgentEvent, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("event peek limit must be a positive integer")
        with self._lock:
            items = list(self._events.values())
            if self._overflow_marker is not None:
                items.append(self._overflow_marker)
            result = tuple(sorted(items, key=lambda event: event.sequence_number)[:limit])
            if result:
                self._last_peeked_sequence = max(
                    self._last_peeked_sequence,
                    result[-1].sequence_number,
                )
            return result

    async def acknowledge_through(self, sequence_number: int) -> int:
        if isinstance(sequence_number, bool) or sequence_number < 0:
            raise ValueError("acknowledged sequence number must be a non-negative integer")
        with self._lock:
            if sequence_number <= self._last_acknowledged_sequence:
                return 0
            if sequence_number > self._last_peeked_sequence:
                raise ValueError("cannot acknowledge an event sequence that has not been peeked")
            removed = 0
            acknowledged = (sequence for sequence in self._events if sequence <= sequence_number)
            for current in sorted(acknowledged):
                event = self._events.pop(current)
                self._remove_coalescing_reference(event)
                removed += 1
            if (
                self._overflow_marker is not None
                and self._overflow_marker.sequence_number <= sequence_number
            ):
                self._overflow_marker = None
                removed += 1
            self._last_acknowledged_sequence = sequence_number
            return removed

    async def stats(self) -> EventBufferStats:
        with self._lock:
            return EventBufferStats(
                buffered_events=len(self._events) + int(self._overflow_marker is not None),
                buffered_data_events=len(self._events),
                last_issued_sequence=self._next_sequence - 1,
                last_acknowledged_sequence=self._last_acknowledged_sequence,
                dropped_progress=self._dropped[BufferedEventPriority.PROGRESS],
                dropped_state=self._dropped[BufferedEventPriority.STATE],
                dropped_failure=self._dropped[BufferedEventPriority.FAILURE],
                dropped_terminal=self._dropped[BufferedEventPriority.TERMINAL],
                coalesced_progress=self._coalesced_progress,
            )

    def _new_event(
        self,
        event_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        priority: BufferedEventPriority,
        created_at: datetime | None,
    ) -> BufferedAgentEvent:
        event = BufferedAgentEvent(
            id=event_id,
            agent_id=self._agent_id,
            sequence_number=self._next_sequence,
            event_type=event_type,
            payload=dict(payload),
            priority=priority,
            created_at=created_at or self._clock(),
        )
        self._next_sequence += 1
        return event

    def _discard_buffered(self, event: BufferedAgentEvent) -> None:
        self._events.pop(event.sequence_number)
        self._remove_coalescing_reference(event)
        self._record_overflow(event.priority)

    def _remove_coalescing_reference(self, event: BufferedAgentEvent) -> None:
        stale = [
            key
            for key, sequence_number in self._coalesce_sequences.items()
            if sequence_number == event.sequence_number
        ]
        for key in stale:
            self._coalesce_sequences.pop(key, None)

    def _record_overflow(self, dropped_priority: BufferedEventPriority) -> None:
        self._dropped[dropped_priority] += 1
        counts = self._overflow_payload()
        if self._overflow_marker is None:
            marker = self._new_event(
                self._event_id_factory(),
                EVENT_BUFFER_OVERFLOW,
                counts,
                priority=BufferedEventPriority.FAILURE,
                created_at=self._clock(),
            )
            self._overflow_marker = marker
            self._known[marker.id] = marker
            return
        self._overflow_marker = self._overflow_marker.model_copy(update={"payload": counts})
        self._known[self._overflow_marker.id] = self._overflow_marker

    def _overflow_payload(self) -> dict[str, int]:
        return {
            "dropped_total": sum(self._dropped.values()),
            "dropped_progress": self._dropped[BufferedEventPriority.PROGRESS],
            "dropped_state": self._dropped[BufferedEventPriority.STATE],
            "dropped_failure": self._dropped[BufferedEventPriority.FAILURE],
            "dropped_terminal": self._dropped[BufferedEventPriority.TERMINAL],
        }


def _default_progress_key(event_type: str, payload: Mapping[str, Any]) -> str:
    identity_parts = [event_type]
    for field in ("command_id", "operation_id", "workflow_run_id", "step_index", "bench_id"):
        value = payload.get(field)
        if value is not None:
            identity_parts.append(f"{field}={value}")
    return "|".join(identity_parts)


def _utc_now() -> datetime:
    return datetime.now(UTC)
