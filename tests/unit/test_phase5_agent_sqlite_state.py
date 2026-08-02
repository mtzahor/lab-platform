from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_runtime.event_buffer import (
    EVENT_BUFFER_OVERFLOW,
    AgentEventBufferRepository,
)
from lab_platform.agent_runtime.leases import (
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseStore,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.agent_runtime.sqlite_state import (
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.models import BufferedEventPriority, ReservationLease
from lab_platform.persistence import SQLiteDatabase

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _lease(
    agent_id: UUID,
    *,
    reservation_id: UUID | None = None,
    bench_id: str = "home-lab/bench-01",
    version: int = 1,
    valid_from: datetime = NOW - timedelta(minutes=1),
    valid_until: datetime = NOW + timedelta(minutes=5),
) -> ReservationLease:
    return ReservationLease(
        reservation_id=reservation_id or uuid4(),
        agent_id=agent_id,
        bench_id=bench_id,
        owner="ci-owner",
        valid_from=valid_from,
        valid_until=valid_until,
        lease_version=version,
    )


def test_sqlite_lease_store_retains_highest_version_and_tombstone_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "agent-state.db"
        agent_id = uuid4()
        reservation_id = uuid4()
        first = _lease(agent_id, reservation_id=reservation_id)

        database = _database(path)
        store: ReservationLeaseStore = SQLiteReservationLeaseStore(
            database,
            agent_id,
            clock=lambda: NOW,
        )
        assert await store.apply(first) == first
        database.close()

        reopened = _database(path)
        restarted: ReservationLeaseStore = SQLiteReservationLeaseStore(
            reopened,
            agent_id,
            clock=lambda: NOW,
        )
        assert (
            await restarted.validate(
                agent_id=agent_id,
                reservation_id=reservation_id,
                bench_id=first.bench_id,
                lease_version=1,
            )
            == first
        )

        tombstone = await restarted.release(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=first.bench_id,
            lease_version=2,
            released_at=NOW,
        )
        reopened.close()

        after_release = _database(path)
        durable = SQLiteReservationLeaseStore(after_release, agent_id, clock=lambda: NOW)
        assert await durable.get(first.bench_id) == tombstone
        assert (
            await durable.release(
                agent_id=agent_id,
                reservation_id=reservation_id,
                bench_id=first.bench_id,
                lease_version=2,
                released_at=NOW + timedelta(seconds=30),
            )
            == tombstone
        )
        with pytest.raises(ReservationLeaseVersionMismatchError, match="older"):
            await durable.apply(first)
        with pytest.raises(
            ReservationLeaseVersionMismatchError,
            match="cannot be reactivated",
        ):
            await durable.apply(_lease(agent_id, reservation_id=reservation_id, version=2))

        replacement = _lease(agent_id, reservation_id=uuid4(), version=3)
        assert await durable.apply(replacement) == replacement
        assert await durable.list() == [replacement]
        with after_release.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM agent_local_reservation_leases"
            ).fetchone()["count"]
        assert count == 3
        after_release.close()

    asyncio.run(scenario())


def test_sqlite_lease_store_preserves_validation_and_replay_rules(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        database = _database(tmp_path / "lease-validation.db")
        store = SQLiteReservationLeaseStore(database, agent_id, clock=lambda: NOW)
        lease = _lease(
            agent_id,
            valid_from=NOW + timedelta(seconds=5),
            valid_until=NOW + timedelta(seconds=30),
        )

        with pytest.raises(ReservationLeaseInvalidError, match="not valid yet"):
            await store.apply(lease)
        assert await store.apply(lease, maximum_clock_skew_seconds=5) == lease
        assert await store.apply(lease, maximum_clock_skew_seconds=5) == lease
        with pytest.raises(ReservationLeaseVersionMismatchError, match="different content"):
            await store.apply(
                lease.model_copy(update={"valid_until": NOW + timedelta(seconds=31)}),
                maximum_clock_skew_seconds=5,
            )
        with pytest.raises(ReservationLeaseInvalidError, match="different identity"):
            await store.apply(
                lease.model_copy(update={"reservation_id": uuid4()}),
                maximum_clock_skew_seconds=5,
            )
        with pytest.raises(ReservationLeaseExpiredError):
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=1,
                observed_at=lease.valid_until + timedelta(seconds=6),
                maximum_clock_skew_seconds=5,
            )
        with pytest.raises(ReservationLeaseInvalidError, match="different Agent"):
            await store.validate(
                agent_id=uuid4(),
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=1,
            )
        database.close()

    asyncio.run(scenario())


def test_sqlite_event_buffer_persists_dedup_and_progress_coalescing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "event-coalescing.db"
        agent_id = uuid4()
        first_id = uuid4()
        latest_id = uuid4()
        state_id = uuid4()

        database = _database(path)
        buffer: AgentEventBufferRepository = SQLiteAgentEventBuffer(
            database,
            agent_id,
            capacity=4,
            clock=lambda: NOW,
        )
        first = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 10},
            priority=BufferedEventPriority.PROGRESS,
            event_id=first_id,
        )
        assert (
            await buffer.append(
                "IGNORED_DUPLICATE_CONTENT",
                {"progress": 99},
                priority=BufferedEventPriority.TERMINAL,
                event_id=first_id,
            )
            == first
        )
        latest = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 20},
            priority=BufferedEventPriority.PROGRESS,
            event_id=latest_id,
        )
        state = await buffer.append(
            "OPERATION_STARTED",
            {"operation_id": "op-1"},
            event_id=state_id,
        )
        assert first is not None and latest is not None and state is not None
        database.close()

        reopened = _database(path)
        restarted = SQLiteAgentEventBuffer(
            reopened,
            agent_id,
            capacity=4,
            clock=lambda: NOW,
        )
        assert [event.sequence_number for event in await restarted.peek()] == [2, 3]
        assert (
            await restarted.append(
                "STILL_IGNORED",
                {},
                event_id=first_id,
            )
            == first
        )

        newest = await restarted.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 30},
            priority=BufferedEventPriority.PROGRESS,
            event_id=uuid4(),
        )
        assert newest is not None
        assert [event.sequence_number for event in await restarted.peek()] == [3, 4]
        stats = await restarted.stats()
        assert stats.last_issued_sequence == 4
        assert stats.coalesced_progress == 2
        reopened.close()

    asyncio.run(scenario())


