from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import CommandCancelPayload, CommandRequestPayload
from lab_platform.agent_runtime import InMemoryAgentEventBuffer, InMemoryReservationLeaseStore
from lab_platform.agent_runtime.command_handler import (
    COMMAND_ACCEPTED,
    COMMAND_REJECTED,
    OPERATION_CANCELLED,
    OPERATION_FAILED,
    OPERATION_PROGRESS,
    OPERATION_STARTED,
    OPERATION_SUCCEEDED,
    AgentCommandHandler,
    CommandProgressReporter,
)
from lab_platform.agent_runtime.command_journal import SQLiteAgentCommandJournal
from lab_platform.control_plane_core import (
    AgentRestartedDuringOperationError,
    RemoteCommandDuplicateError,
)
from lab_platform.core.errors import CapabilityNotSupportedError
from lab_platform.models import (
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)
from lab_platform.persistence import SQLiteDatabase

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")
OTHER_AGENT_ID = UUID("bfc5823d-c598-492d-9386-00922aebaf37")
RESERVATION_ID = UUID("a9e16eb6-cc01-48df-a6c7-c99ef117a002")
BENCH_ID = "home-lab/esp32-01"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeSafety:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str | None]] = []
        self.error: Exception | None = None

    async def validate(
        self,
        request: CommandRequestPayload,
        *,
        required_capability: str | None,
    ) -> None:
        self.calls.append((request.command.id, required_capability))
        if self.error is not None:
            raise self.error


class FakeExecutor:
    def __init__(self) -> None:
        self.executed: list[UUID] = []
        self.cancelled: list[tuple[UUID, UUID, str | None]] = []
        self.error: Exception | None = None

    async def execute(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any] | None:
        self.executed.append(command.id)
        if self.error is not None:
            raise self.error
        await report_progress(progress=50, message="halfway", result={"step": 1})
        return {"ok": True, "operation_id": str(local_operation_id)}

    async def cancel(
        self,
        *,
        command_id: UUID,
        local_operation_id: UUID,
        reason: str | None,
    ) -> None:
        self.cancelled.append((command_id, local_operation_id, reason))


class BlockingExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def execute(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any] | None:
        self.executed.append(command.id)
        self.started.set()
        await asyncio.Event().wait()
        return None  # pragma: no cover - cancellation always interrupts the wait


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _lease(**updates: object) -> ReservationLease:
    values: dict[str, object] = {
        "reservation_id": RESERVATION_ID,
        "agent_id": AGENT_ID,
        "bench_id": BENCH_ID,
        "owner": "ci",
        "valid_from": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=10),
        "lease_version": 3,
    }
    values.update(updates)
    return ReservationLease.model_validate(values)


def _request(**command_updates: object) -> CommandRequestPayload:
    lease = cast(
        ReservationLease | None,
        command_updates.pop("reservation_lease", _lease()),
    )
    values: dict[str, object] = {
        "id": uuid4(),
        "agent_id": AGENT_ID,
        "bench_id": BENCH_ID,
        "command_type": RemoteCommandType.RESET,
        "payload": {"mode": "hard"},
        "status": RemoteCommandStatus.DISPATCHED,
        "created_at": NOW - timedelta(seconds=30),
        "dispatched_at": NOW - timedelta(seconds=10),
        "expires_at": NOW + timedelta(minutes=5),
        "idempotency_key": f"command:{uuid4()}",
        "attempt_count": 1,
        "reservation_id": RESERVATION_ID,
        "lease_version": 3,
    }
    values.update(command_updates)
    command = RemoteCommand.model_validate(values)
    return CommandRequestPayload(command=command, reservation_lease=lease)


async def _handler(
    path: Path,
    request: CommandRequestPayload,
    *,
    safety: FakeSafety | None = None,
    executor: FakeExecutor | None = None,
    clock: MutableClock | None = None,
    buffer: InMemoryAgentEventBuffer | None = None,
    leases: InMemoryReservationLeaseStore | None = None,
) -> tuple[
    AgentCommandHandler,
    SQLiteDatabase,
    SQLiteAgentCommandJournal,
    FakeSafety,
    FakeExecutor,
    InMemoryAgentEventBuffer,
]:
    current_clock = clock or MutableClock()
    database = _database(path)
    journal = SQLiteAgentCommandJournal(database)
    lease_store = leases or InMemoryReservationLeaseStore(AGENT_ID, clock=current_clock)
    if (
        request.reservation_lease is not None
        and request.reservation_lease.agent_id == lease_store.agent_id
        and await lease_store.get(request.command.bench_id) is None
    ):
        await lease_store.apply(request.reservation_lease, observed_at=current_clock())
    event_buffer = buffer or InMemoryAgentEventBuffer(AGENT_ID, clock=current_clock)
    safety_port = safety or FakeSafety()
    local_executor = executor or FakeExecutor()
    handler = AgentCommandHandler(
        agent_id=AGENT_ID,
        journal=journal,
        leases=lease_store,
        events=event_buffer,
        safety=safety_port,
        executor=local_executor,
        clock=current_clock,
    )
    return handler, database, journal, safety_port, local_executor, event_buffer


