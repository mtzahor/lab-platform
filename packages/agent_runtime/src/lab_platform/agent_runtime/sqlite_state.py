from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from lab_platform.agent_runtime.event_buffer import EVENT_BUFFER_OVERFLOW, EventBufferStats
from lab_platform.agent_runtime.leases import (
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseTombstone,
    ReservationLeaseVersionMismatchError,
    StoredReservationLease,
)
from lab_platform.models import BufferedAgentEvent, BufferedEventPriority, ReservationLease
from lab_platform.persistence import SQLiteDatabase

Clock = Callable[[], datetime]
EventIdFactory = Callable[[], UUID]

_EVENT_STATE_VERSION = 1
_LEASE_TOMBSTONE_OWNER = "__released_before_activation__"


class SQLiteReservationLeaseStore:
    """Schema-v7 Agent-local lease store with durable version tombstones."""

    def __init__(
        self,
        database: SQLiteDatabase,
        agent_id: UUID,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._agent_id = agent_id
        self._clock = clock or _utc_now

    @property
    def agent_id(self) -> UUID:
        return self._agent_id

    async def apply(
        self,
        lease: ReservationLease,
        *,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> StoredReservationLease:
        self._require_agent(lease.agent_id)
        observed = _as_utc(
            observed_at or self._clock(),
            field="Lease observation timestamp",
        )
        skew = _clock_skew(maximum_clock_skew_seconds)
        if lease.released_at is None:
            _require_time_valid(lease, observed, skew)
        incoming = _stored_from_lease(lease)

        with self._database.transaction(immediate=True) as connection:
            previous = _current_lease(connection, lease.bench_id)
            if previous is not None:
                self._require_agent(previous.agent_id)
                replay = _resolve_lease_version(previous, incoming)
                if replay is not None:
                    return replay
                if (
                    isinstance(incoming, ReservationLeaseTombstone)
                    and incoming.lease_version == previous.lease_version
                ):
                    connection.execute(
                        "UPDATE agent_local_reservation_leases SET released_at = ? "
                        "WHERE bench_id = ? AND lease_version = ? AND released_at IS NULL",
                        (
                            incoming.released_at.isoformat(),
                            incoming.bench_id,
                            incoming.lease_version,
                        ),
                    )
                    return incoming
            try:
                _insert_stored_lease(connection, incoming)
            except sqlite3.IntegrityError as exc:
                raise ReservationLeaseInvalidError(
                    "Reservation lease identity conflicts with durable local state.",
                    reservation_id=str(incoming.reservation_id),
                    bench_id=incoming.bench_id,
                    lease_version=incoming.lease_version,
                ) from exc
        return incoming

    async def release(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        released_at: datetime,
    ) -> ReservationLeaseTombstone:
        self._require_agent(agent_id)
        tombstone = ReservationLeaseTombstone(
            reservation_id=reservation_id,
            agent_id=agent_id,
            bench_id=bench_id,
            lease_version=lease_version,
            released_at=released_at,
        )
        with self._database.transaction(immediate=True) as connection:
            previous = _current_lease(connection, bench_id)
            if previous is not None:
                self._require_agent(previous.agent_id)
                replay = _resolve_lease_version(previous, tombstone)
                if replay is not None:
                    if isinstance(replay, ReservationLeaseTombstone):
                        return replay
                    raise AssertionError("Active lease cannot resolve a release replay")
                if previous.lease_version == lease_version:
                    connection.execute(
                        "UPDATE agent_local_reservation_leases SET released_at = ? "
                        "WHERE bench_id = ? AND lease_version = ? AND released_at IS NULL",
                        (tombstone.released_at.isoformat(), bench_id, lease_version),
                    )
                    return tombstone
            try:
                _insert_stored_lease(connection, tombstone)
            except sqlite3.IntegrityError as exc:
                raise ReservationLeaseInvalidError(
                    "Reservation release identity conflicts with durable local state.",
                    reservation_id=str(reservation_id),
                    bench_id=bench_id,
                    lease_version=lease_version,
                ) from exc
        return tombstone

    async def validate(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> ReservationLease:
        self._require_agent(agent_id)
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version < 1
        ):
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease version must be at least one.",
                received_lease_version=lease_version,
            )
        with self._database.transaction() as connection:
            record = _current_lease(connection, bench_id)
        if record is None:
            raise ReservationLeaseInvalidError(
                "No reservation lease is stored for this bench.",
                bench_id=bench_id,
                reservation_id=str(reservation_id),
            )
        _require_stored_identity(
            record,
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=bench_id,
        )
        if record.lease_version != lease_version:
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease version does not match the latest stored version.",
                bench_id=bench_id,
                expected_lease_version=record.lease_version,
                received_lease_version=lease_version,
            )
        if isinstance(record, ReservationLeaseTombstone):
            raise ReservationLeaseInvalidError(
                "Reservation lease has been released.",
                bench_id=bench_id,
                reservation_id=str(reservation_id),
                lease_version=lease_version,
            )
        observed = _as_utc(
            observed_at or self._clock(),
            field="Lease validation timestamp",
        )
        _require_time_valid(record, observed, _clock_skew(maximum_clock_skew_seconds))
        return record

    async def get(self, bench_id: str) -> StoredReservationLease | None:
        with self._database.transaction() as connection:
            record = _current_lease(connection, bench_id)
        if record is not None:
            self._require_agent(record.agent_id)
        return record

    async def list(self) -> list[StoredReservationLease]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT lease.* FROM agent_local_reservation_leases AS lease "
                "JOIN (SELECT bench_id, MAX(lease_version) AS lease_version "
                "FROM agent_local_reservation_leases GROUP BY bench_id) AS latest "
                "ON latest.bench_id = lease.bench_id "
                "AND latest.lease_version = lease.lease_version "
                "ORDER BY lease.bench_id"
            ).fetchall()
        records = [_stored_lease_from_row(row) for row in rows]
        for record in records:
            self._require_agent(record.agent_id)
        return records

    def _require_agent(self, agent_id: UUID) -> None:
        if agent_id != self._agent_id:
            raise ReservationLeaseInvalidError(
                "Reservation lease was issued to a different Agent.",
                expected_agent_id=str(self._agent_id),
                received_agent_id=str(agent_id),
            )