def test_sqlite_event_buffer_persists_ack_watermarks_and_next_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "event-ack.db"
        agent_id = uuid4()
        database = _database(path)
        buffer = SQLiteAgentEventBuffer(database, agent_id, capacity=3, clock=lambda: NOW)
        first = await buffer.append("STATE_ONE", {}, event_id=uuid4())
        second = await buffer.append("STATE_TWO", {}, event_id=uuid4())
        assert first is not None and second is not None
        database.close()

        reopened = _database(path)
        restarted = SQLiteAgentEventBuffer(reopened, agent_id, capacity=3, clock=lambda: NOW)
        with pytest.raises(ValueError, match="has not been peeked"):
            await restarted.acknowledge_through(1)
        assert await restarted.peek(limit=1) == (first,)
        reopened.close()

        after_peek = _database(path)
        durable = SQLiteAgentEventBuffer(after_peek, agent_id, capacity=3, clock=lambda: NOW)
        assert await durable.acknowledge_through(1) == 1
        assert await durable.peek() == (second,)
        assert await durable.acknowledge_through(2) == 1
        after_peek.close()

        after_ack = _database(path)
        empty = SQLiteAgentEventBuffer(after_ack, agent_id, capacity=3, clock=lambda: NOW)
        assert await empty.acknowledge_through(2) == 0
        third = await empty.append("STATE_THREE", {}, event_id=uuid4())
        assert third is not None and third.sequence_number == 3
        assert (await empty.stats()).last_acknowledged_sequence == 2
        after_ack.close()

    asyncio.run(scenario())