def test_sqlite_journal_replays_after_reopen_and_rejects_fingerprint_collisions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "command-journal.db"
        request = _request()
        database = _database(path)
        journal = SQLiteAgentCommandJournal(database)

        created = await journal.create_or_replay(request, received_at=NOW)
        assert created.created
        retry_command = request.command.model_copy(update={"attempt_count": 99})
        replay = await journal.create_or_replay(
            CommandRequestPayload(
                command=retry_command, reservation_lease=request.reservation_lease
            ),
            received_at=NOW + timedelta(seconds=1),
        )
        assert replay.replayed
        assert replay.record.entry.received_at == NOW

        with pytest.raises(RemoteCommandDuplicateError):
            await journal.create_or_replay(
                CommandRequestPayload(
                    command=request.command.model_copy(update={"payload": {"mode": "soft"}}),
                    reservation_lease=request.reservation_lease,
                ),
                received_at=NOW,
            )
        with pytest.raises(RemoteCommandDuplicateError):
            await journal.create_or_replay(
                _request(
                    idempotency_key=request.command.idempotency_key,
                    payload={"mode": "different"},
                ),
                received_at=NOW,
            )

        await journal.mark_running(request.command.id, started_at=NOW + timedelta(seconds=1))
        completed = await journal.complete(
            request.command.id,
            status=RemoteCommandStatus.SUCCEEDED,
            completed_at=NOW + timedelta(seconds=2),
            result={"ok": True},
        )
        assert completed.entry.status is RemoteCommandStatus.SUCCEEDED
        database.close()

        reopened_database = _database(path)
        reopened = SQLiteAgentCommandJournal(reopened_database)
        stored = await reopened.find_replay(request)
        assert stored is not None
        assert stored.entry.status is RemoteCommandStatus.SUCCEEDED
        assert stored.entry.result == {"ok": True}
        assert (
            await reopened.mark_running(request.command.id, started_at=NOW)
        ).entry == stored.entry
        reopened_database.close()

    asyncio.run(scenario())


def test_sqlite_journal_atomically_fences_concurrent_identical_creates(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-journal.db"
    request = _request()
    first_database = _database(path)
    second_database = _database(path)
    first = SQLiteAgentCommandJournal(first_database)
    second = SQLiteAgentCommandJournal(second_database)

    def create(journal: SQLiteAgentCommandJournal) -> bool:
        return asyncio.run(journal.create_or_replay(request, received_at=NOW)).created

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, (first, second)))

    assert sorted(outcomes) == [False, True]
    assert len(asyncio.run(first.list())) == 1
    first_database.close()
    second_database.close()


def test_handler_journals_before_execution_emits_ordered_progress_and_replays_terminal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "handler-success.db"
        request = _request()
        handler, database, journal, safety, executor, events = await _handler(path, request)

        result = await handler.handle(request)
        assert result.is_accepted
        assert not result.replayed
        assert result.journal_entry is not None
        assert result.journal_entry.status is RemoteCommandStatus.SUCCEEDED
        assert executor.executed == [request.command.id]
        assert safety.calls == [(request.command.id, "reset")]
        stored = await journal.get(request.command.id)
        assert stored is not None and stored.entry.status is RemoteCommandStatus.SUCCEEDED
        event_types = [event.event_type for event in await events.peek(limit=20)]
        assert event_types == [
            COMMAND_ACCEPTED,
            OPERATION_STARTED,
            OPERATION_PROGRESS,
            OPERATION_SUCCEEDED,
        ]
        database.close()

        reopened_database = _database(path)
        reopened_journal = SQLiteAgentCommandJournal(reopened_database)
        replay_events = InMemoryAgentEventBuffer(AGENT_ID, clock=MutableClock())
        replay_executor = FakeExecutor()
        empty_leases = InMemoryReservationLeaseStore(AGENT_ID, clock=MutableClock())
        replay_handler = AgentCommandHandler(
            agent_id=AGENT_ID,
            journal=reopened_journal,
            leases=empty_leases,
            events=replay_events,
            safety=FakeSafety(),
            executor=replay_executor,
            clock=MutableClock(NOW + timedelta(days=1)),
        )
        replay_result = await replay_handler.handle(request)
        assert replay_result.replayed
        assert replay_result.journal_entry is not None
        assert replay_result.journal_entry.status is RemoteCommandStatus.SUCCEEDED
        assert replay_executor.executed == []
        assert [event.event_type for event in await replay_events.peek(limit=10)] == [
            COMMAND_ACCEPTED,
            OPERATION_SUCCEEDED,
        ]
        reopened_database.close()

    asyncio.run(scenario())


