from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_runtime import (
    EVENT_BUFFER_OVERFLOW,
    InMemoryAgentEventBuffer,
    InMemoryReservationLeaseStore,
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseTombstone,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.models import BufferedAgentEvent, BufferedEventPriority, ReservationLease

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


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


def test_lease_store_validates_identity_time_and_clock_skew() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        lease = _lease(
            agent_id,
            valid_from=NOW + timedelta(seconds=5),
            valid_until=NOW + timedelta(seconds=30),
        )
        store = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)

        with pytest.raises(ReservationLeaseInvalidError, match="not valid yet"):
            await store.apply(lease)
        assert await store.apply(lease, maximum_clock_skew_seconds=5) == lease
        assert await store.apply(lease, maximum_clock_skew_seconds=5) == lease
        assert (
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                observed_at=lease.valid_until + timedelta(seconds=5),
                maximum_clock_skew_seconds=5,
            )
            == lease
        )

        with pytest.raises(ReservationLeaseExpiredError):
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                observed_at=lease.valid_until + timedelta(seconds=6),
                maximum_clock_skew_seconds=5,
            )
        with pytest.raises(ReservationLeaseInvalidError, match="different Agent"):
            await store.validate(
                agent_id=uuid4(),
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
            )
        with pytest.raises(ReservationLeaseInvalidError, match="No reservation lease"):
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id="home-lab/wrong-bench",
                lease_version=lease.lease_version,
            )
        with pytest.raises(ReservationLeaseInvalidError, match="command reservation"):
            await store.validate(
                agent_id=agent_id,
                reservation_id=uuid4(),
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
            )
        with pytest.raises(ValueError, match="non-negative integer"):
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                maximum_clock_skew_seconds=-1,
            )

    asyncio.run(scenario())


def test_release_tombstone_blocks_stale_and_same_version_reactivation() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        reservation_id = uuid4()
        store = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        first = _lease(agent_id, reservation_id=reservation_id, version=1)
        await store.apply(first)

        # A release can arrive before its corresponding renewal activation. The higher-version
        # tombstone must still be retained so delayed activation messages cannot resurrect it.
        tombstone = await store.release(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=first.bench_id,
            lease_version=2,
            released_at=NOW,
        )
        assert isinstance(await store.get(first.bench_id), ReservationLeaseTombstone)
        assert (
            await store.release(
                agent_id=agent_id,
                reservation_id=reservation_id,
                bench_id=first.bench_id,
                lease_version=2,
                released_at=NOW + timedelta(seconds=1),
            )
            == tombstone
        )

        with pytest.raises(ReservationLeaseVersionMismatchError, match="older"):
            await store.apply(first)
        with pytest.raises(ReservationLeaseVersionMismatchError, match="cannot be reactivated"):
            await store.apply(_lease(agent_id, reservation_id=reservation_id, version=2))
        with pytest.raises(ReservationLeaseInvalidError, match="released"):
            await store.validate(
                agent_id=agent_id,
                reservation_id=reservation_id,
                bench_id=first.bench_id,
                lease_version=2,
            )

        replacement = _lease(agent_id, reservation_id=uuid4(), version=3)
        assert await store.apply(replacement) == replacement
        assert await store.list() == [replacement]

    asyncio.run(scenario())


def test_same_lease_version_cannot_be_reused_with_changed_content_or_identity() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        store = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        lease = _lease(agent_id)
        await store.apply(lease)

        with pytest.raises(ReservationLeaseVersionMismatchError, match="different content"):
            await store.apply(lease.model_copy(update={"valid_until": NOW + timedelta(minutes=6)}))
        with pytest.raises(ReservationLeaseInvalidError, match="different identity"):
            await store.apply(lease.model_copy(update={"reservation_id": uuid4()}))
        with pytest.raises(ReservationLeaseVersionMismatchError, match="latest stored"):
            await store.validate(
                agent_id=agent_id,
                reservation_id=lease.reservation_id,
                bench_id=lease.bench_id,
                lease_version=2,
            )

    asyncio.run(scenario())


def test_lease_store_is_safe_across_threads_and_retains_highest_version() -> None:
    agent_id = uuid4()
    reservation_id = uuid4()
    store = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)

    def apply_version(version: int) -> object:
        lease = _lease(agent_id, reservation_id=reservation_id, version=version)
        try:
            return asyncio.run(store.apply(lease))
        except ReservationLeaseVersionMismatchError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(apply_version, range(1, 51)))

    assert len(results) == 50
    current = asyncio.run(store.get("home-lab/bench-01"))
    assert isinstance(current, ReservationLease)
    assert current.lease_version == 50