def test_sqlite_event_buffer_bounds_overflow_and_marker_ack_survive_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "event-overflow.db"
        agent_id = uuid4()
        marker_id = uuid4()
        dropped_id = uuid4()
        database = _database(path)
        buffer = SQLiteAgentEventBuffer(
            database,
            agent_id,
            capacity=3,
            clock=lambda: NOW,
            event_id_factory=lambda: marker_id,
        )
        terminal = await buffer.append(
            "OPERATION_SUCCEEDED",
            {},
            priority=BufferedEventPriority.TERMINAL,
            event_id=uuid4(),
        )
        failure = await buffer.append(
            "HARDWARE_FAILURE",
            {},
            priority=BufferedEventPriority.FAILURE,
            event_id=uuid4(),
        )
        progress = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 10},
            priority=BufferedEventPriority.PROGRESS,
            event_id=uuid4(),
        )
        state = await buffer.append(
            "BENCH_HEALTH_CHANGED",
            {"bench_id": "home-lab/bench-01"},
            priority=BufferedEventPriority.STATE,
            event_id=uuid4(),
        )
        assert all(event is not None for event in (terminal, failure, progress, state))
        assert (
            await buffer.append(
                "OPERATION_PROGRESS",
                {"operation_id": "op-2", "progress": 1},
                priority=BufferedEventPriority.PROGRESS,
                event_id=dropped_id,
            )
            is None
        )
        database.close()

        reopened = _database(path)
        restarted = SQLiteAgentEventBuffer(
            reopened,
            agent_id,
            capacity=3,
            clock=lambda: NOW,
        )
        assert (
            await restarted.append(
                "DUPLICATE_DROP_IS_STILL_DROPPED",
                {},
                priority=BufferedEventPriority.TERMINAL,
                event_id=dropped_id,
            )
            is None
        )
        items = await restarted.peek(limit=10)
        markers = [event for event in items if event.event_type == EVENT_BUFFER_OVERFLOW]
        assert len(markers) == 1
        marker = markers[0]
        assert marker.id == marker_id
        assert marker.payload == {
            "dropped_total": 2,
            "dropped_progress": 2,
            "dropped_state": 0,
            "dropped_failure": 0,
            "dropped_terminal": 0,
        }
        stats = await restarted.stats()
        assert stats.buffered_data_events == restarted.capacity
        assert stats.buffered_events == restarted.capacity + 1
        assert stats.dropped_total == 2

        newer_terminal = await restarted.append(
            "WORKFLOW_COMPLETED",
            {},
            priority=BufferedEventPriority.TERMINAL,
            event_id=uuid4(),
        )
        assert newer_terminal is not None
        after_eviction = await restarted.peek(limit=10)
        assert state not in after_eviction
        assert newer_terminal in after_eviction
        assert (
            len([event for event in after_eviction if event.event_type == EVENT_BUFFER_OVERFLOW])
            == 1
        )
        assert (await restarted.stats()).dropped_state == 1

        assert await restarted.acknowledge_through(marker.sequence_number) == 3
        assert await restarted.peek(limit=10) == (newer_terminal,)
        reopened.close()

        after_marker_ack = _database(path)
        durable = SQLiteAgentEventBuffer(
            after_marker_ack,
            agent_id,
            capacity=3,
            clock=lambda: NOW,
        )
        durable_stats = await durable.stats()
        assert durable_stats.buffered_events == 1
        assert durable_stats.buffered_data_events == 1
        assert durable_stats.dropped_total == 3
        after_marker_ack.close()

    asyncio.run(scenario())


def test_sqlite_event_buffer_coordinates_repository_instances_and_capacity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "event-shared.db"
        agent_id = uuid4()
        first_database = _database(path)
        first = SQLiteAgentEventBuffer(first_database, agent_id, capacity=5, clock=lambda: NOW)
        second_database = _database(path)
        second = SQLiteAgentEventBuffer(second_database, agent_id, capacity=5, clock=lambda: NOW)
        shared_id = uuid4()

        event = await first.append("STATE", {}, event_id=shared_id)
        assert event is not None
        assert await second.append("IGNORED", {"changed": True}, event_id=shared_id) == event
        next_event = await second.append("NEXT", {}, event_id=uuid4())
        assert next_event is not None and next_event.sequence_number == 2
        assert await first.peek() == (event, next_event)

        with pytest.raises(ValueError, match="capacity does not match"):
            SQLiteAgentEventBuffer(second_database, agent_id, capacity=4)
        first_database.close()
        second_database.close()

    asyncio.run(scenario())