def test_handler_replays_accepted_and_running_across_same_boot_reopen_without_execution(
    tmp_path: Path,
) -> None:
    def replay_handler(
        journal: SQLiteAgentCommandJournal,
        events: InMemoryAgentEventBuffer,
        executor: FakeExecutor,
    ) -> AgentCommandHandler:
        safety = FakeSafety()
        safety.error = AssertionError("replay must not be revalidated")
        return AgentCommandHandler(
            agent_id=AGENT_ID,
            journal=journal,
            leases=InMemoryReservationLeaseStore(AGENT_ID, clock=MutableClock()),
            events=events,
            safety=safety,
            executor=executor,
            clock=MutableClock(NOW + timedelta(days=1)),
        )

    async def scenario() -> None:
        path = tmp_path / "handler-inflight-replay.db"
        request = _request()
        boot_id = uuid4()
        database = _database(path)
        journal = SQLiteAgentCommandJournal(database)
        assert not await journal.recover_interrupted(boot_id=boot_id, recovered_at=NOW)
        await journal.create_or_replay(request, received_at=NOW)
        database.close()

        accepted_database = _database(path)
        accepted_journal = SQLiteAgentCommandJournal(accepted_database)
        assert not await accepted_journal.recover_interrupted(
            boot_id=boot_id,
            recovered_at=NOW + timedelta(seconds=1),
        )
        accepted_events = InMemoryAgentEventBuffer(AGENT_ID, clock=MutableClock())
        accepted_executor = FakeExecutor()
        accepted_handler = replay_handler(
            accepted_journal,
            accepted_events,
            accepted_executor,
        )
        accepted = await accepted_handler.handle(request)
        assert accepted.replayed
        assert accepted.journal_entry is not None
        assert accepted.journal_entry.status is RemoteCommandStatus.ACCEPTED
        assert accepted_executor.executed == []
        assert [event.event_type for event in await accepted_events.peek()] == [COMMAND_ACCEPTED]
        await accepted_journal.mark_running(
            request.command.id,
            started_at=NOW + timedelta(seconds=1),
        )
        accepted_database.close()

        running_database = _database(path)
        running_journal = SQLiteAgentCommandJournal(running_database)
        assert not await running_journal.recover_interrupted(
            boot_id=boot_id,
            recovered_at=NOW + timedelta(seconds=2),
        )
        running_events = InMemoryAgentEventBuffer(AGENT_ID, clock=MutableClock())
        running_executor = FakeExecutor()
        running_handler = replay_handler(
            running_journal,
            running_events,
            running_executor,
        )
        running = await running_handler.handle(request)
        assert running.replayed
        assert running.journal_entry is not None
        assert running.journal_entry.status is RemoteCommandStatus.RUNNING
        assert running_executor.executed == []
        assert [event.event_type for event in await running_events.peek()] == [
            COMMAND_ACCEPTED,
            OPERATION_STARTED,
        ]
        running_database.close()

    asyncio.run(scenario())