def test_event_buffer_deduplicates_coalesces_and_acknowledges_in_order() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        buffer = InMemoryAgentEventBuffer(agent_id, capacity=10, clock=lambda: NOW)
        event_id = uuid4()
        first = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 10},
            priority=BufferedEventPriority.PROGRESS,
            event_id=event_id,
        )
        duplicate = await buffer.append(
            "DIFFERENT_CONTENT_IS_IGNORED_FOR_A_DUPLICATE_ID",
            {"progress": 99},
            priority=BufferedEventPriority.TERMINAL,
            event_id=event_id,
        )
        latest = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-1", "progress": 20},
            priority=BufferedEventPriority.PROGRESS,
        )
        state = await buffer.append("OPERATION_STARTED", {"operation_id": "op-1"})

        assert duplicate == first
        assert first is not None and latest is not None and state is not None
        assert [event.sequence_number for event in await buffer.peek()] == [2, 3]
        assert (await buffer.stats()).coalesced_progress == 1

        with pytest.raises(ValueError, match="has not been peeked"):
            await buffer.acknowledge_through(4)
        assert await buffer.acknowledge_through(2) == 1
        assert [event.sequence_number for event in await buffer.peek()] == [3]
        assert await buffer.acknowledge_through(2) == 0
        assert await buffer.acknowledge_through(3) == 1
        assert await buffer.peek() == ()

    asyncio.run(scenario())


def test_event_buffer_overflow_preserves_priority_and_coalesces_marker() -> None:
    async def scenario() -> None:
        buffer = InMemoryAgentEventBuffer(uuid4(), capacity=3, clock=lambda: NOW)
        terminal = await buffer.append(
            "OPERATION_SUCCEEDED",
            {"operation_id": "op-1"},
            priority=BufferedEventPriority.TERMINAL,
        )
        failure = await buffer.append(
            "HARDWARE_FAILURE",
            {"operation_id": "op-2"},
            priority=BufferedEventPriority.FAILURE,
        )
        progress = await buffer.append(
            "OPERATION_PROGRESS",
            {"operation_id": "op-3", "progress": 10},
            priority=BufferedEventPriority.PROGRESS,
        )
        state = await buffer.append(
            "BENCH_HEALTH_CHANGED",
            {"bench_id": "home-lab/bench-01"},
            priority=BufferedEventPriority.STATE,
        )
        assert all(item is not None for item in (terminal, failure, progress, state))

        # The state event evicts progress. A later progress event is itself dropped because every
        # buffered record has a higher priority. Both losses update one overflow marker.
        dropped_id = uuid4()
        assert (
            await buffer.append(
                "OPERATION_PROGRESS",
                {"operation_id": "op-4", "progress": 1},
                priority=BufferedEventPriority.PROGRESS,
                event_id=dropped_id,
            )
            is None
        )
        assert (
            await buffer.append(
                "OPERATION_PROGRESS",
                {"operation_id": "op-4", "progress": 1},
                priority=BufferedEventPriority.PROGRESS,
                event_id=dropped_id,
            )
            is None
        )

        items = await buffer.peek(limit=10)
        assert terminal in items
        assert failure in items
        assert progress not in items
        markers = [item for item in items if item.event_type == EVENT_BUFFER_OVERFLOW]
        assert len(markers) == 1
        assert markers[0].payload == {
            "dropped_total": 2,
            "dropped_progress": 2,
            "dropped_state": 0,
            "dropped_failure": 0,
            "dropped_terminal": 0,
        }
        stats = await buffer.stats()
        assert stats.buffered_data_events == buffer.capacity
        assert stats.buffered_events == buffer.capacity + 1
        assert stats.dropped_total == 2

        newer_terminal = await buffer.append(
            "WORKFLOW_COMPLETED",
            {"workflow_run_id": "run-1"},
            priority=BufferedEventPriority.TERMINAL,
        )
        assert newer_terminal is not None
        after_terminal = await buffer.peek(limit=10)
        assert terminal in after_terminal
        assert failure in after_terminal
        assert state not in after_terminal
        assert newer_terminal in after_terminal

    asyncio.run(scenario())


def test_event_buffer_assigns_unique_monotonic_sequences_under_async_and_thread_races() -> None:
    async def async_race(buffer: InMemoryAgentEventBuffer) -> list[int]:
        events = await asyncio.gather(
            *(
                buffer.append(
                    "STATE_CHANGED",
                    {"index": index},
                    event_id=uuid4(),
                )
                for index in range(100)
            )
        )
        return sorted(event.sequence_number for event in events if event is not None)

    buffer = InMemoryAgentEventBuffer(uuid4(), capacity=200, clock=lambda: NOW)
    assert asyncio.run(async_race(buffer)) == list(range(1, 101))

    shared_id = uuid4()

    def append_duplicate() -> BufferedAgentEvent | None:
        return asyncio.run(
            buffer.append(
                "TERMINAL_RESULT",
                {"result": "same"},
                priority=BufferedEventPriority.TERMINAL,
                event_id=shared_id,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: append_duplicate(), range(20)))
    assert len({event.sequence_number for event in results if event is not None}) == 1
    assert (asyncio.run(buffer.stats())).last_issued_sequence == 101
