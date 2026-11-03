from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent.distributed import (
    AgentArtifactCache,
    ArtifactTransferDescriptor,
)
from lab_platform.agent_runtime import (
    EVENT_BUFFER_OVERFLOW,
    InMemoryAgentEventBuffer,
    InMemoryReservationLeaseStore,
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseTombstone,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.agent_runtime.event_buffer import AgentEventBufferRepository
from lab_platform.agent_runtime.leases import ReservationLeaseStore
from lab_platform.agent_runtime.sqlite_state import (
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.control_plane_core.errors import ArtifactTransferFailedError
from lab_platform.models import BufferedAgentEvent, BufferedEventPriority, ReservationLease
from pydantic import SecretStr

from tests.support.persistence import open_sqlite_database

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
        # The in-memory and SQLite stores share one skew rule, so both reject
        # fractional and boolean skews as well as negative ones.
        for invalid_skew in (-1, 1.5, True):
            with pytest.raises(ValueError, match="non-negative integer"):
                await store.validate(
                    agent_id=agent_id,
                    reservation_id=lease.reservation_id,
                    bench_id=lease.bench_id,
                    lease_version=lease.lease_version,
                    maximum_clock_skew_seconds=cast(Any, invalid_skew),
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


def test_sqlite_lease_store_retains_highest_version_and_tombstone_after_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "agent-state.db"
        agent_id = uuid4()
        reservation_id = uuid4()
        first = _lease(agent_id, reservation_id=reservation_id)

        database = open_sqlite_database(path)
        store: ReservationLeaseStore = SQLiteReservationLeaseStore(
            database,
            agent_id,
            clock=lambda: NOW,
        )
        assert await store.apply(first) == first
        database.close()

        reopened = open_sqlite_database(path)
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

        after_release = open_sqlite_database(path)
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
        database = open_sqlite_database(tmp_path / "lease-validation.db")
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

        database = open_sqlite_database(path)
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

        reopened = open_sqlite_database(path)
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
        database = open_sqlite_database(path)
        buffer = SQLiteAgentEventBuffer(database, agent_id, capacity=3, clock=lambda: NOW)
        first = await buffer.append("STATE_ONE", {}, event_id=uuid4())
        second = await buffer.append("STATE_TWO", {}, event_id=uuid4())
        assert first is not None and second is not None
        database.close()

        reopened = open_sqlite_database(path)
        restarted = SQLiteAgentEventBuffer(reopened, agent_id, capacity=3, clock=lambda: NOW)
        with pytest.raises(ValueError, match="has not been peeked"):
            await restarted.acknowledge_through(1)
        assert await restarted.peek(limit=1) == (first,)
        reopened.close()

        after_peek = open_sqlite_database(path)
        durable = SQLiteAgentEventBuffer(after_peek, agent_id, capacity=3, clock=lambda: NOW)
        assert await durable.acknowledge_through(1) == 1
        assert await durable.peek() == (second,)
        assert await durable.acknowledge_through(2) == 1
        after_peek.close()

        after_ack = open_sqlite_database(path)
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
        database = open_sqlite_database(path)
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

        reopened = open_sqlite_database(path)
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

        after_marker_ack = open_sqlite_database(path)
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
        first_database = open_sqlite_database(path)
        first = SQLiteAgentEventBuffer(first_database, agent_id, capacity=5, clock=lambda: NOW)
        second_database = open_sqlite_database(path)
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


class _Transport:
    def __init__(self, content: dict[UUID, bytes]) -> None:
        self.content = content
        self.downloads: list[UUID] = []

    def download(
        self,
        descriptor: ArtifactTransferDescriptor,
        destination: Path,
        *,
        maximum_size_bytes: int,
    ) -> tuple[int, str]:
        value = self.content[descriptor.artifact_id]
        assert len(value) <= maximum_size_bytes
        destination.write_bytes(value)
        self.downloads.append(descriptor.artifact_id)
        return len(value), hashlib.sha256(value).hexdigest()

    def upload(
        self,
        url: str,
        token: str,
        source: Path,
        *,
        expected_sha256: str,
        maximum_size_bytes: int,
    ) -> None:  # pragma: no cover - cache tests exercise downloads
        raise AssertionError((url, token, source, expected_sha256, maximum_size_bytes))


def _descriptor(
    artifact_id: UUID,
    content: bytes,
    *,
    url: str = "https://control.example/api/v1/artifact-transfers/id/content",
    expires_at: datetime | None = None,
) -> ArtifactTransferDescriptor:
    return ArtifactTransferDescriptor(
        artifact_id=artifact_id,
        agent_id=uuid4(),
        transfer_id=uuid4(),
        input_name="firmware",
        download_url=url,
        transfer_token=SecretStr("secret-transfer-token"),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        target_path=f"inputs/{artifact_id}",
    )


@pytest.mark.anyio
async def test_cache_verifies_once_and_reuses_immutable_content(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"firmware"
    transport = _Transport({artifact_id: content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "wss://control.example/api/v1/agent-gateway",
        maximum_size_bytes=100,
        transport=transport,
    )
    descriptor = _descriptor(artifact_id, content)

    first = await cache.fetch(descriptor)
    second = await cache.fetch(descriptor)

    assert first == second
    assert first.read_bytes() == content
    assert transport.downloads == [artifact_id]


@pytest.mark.anyio
async def test_cache_rejects_cross_origin_and_expired_capabilities(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"firmware"
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=100,
        transport=_Transport({artifact_id: content}),
    )

    with pytest.raises(ArtifactTransferFailedError, match="configured control plane"):
        await cache.fetch(_descriptor(artifact_id, content, url="https://attacker.invalid/file"))
    with pytest.raises(ArtifactTransferFailedError, match="expired"):
        await cache.fetch(
            _descriptor(
                artifact_id,
                content,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )


@pytest.mark.anyio
async def test_cache_evicts_lru_but_never_an_active_artifact(tmp_path: Path) -> None:
    first_id, second_id = uuid4(), uuid4()
    first_content, second_content = b"1111", b"2222"
    transport = _Transport({first_id: first_content, second_id: second_content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=6,
        transport=transport,
    )
    first = _descriptor(first_id, first_content)
    second = _descriptor(second_id, second_content)

    first_path = await cache.fetch(first, pin=True)
    with pytest.raises(ArtifactTransferFailedError, match="active operations"):
        await cache.fetch(second)
    assert first_path.is_file()

    cache.release(first.sha256)
    second_path = await cache.fetch(second)
    assert second_path.read_bytes() == second_content
    assert not first_path.exists()


@pytest.mark.anyio
async def test_cache_discards_corrupt_content_before_reusing_it(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"valid-content"
    transport = _Transport({artifact_id: content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=100,
        transport=transport,
    )
    descriptor = _descriptor(artifact_id, content)
    path = await cache.fetch(descriptor)
    path.write_bytes(b"corrupt")

    restored = await cache.fetch(descriptor)

    assert restored.read_bytes() == content
    assert transport.downloads == [artifact_id, artifact_id]