def test_new_boot_atomically_fails_active_journal_and_replays_terminal_history(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "journal-new-boot.db"
        accepted_request = _request()
        running_request = _request()
        succeeded_request = _request()
        old_boot = uuid4()
        new_boot = uuid4()

        database = _database(path)
        journal = SQLiteAgentCommandJournal(database)
        assert not await journal.recover_interrupted(boot_id=old_boot, recovered_at=NOW)
        await journal.create_or_replay(accepted_request, received_at=NOW)
        await journal.create_or_replay(running_request, received_at=NOW)
        await journal.mark_running(
            running_request.command.id,
            started_at=NOW + timedelta(seconds=1),
        )
        await journal.create_or_replay(succeeded_request, received_at=NOW)
        await journal.complete(
            succeeded_request.command.id,
            status=RemoteCommandStatus.SUCCEEDED,
            completed_at=NOW + timedelta(seconds=2),
            result={"ok": True},
        )
        database.close()

        restarted_database = _database(path)
        restarted_journal = SQLiteAgentCommandJournal(restarted_database)
        interrupted = await restarted_journal.recover_interrupted(
            boot_id=new_boot,
            recovered_at=NOW + timedelta(minutes=1),
        )

        assert {record.entry.command_id for record in interrupted} == {
            accepted_request.command.id,
            running_request.command.id,
        }
        for request in (accepted_request, running_request):
            record = await restarted_journal.get(request.command.id)
            assert record is not None
            assert record.entry.status is RemoteCommandStatus.FAILED
            assert record.entry.completed_at == NOW + timedelta(minutes=1)
            assert record.entry.error_code == AgentRestartedDuringOperationError.code
        succeeded = await restarted_journal.get(succeeded_request.command.id)
        assert succeeded is not None
        assert succeeded.entry.status is RemoteCommandStatus.SUCCEEDED
        assert succeeded.entry.result == {"ok": True}
        assert not await restarted_journal.recover_interrupted(
            boot_id=new_boot,
            recovered_at=NOW + timedelta(minutes=2),
        )

        replay_events = InMemoryAgentEventBuffer(AGENT_ID, clock=MutableClock())
        replay_executor = FakeExecutor()
        replay_handler = replay_handler_for_journal(
            restarted_journal,
            replay_events,
            replay_executor,
        )
        replayed = await replay_handler.handle(running_request)
        assert replayed.replayed
        assert replayed.journal_entry is not None
        assert replayed.journal_entry.status is RemoteCommandStatus.FAILED
        assert replay_executor.executed == []
        assert [event.event_type for event in await replay_events.peek(limit=10)] == [
            COMMAND_ACCEPTED,
            OPERATION_FAILED,
        ]
        restarted_database.close()

    def replay_handler_for_journal(
        journal: SQLiteAgentCommandJournal,
        events: InMemoryAgentEventBuffer,
        executor: FakeExecutor,
    ) -> AgentCommandHandler:
        safety = FakeSafety()
        safety.error = AssertionError("replay must not be revalidated")
        return AgentCommandHandler(
            agent_id=AGENT_ID,
            journal=journal,
            leases=InMemoryReservationLeaseStore(AGENT_ID, clock=MutableClock()),
            events=events,
            safety=safety,
            executor=executor,
            clock=MutableClock(NOW + timedelta(days=1)),
        )

    asyncio.run(scenario())


def test_handler_rejects_agent_expiry_drain_missing_lease_and_safety_before_journal(
    tmp_path: Path,
) -> None:
    async def assert_rejected(
        request: CommandRequestPayload,
        expected_code: str,
        *,
        draining: bool = False,
        safety_error: Exception | None = None,
    ) -> None:
        safety = FakeSafety()
        safety.error = safety_error
        handler, database, journal, _, executor, events = await _handler(
            tmp_path / f"{uuid4()}.db",
            request,
            safety=safety,
        )
        handler.set_draining(draining)
        result = await handler.handle(request)
        assert result.rejected is not None
        assert result.rejected.error_code == expected_code
        assert result.journal_entry is None
        assert executor.executed == []
        assert await journal.get(request.command.id) is None
        assert [event.event_type for event in await events.peek()] == [COMMAND_REJECTED]
        database.close()

    async def scenario() -> None:
        other_lease = _lease(agent_id=OTHER_AGENT_ID)
        await assert_rejected(
            _request(agent_id=OTHER_AGENT_ID, reservation_lease=other_lease),
            "BENCH_AGENT_MISMATCH",
        )
        await assert_rejected(
            _request(
                created_at=NOW - timedelta(minutes=2),
                dispatched_at=NOW - timedelta(minutes=1),
                expires_at=NOW - timedelta(seconds=1),
            ),
            "REMOTE_COMMAND_EXPIRED",
        )
        await assert_rejected(_request(), "AGENT_DRAINING", draining=True)
        await assert_rejected(
            _request(reservation_id=None, lease_version=None, reservation_lease=None),
            "RESERVATION_LEASE_INVALID",
        )
        await assert_rejected(
            _request(),
            "CAPABILITY_NOT_SUPPORTED",
            safety_error=CapabilityNotSupportedError("Reset capability is unavailable."),
        )
        await assert_rejected(
            _request(),
            "REMOTE_COMMAND_REJECTED",
            safety_error=ValueError("malformed reset payload"),
        )

    asyncio.run(scenario())


def test_handler_preserves_stable_executor_failure_without_leaking_exception_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request = _request()
        executor = FakeExecutor()
        executor.error = RuntimeError("do-not-leak --password super-secret")
        handler, database, journal, _, _, events = await _handler(
            tmp_path / "handler-failure.db",
            request,
            executor=executor,
        )

        result = await handler.handle(request)
        assert result.journal_entry is not None
        assert result.journal_entry.status is RemoteCommandStatus.FAILED
        assert result.journal_entry.error_code == "REMOTE_COMMAND_REJECTED"
        assert result.journal_entry.error_message == "Local command execution failed."
        assert "super-secret" not in result.journal_entry.error_message
        stored = await journal.get(request.command.id)
        assert stored is not None and stored.entry == result.journal_entry
        event_types = [event.event_type for event in await events.peek(limit=10)]
        assert event_types == [COMMAND_ACCEPTED, OPERATION_STARTED, OPERATION_FAILED]
        database.close()

    asyncio.run(scenario())


def test_handler_cancellation_calls_executor_and_durably_records_terminal_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request = _request(command_type=RemoteCommandType.RUN_WORKFLOW)
        executor = BlockingExecutor()
        handler, database, journal, _, _, events = await _handler(
            tmp_path / "handler-cancel.db",
            request,
            executor=executor,
        )

        execution = asyncio.create_task(handler.handle(request))
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        cancelled = await handler.cancel(
            CommandCancelPayload(command_id=request.command.id, reason="operator requested")
        )
        result = await execution

        assert cancelled.status is RemoteCommandStatus.CANCELLED
        assert result.journal_entry is not None
        assert result.journal_entry.status is RemoteCommandStatus.CANCELLED
        assert result.journal_entry.error_message == "operator requested"
        assert len(executor.cancelled) == 1
        assert executor.cancelled[0][0] == request.command.id
        stored = await journal.get(request.command.id)
        assert stored is not None and stored.entry.status is RemoteCommandStatus.CANCELLED
        assert [event.event_type for event in await events.peek(limit=10)] == [
            COMMAND_ACCEPTED,
            OPERATION_STARTED,
            OPERATION_CANCELLED,
        ]
        database.close()

    asyncio.run(scenario())


def test_dispatch_persists_acceptance_before_execution_and_survives_crash_boundary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "dispatch-crash-boundary.db"
        request = _request()
        executor = BlockingExecutor()
        handler, database, journal, _, _, events = await _handler(
            path,
            request,
            executor=executor,
        )

        dispatch = await handler.dispatch(request)

        assert dispatch.execution is not None
        assert dispatch.initial_result.journal_entry is not None
        assert dispatch.initial_result.journal_entry.status is RemoteCommandStatus.ACCEPTED
        stored = await journal.get(request.command.id)
        assert stored is not None and stored.entry.status is RemoteCommandStatus.ACCEPTED
        assert executor.executed == []
        assert [event.event_type for event in await events.peek()] == [COMMAND_ACCEPTED]

        # This cancellation models process loss before the independently owned execution task gets
        # its first event-loop turn. No transport sequence acknowledgment can precede the journal.
        dispatch.execution.cancel()
        await asyncio.gather(dispatch.execution, return_exceptions=True)
        database.close()

        reopened_database = _database(path)
        reopened = SQLiteAgentCommandJournal(reopened_database)
        after_restart = await reopened.get(request.command.id)
        assert after_restart is not None
        assert after_restart.entry.status is RemoteCommandStatus.ACCEPTED
        reopened_database.close()

    asyncio.run(scenario())


def test_immediate_cancel_after_dispatch_cannot_race_ahead_of_journal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request = _request(command_type=RemoteCommandType.RUN_WORKFLOW)
        executor = BlockingExecutor()
        handler, database, journal, _, _, events = await _handler(
            tmp_path / "dispatch-immediate-cancel.db",
            request,
            executor=executor,
        )

        dispatch = await handler.dispatch(request)
        cancelled = await handler.cancel(
            CommandCancelPayload(command_id=request.command.id, reason="cancel before start")
        )

        assert dispatch.execution is not None and dispatch.execution.cancelled()
        assert cancelled.status is RemoteCommandStatus.CANCELLED
        assert cancelled.error_message == "cancel before start"
        assert executor.executed == []
        assert [item[0] for item in executor.cancelled] == [request.command.id]
        stored = await journal.get(request.command.id)
        assert stored is not None and stored.entry.status is RemoteCommandStatus.CANCELLED
        assert [event.event_type for event in await events.peek(limit=10)] == [
            COMMAND_ACCEPTED,
            OPERATION_CANCELLED,
        ]
        database.close()

    asyncio.run(scenario())