class SQLiteAgentEventBuffer:
    """Schema-v7 ordered event buffer with durable control metadata.

    Ordinary events live in ``agent_event_buffer``. Sequence watermarks, event-ID
    outcomes, progress coalescing references and overflow counters are stored as one
    Agent-scoped JSON value in ``agent_runtime_metadata`` and committed in the same
    immediate transaction as every row mutation.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        agent_id: UUID,
        *,
        capacity: int = 10_000,
        clock: Clock | None = None,
        event_id_factory: EventIdFactory | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("event buffer capacity must be a positive integer")
        self._database = database
        self._agent_id = agent_id
        self._capacity = capacity
        self._clock = clock or _utc_now
        self._event_id_factory = event_id_factory or uuid4
        self._metadata_key = f"agent_event_buffer:{agent_id}:state"
        with self._database.transaction(immediate=True) as connection:
            state = self._load_or_initialize_state(connection)
            if state["capacity"] != capacity:
                raise ValueError(
                    "Persisted event buffer capacity does not match configured capacity"
                )

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
        with self._database.transaction(immediate=True) as connection:
            state = self._load_state(connection)
            known = _known_events(state)
            known_value = known.get(str(identifier), _MISSING)
            if known_value is not _MISSING:
                return (
                    None if known_value is None else BufferedAgentEvent.model_validate(known_value)
                )

            event = BufferedAgentEvent(
                id=identifier,
                agent_id=self._agent_id,
                sequence_number=_take_sequence(state),
                event_type=event_type,
                payload=dict(payload),
                priority=priority,
                created_at=created_at or self._clock(),
            )
            key = (
                coalesce_key or _default_progress_key(event_type, payload)
                if priority is BufferedEventPriority.PROGRESS
                else None
            )
            coalesce_sequences = _coalesce_sequences(state)
            if key is not None:
                previous_sequence = coalesce_sequences.get(key)
                if previous_sequence is not None:
                    cursor = connection.execute(
                        "DELETE FROM agent_event_buffer WHERE agent_id = ? AND sequence_number = ?",
                        (str(self._agent_id), previous_sequence),
                    )
                    if cursor.rowcount == 1:
                        state["coalesced_progress"] = (
                            _state_int(
                                state,
                                "coalesced_progress",
                            )
                            + 1
                        )
                    coalesce_sequences.pop(key, None)

            if _ordinary_event_count(connection, self._agent_id, state) >= self._capacity:
                candidate = _lowest_priority_event(connection, self._agent_id, state)
                if candidate is None:
                    raise RuntimeError("Event buffer capacity metadata is inconsistent")
                if candidate.priority < priority:
                    connection.execute(
                        "DELETE FROM agent_event_buffer WHERE id = ?",
                        (str(candidate.id),),
                    )
                    _remove_coalescing_sequence(state, candidate.sequence_number)
                    self._record_overflow(connection, state, candidate.priority)
                else:
                    known[str(identifier)] = None
                    self._record_overflow(connection, state, priority)
                    self._save_state(connection, state)
                    return None

            try:
                _insert_event(connection, event)
            except sqlite3.IntegrityError as exc:
                raise ValueError("Event ID or sequence is already bound to other state") from exc
            known[str(identifier)] = event.model_dump(mode="json")
            if key is not None:
                coalesce_sequences[key] = event.sequence_number
            self._save_state(connection, state)
            return event

    async def peek(self, *, limit: int = 100) -> tuple[BufferedAgentEvent, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("event peek limit must be a positive integer")
        with self._database.transaction(immediate=True) as connection:
            state = self._load_state(connection)
            rows = connection.execute(
                "SELECT * FROM agent_event_buffer WHERE agent_id = ? "
                "ORDER BY sequence_number LIMIT ?",
                (str(self._agent_id), limit),
            ).fetchall()
            events = tuple(_event_from_row(row) for row in rows)
            if events:
                state["last_peeked_sequence"] = max(
                    _state_int(state, "last_peeked_sequence"),
                    events[-1].sequence_number,
                )
                self._save_state(connection, state)
            return events

    async def acknowledge_through(self, sequence_number: int) -> int:
        if (
            isinstance(sequence_number, bool)
            or not isinstance(sequence_number, int)
            or sequence_number < 0
        ):
            raise ValueError("acknowledged sequence number must be a non-negative integer")
        with self._database.transaction(immediate=True) as connection:
            state = self._load_state(connection)
            if sequence_number <= _state_int(state, "last_acknowledged_sequence"):
                return 0
            if sequence_number > _state_int(state, "last_peeked_sequence"):
                raise ValueError("cannot acknowledge an event sequence that has not been peeked")
            overflow_sequence = _overflow_sequence(connection, self._agent_id, state)
            cursor = connection.execute(
                "DELETE FROM agent_event_buffer WHERE agent_id = ? AND sequence_number <= ?",
                (str(self._agent_id), sequence_number),
            )
            _remove_coalescing_through(state, sequence_number)
            if overflow_sequence is not None and overflow_sequence <= sequence_number:
                state["overflow_marker_id"] = None
            state["last_acknowledged_sequence"] = sequence_number
            self._save_state(connection, state)
            return cursor.rowcount

    async def stats(self) -> EventBufferStats:
        with self._database.transaction() as connection:
            state = self._load_state(connection)
            buffered_events = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM agent_event_buffer WHERE agent_id = ?",
                    (str(self._agent_id),),
                ).fetchone()["count"]
            )
            buffered_data_events = _ordinary_event_count(
                connection,
                self._agent_id,
                state,
            )
        dropped = _dropped_counts(state)
        return EventBufferStats(
            buffered_events=buffered_events,
            buffered_data_events=buffered_data_events,
            last_issued_sequence=_state_int(state, "next_sequence") - 1,
            last_acknowledged_sequence=_state_int(
                state,
                "last_acknowledged_sequence",
            ),
            dropped_progress=dropped[BufferedEventPriority.PROGRESS],
            dropped_state=dropped[BufferedEventPriority.STATE],
            dropped_failure=dropped[BufferedEventPriority.FAILURE],
            dropped_terminal=dropped[BufferedEventPriority.TERMINAL],
            coalesced_progress=_state_int(state, "coalesced_progress"),
        )

    def _record_overflow(
        self,
        connection: sqlite3.Connection,
        state: dict[str, object],
        dropped_priority: BufferedEventPriority,
    ) -> None:
        dropped = _dropped_counts(state)
        dropped[dropped_priority] += 1
        state["dropped"] = {str(int(priority)): count for priority, count in dropped.items()}
        payload = _overflow_payload(dropped)
        marker_id = state.get("overflow_marker_id")
        if marker_id is None:
            marker = BufferedAgentEvent(
                id=self._event_id_factory(),
                agent_id=self._agent_id,
                sequence_number=_take_sequence(state),
                event_type=EVENT_BUFFER_OVERFLOW,
                payload=payload,
                priority=BufferedEventPriority.FAILURE,
                created_at=self._clock(),
            )
            _insert_event(connection, marker)
            state["overflow_marker_id"] = str(marker.id)
            _known_events(state)[str(marker.id)] = marker.model_dump(mode="json")
            return
        row = connection.execute(
            "SELECT * FROM agent_event_buffer WHERE id = ? AND agent_id = ?",
            (str(marker_id), str(self._agent_id)),
        ).fetchone()
        if row is None:
            raise RuntimeError("Persisted overflow marker metadata is inconsistent")
        marker = _event_from_row(row).model_copy(update={"payload": payload})
        connection.execute(
            "UPDATE agent_event_buffer SET payload_json = ? WHERE id = ?",
            (_dump_json(payload), str(marker.id)),
        )
        _known_events(state)[str(marker.id)] = marker.model_dump(mode="json")

    def _load_or_initialize_state(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT value FROM agent_runtime_metadata WHERE key = ?",
            (self._metadata_key,),
        ).fetchone()
        if row is not None:
            return _parse_event_state(row["value"], self._agent_id)
        state = _initial_event_state(self._agent_id, self._capacity)
        rows = connection.execute(
            "SELECT * FROM agent_event_buffer WHERE agent_id = ? ORDER BY sequence_number",
            (str(self._agent_id),),
        ).fetchall()
        if rows:
            events = [_event_from_row(event_row) for event_row in rows]
            state["next_sequence"] = events[-1].sequence_number + 1
            known = _known_events(state)
            coalescing = _coalesce_sequences(state)
            for event in events:
                known[str(event.id)] = event.model_dump(mode="json")
                if event.event_type == EVENT_BUFFER_OVERFLOW:
                    state["overflow_marker_id"] = str(event.id)
                    _restore_dropped_from_marker(state, event)
                elif event.priority is BufferedEventPriority.PROGRESS:
                    coalescing[_default_progress_key(event.event_type, event.payload)] = (
                        event.sequence_number
                    )
        self._save_state(connection, state)
        return state

    def _load_state(self, connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute(
            "SELECT value FROM agent_runtime_metadata WHERE key = ?",
            (self._metadata_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Event buffer metadata disappeared after initialization")
        state = _parse_event_state(row["value"], self._agent_id)
        if state["capacity"] != self._capacity:
            raise ValueError("Persisted event buffer capacity changed unexpectedly")
        return state

    def _save_state(
        self,
        connection: sqlite3.Connection,
        state: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO agent_runtime_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self._metadata_key, _dump_json(state)),
        )


SQLiteAgentEventBufferRepository = SQLiteAgentEventBuffer


_MISSING = object()


def _stored_from_lease(lease: ReservationLease) -> StoredReservationLease:
    if lease.released_at is None:
        return lease
    return ReservationLeaseTombstone(
        reservation_id=lease.reservation_id,
        agent_id=lease.agent_id,
        bench_id=lease.bench_id,
        lease_version=lease.lease_version,
        released_at=lease.released_at,
    )


def _current_lease(
    connection: sqlite3.Connection,
    bench_id: str,
) -> StoredReservationLease | None:
    row = connection.execute(
        "SELECT * FROM agent_local_reservation_leases WHERE bench_id = ? "
        "ORDER BY lease_version DESC LIMIT 1",
        (bench_id,),
    ).fetchone()
    return _stored_lease_from_row(row) if row is not None else None


def _stored_lease_from_row(row: sqlite3.Row) -> StoredReservationLease:
    released_at = _parse_optional_datetime(row["released_at"])
    if released_at is not None:
        return ReservationLeaseTombstone(
            reservation_id=UUID(row["reservation_id"]),
            agent_id=UUID(row["agent_id"]),
            bench_id=row["bench_id"],
            lease_version=row["lease_version"],
            released_at=released_at,
        )
    return ReservationLease(
        reservation_id=UUID(row["reservation_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        valid_from=_parse_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        lease_version=row["lease_version"],
    )


def _insert_stored_lease(
    connection: sqlite3.Connection,
    record: StoredReservationLease,
) -> None:
    if isinstance(record, ReservationLeaseTombstone):
        values: tuple[object, ...] = (
            str(record.reservation_id),
            record.bench_id,
            str(record.agent_id),
            _LEASE_TOMBSTONE_OWNER,
            record.released_at.isoformat(),
            record.released_at.isoformat(),
            record.lease_version,
            record.released_at.isoformat(),
        )
    else:
        values = (
            str(record.reservation_id),
            record.bench_id,
            str(record.agent_id),
            record.owner,
            record.valid_from.isoformat(),
            record.valid_until.isoformat(),
            record.lease_version,
            None,
        )
    connection.execute(
        "INSERT INTO agent_local_reservation_leases "
        "(reservation_id, bench_id, agent_id, owner, valid_from, valid_until, "
        "lease_version, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )


def _resolve_lease_version(
    previous: StoredReservationLease,
    incoming: StoredReservationLease,
) -> StoredReservationLease | None:
    if incoming.lease_version < previous.lease_version:
        raise ReservationLeaseVersionMismatchError(
            "Reservation lease version is older than the latest stored version.",
            bench_id=incoming.bench_id,
            stored_lease_version=previous.lease_version,
            received_lease_version=incoming.lease_version,
        )
    if incoming.lease_version > previous.lease_version:
        return None
    if (
        previous.agent_id != incoming.agent_id
        or previous.bench_id != incoming.bench_id
        or previous.reservation_id != incoming.reservation_id
    ):
        raise ReservationLeaseInvalidError(
            "Reservation lease version was reused for a different identity.",
            bench_id=incoming.bench_id,
            lease_version=incoming.lease_version,
        )
    if isinstance(previous, ReservationLeaseTombstone):
        if isinstance(incoming, ReservationLeaseTombstone):
            return previous
        raise ReservationLeaseVersionMismatchError(
            "A released reservation lease cannot be reactivated at the same version.",
            bench_id=incoming.bench_id,
            lease_version=incoming.lease_version,
        )
    if isinstance(incoming, ReservationLeaseTombstone):
        return None
    if previous == incoming:
        return previous
    raise ReservationLeaseVersionMismatchError(
        "Reservation lease version was reused with different content.",
        bench_id=incoming.bench_id,
        lease_version=incoming.lease_version,
    )


def _require_stored_identity(
    record: StoredReservationLease,
    *,
    agent_id: UUID,
    reservation_id: UUID,
    bench_id: str,
) -> None:
    if record.agent_id != agent_id or record.bench_id != bench_id:
        raise ReservationLeaseInvalidError(
            "Reservation lease Agent or bench identity does not match the command.",
            bench_id=bench_id,
            agent_id=str(agent_id),
        )
    if record.reservation_id != reservation_id:
        raise ReservationLeaseInvalidError(
            "Reservation lease does not belong to the command reservation.",
            bench_id=bench_id,
            reservation_id=str(reservation_id),
        )


def _require_time_valid(
    lease: ReservationLease,
    observed: datetime,
    skew: timedelta,
) -> None:
    if observed < lease.valid_from - skew:
        raise ReservationLeaseInvalidError(
            "Reservation lease is not valid yet.",
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            valid_from=lease.valid_from.isoformat(),
            observed_at=observed.isoformat(),
        )
    if observed > lease.valid_until + skew:
        raise ReservationLeaseExpiredError(
            "Reservation lease has expired.",
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            valid_until=lease.valid_until.isoformat(),
            observed_at=observed.isoformat(),
        )


def _clock_skew(maximum_clock_skew_seconds: int) -> timedelta:
    if (
        isinstance(maximum_clock_skew_seconds, bool)
        or not isinstance(maximum_clock_skew_seconds, int)
        or maximum_clock_skew_seconds < 0
    ):
        raise ValueError("maximum_clock_skew_seconds must be a non-negative integer")
    return timedelta(seconds=maximum_clock_skew_seconds)


def _initial_event_state(agent_id: UUID, capacity: int) -> dict[str, object]:
    return {
        "version": _EVENT_STATE_VERSION,
        "agent_id": str(agent_id),
        "capacity": capacity,
        "next_sequence": 1,
        "last_acknowledged_sequence": 0,
        "last_peeked_sequence": 0,
        "dropped": {str(int(priority)): 0 for priority in BufferedEventPriority},
        "coalesced_progress": 0,
        "coalesce_sequences": {},
        "overflow_marker_id": None,
        "known": {},
    }


def _parse_event_state(raw: object, agent_id: UUID) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ValueError("Persisted event buffer metadata must be text")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Persisted event buffer metadata must be an object")
    state = cast(dict[str, object], parsed)
    if state.get("version") != _EVENT_STATE_VERSION:
        raise ValueError("Persisted event buffer metadata version is unsupported")
    if state.get("agent_id") != str(agent_id):
        raise ValueError("Persisted event buffer metadata belongs to another Agent")
    for field, minimum in (
        ("capacity", 1),
        ("next_sequence", 1),
        ("last_acknowledged_sequence", 0),
        ("last_peeked_sequence", 0),
        ("coalesced_progress", 0),
    ):
        value = state.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"Persisted event buffer {field} is invalid")
    if _state_int(state, "last_acknowledged_sequence") > _state_int(
        state,
        "last_peeked_sequence",
    ):
        raise ValueError("Persisted ACK watermark is later than the peek watermark")
    _dropped_counts(state)
    _coalesce_sequences(state)
    _known_events(state)
    marker_id = state.get("overflow_marker_id")
    if marker_id is not None:
        UUID(cast(str, marker_id))
    return state


def _known_events(state: dict[str, object]) -> dict[str, object]:
    known = state.get("known")
    if not isinstance(known, dict):
        raise ValueError("Persisted known-event map is invalid")
    return cast(dict[str, object], known)


def _coalesce_sequences(state: dict[str, object]) -> dict[str, int]:
    raw = state.get("coalesce_sequences")
    if not isinstance(raw, dict):
        raise ValueError("Persisted event coalescing map is invalid")
    result = cast(dict[str, int], raw)
    if any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        for key, value in result.items()
    ):
        raise ValueError("Persisted event coalescing entry is invalid")
    return result


def _dropped_counts(
    state: dict[str, object],
) -> dict[BufferedEventPriority, int]:
    raw = state.get("dropped")
    if not isinstance(raw, dict):
        raise ValueError("Persisted event drop counters are invalid")
    result: dict[BufferedEventPriority, int] = {}
    for priority in BufferedEventPriority:
        value = raw.get(str(int(priority)))
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Persisted event drop counter is invalid")
        result[priority] = value
    return result


def _state_int(state: dict[str, object], field: str) -> int:
    value = state[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Persisted event buffer {field} is invalid")
    return value


def _take_sequence(state: dict[str, object]) -> int:
    sequence = _state_int(state, "next_sequence")
    state["next_sequence"] = sequence + 1
    return sequence


def _ordinary_event_count(
    connection: sqlite3.Connection,
    agent_id: UUID,
    state: dict[str, object],
) -> int:
    marker_id = state.get("overflow_marker_id")
    if marker_id is None:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM agent_event_buffer WHERE agent_id = ?",
            (str(agent_id),),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM agent_event_buffer WHERE agent_id = ? AND id != ?",
            (str(agent_id), str(marker_id)),
        ).fetchone()
    return int(row["count"])


def _lowest_priority_event(
    connection: sqlite3.Connection,
    agent_id: UUID,
    state: dict[str, object],
) -> BufferedAgentEvent | None:
    marker_id = state.get("overflow_marker_id")
    if marker_id is None:
        row = connection.execute(
            "SELECT * FROM agent_event_buffer WHERE agent_id = ? "
            "ORDER BY priority, sequence_number LIMIT 1",
            (str(agent_id),),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM agent_event_buffer WHERE agent_id = ? AND id != ? "
            "ORDER BY priority, sequence_number LIMIT 1",
            (str(agent_id), str(marker_id)),
        ).fetchone()
    return _event_from_row(row) if row is not None else None


def _overflow_sequence(
    connection: sqlite3.Connection,
    agent_id: UUID,
    state: dict[str, object],
) -> int | None:
    marker_id = state.get("overflow_marker_id")
    if marker_id is None:
        return None
    row = connection.execute(
        "SELECT sequence_number FROM agent_event_buffer WHERE id = ? AND agent_id = ?",
        (str(marker_id), str(agent_id)),
    ).fetchone()
    return int(row["sequence_number"]) if row is not None else None


def _remove_coalescing_sequence(state: dict[str, object], sequence_number: int) -> None:
    coalescing = _coalesce_sequences(state)
    for key in [key for key, value in coalescing.items() if value == sequence_number]:
        coalescing.pop(key)


def _remove_coalescing_through(state: dict[str, object], sequence_number: int) -> None:
    coalescing = _coalesce_sequences(state)
    for key in [key for key, value in coalescing.items() if value <= sequence_number]:
        coalescing.pop(key)


def _overflow_payload(
    dropped: dict[BufferedEventPriority, int],
) -> dict[str, int]:
    return {
        "dropped_total": sum(dropped.values()),
        "dropped_progress": dropped[BufferedEventPriority.PROGRESS],
        "dropped_state": dropped[BufferedEventPriority.STATE],
        "dropped_failure": dropped[BufferedEventPriority.FAILURE],
        "dropped_terminal": dropped[BufferedEventPriority.TERMINAL],
    }


def _restore_dropped_from_marker(
    state: dict[str, object],
    marker: BufferedAgentEvent,
) -> None:
    mapping = {
        BufferedEventPriority.PROGRESS: "dropped_progress",
        BufferedEventPriority.STATE: "dropped_state",
        BufferedEventPriority.FAILURE: "dropped_failure",
        BufferedEventPriority.TERMINAL: "dropped_terminal",
    }
    dropped: dict[str, int] = {}
    for priority, field in mapping.items():
        value = marker.payload.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Persisted overflow marker payload is invalid")
        dropped[str(int(priority))] = value
    state["dropped"] = dropped


def _insert_event(connection: sqlite3.Connection, event: BufferedAgentEvent) -> None:
    connection.execute(
        "INSERT INTO agent_event_buffer "
        "(id, agent_id, sequence_number, event_type, payload_json, priority, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(event.id),
            str(event.agent_id),
            event.sequence_number,
            event.event_type,
            _dump_json(event.payload),
            int(event.priority),
            event.created_at.isoformat(),
        ),
    )


def _event_from_row(row: sqlite3.Row) -> BufferedAgentEvent:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("Persisted buffered event payload must be an object")
    return BufferedAgentEvent(
        id=UUID(row["id"]),
        agent_id=UUID(row["agent_id"]),
        sequence_number=row["sequence_number"],
        event_type=row["event_type"],
        payload=cast(dict[str, Any], payload),
        priority=BufferedEventPriority(row["priority"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _default_progress_key(event_type: str, payload: Mapping[str, Any]) -> str:
    identity_parts = [event_type]
    for field in ("command_id", "operation_id", "workflow_run_id", "step_index", "bench_id"):
        value = payload.get(field)
        if value is not None:
            identity_parts.append(f"{field}={value}")
    return "|".join(identity_parts)


def _dump_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value), field="Persisted timestamp")


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
