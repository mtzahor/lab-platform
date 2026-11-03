from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    AgentStatus,
    AgentStatusPayload,
    CommandCancelPayload,
    CommandRequestPayload,
    DrainAgentPayload,
    EventAckPayload,
    MessageType,
    ReconciliationRequestPayload,
    ReservationActivatedPayload,
    ReservationReleasedPayload,
    WelcomePayload,
    parse_control_plane_message,
)
from lab_platform.agent_runtime import (
    AgentCommandJournalRepository,
    AgentConnectionError,
    AgentConnectionManager,
    AgentDrainPort,
    AgentHandshakeError,
    AgentHeartbeatSource,
    AgentHeartbeatState,
    AgentOutgoingQueueFullError,
    AgentReconciliationReportBuilder,
    AgentTransportSecurityError,
    CommandDispatchResult,
    CommandHandlingResult,
    InMemoryAgentEventBuffer,
    InMemoryReservationLeaseStore,
    StoredCommandJournalEntry,
)
from lab_platform.agent_runtime.connection_manager import AgentWebSocket, WebSocketConnector
from lab_platform.models import (
    CommandJournalEntry,
    GlobalBenchKind,
    GlobalBenchStatus,
    HealthStatus,
    ReconciliationBenchSnapshot,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)
from pydantic import SecretStr

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


class FakeJournal:
    def __init__(self, records: Sequence[StoredCommandJournalEntry] = ()) -> None:
        self.records = list(records)

    async def list(
        self,
        *,
        status: RemoteCommandStatus | None = None,
        limit: int = 10_000,
    ) -> list[StoredCommandJournalEntry]:
        selected = [
            record for record in self.records if status is None or record.entry.status is status
        ]
        return selected[:limit]


class FakeInventory:
    def __init__(self, benches: Sequence[ReconciliationBenchSnapshot] = ()) -> None:
        self.benches = tuple(benches)

    async def snapshot(self) -> Sequence[ReconciliationBenchSnapshot]:
        return self.benches


class FakeCommands:
    def __init__(self) -> None:
        self.handled: list[CommandRequestPayload] = []
        self.cancelled: list[CommandCancelPayload] = []

    async def dispatch(self, request: CommandRequestPayload) -> CommandDispatchResult:
        self.handled.append(request)
        return CommandDispatchResult(
            initial_result=CommandHandlingResult(
                journal_entry=None,
                accepted=None,
                rejected=None,
                replayed=False,
                local_operation_id=None,
            ),
            execution=None,
        )

    async def cancel(self, payload: CommandCancelPayload) -> object:
        self.cancelled.append(payload)
        return payload.command_id


class GatedCommands(FakeCommands):
    def __init__(self) -> None:
        super().__init__()
        self.dispatch_started = asyncio.Event()
        self.allow_durable_acceptance = asyncio.Event()
        self.durable = False
        self.cancel_observed_durable: list[bool] = []

    async def dispatch(self, request: CommandRequestPayload) -> CommandDispatchResult:
        self.dispatch_started.set()
        await self.allow_durable_acceptance.wait()
        self.durable = True
        return await super().dispatch(request)

    async def cancel(self, payload: CommandCancelPayload) -> object:
        self.cancel_observed_durable.append(self.durable)
        return await super().cancel(payload)


class FakeDrain(AgentDrainPort):
    def __init__(self) -> None:
        self.requests: list[DrainAgentPayload] = []

    async def apply_drain(self, payload: DrainAgentPayload) -> None:
        self.requests.append(payload)


class FakeHeartbeat(AgentHeartbeatSource):
    async def heartbeat_state(self) -> AgentHeartbeatState:
        return AgentHeartbeatState(
            active_operations=2,
            connected_benches=5,
            degraded_benches=1,
        )


class SocketClosed(ConnectionError):
    pass


class FakeSocket:
    def __init__(
        self,
        *,
        on_hello: Callable[[dict[str, Any]], Sequence[str]],
        fail_first_event_batch: bool = False,
        acknowledge_event_batches: bool = True,
    ) -> None:
        self._on_hello = on_hello
        self._incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self._changed = asyncio.Event()
        self._fail_first_event_batch = fail_first_event_batch
        self._failed_event_batch = False
        self._acknowledge_event_batches = acknowledge_event_batches
        self._next_control_plane_sequence = 1
        self.sent: list[dict[str, Any]] = []
        self.attempted: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        value = cast(dict[str, Any], json.loads(message))
        self.attempted.append(value)
        if (
            value["message_type"] == MessageType.EVENT_BATCH.value
            and self._fail_first_event_batch
            and not self._failed_event_batch
        ):
            self._failed_event_batch = True
            raise ConnectionError("injected event send failure")
        self.sent.append(value)
        if value["message_type"] == MessageType.AGENT_HELLO.value:
            for inbound in self._on_hello(value):
                self._incoming.put_nowait(inbound)
                decoded = cast(dict[str, Any], json.loads(inbound))
                self._next_control_plane_sequence = max(
                    self._next_control_plane_sequence,
                    cast(int, decoded["sequence_number"]),
                )
        elif (
            value["message_type"] == MessageType.EVENT_BATCH.value
            and self._acknowledge_event_batches
        ):
            self._next_control_plane_sequence += 1
            batch_message_id = UUID(cast(str, value["message_id"]))
            events = cast(list[dict[str, Any]], value["payload"]["events"])
            self._incoming.put_nowait(
                _control_message(
                    UUID(cast(str, value["agent_id"])),
                    MessageType.EVENT_ACK,
                    EventAckPayload(
                        batch_message_id=batch_message_id,
                        acknowledged_event_sequence=cast(
                            int,
                            events[-1]["sequence_number"],
                        ),
                    ),
                    sequence_number=self._next_control_plane_sequence,
                    correlation_id=batch_message_id,
                )
            )
        self._changed.set()

    async def recv(self) -> str | bytes:
        value = await self._incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._incoming.put_nowait(SocketClosed("socket closed"))
        self._changed.set()

    def queue_incoming(self, message: str | bytes | BaseException) -> None:
        self._incoming.put_nowait(message)

    async def wait_for_message(self, message_type: MessageType, *, timeout: float = 1.0) -> None:
        async def wait() -> None:
            while not any(value["message_type"] == message_type.value for value in self.sent):
                self._changed.clear()
                if any(value["message_type"] == message_type.value for value in self.sent):
                    return
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)


class FakeConnector:
    def __init__(self, sockets: Sequence[FakeSocket]) -> None:
        self.sockets = list(sockets)
        self.calls: list[tuple[str, Mapping[str, str], int]] = []

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket:
        self.calls.append((url, dict(headers), maximum_message_size_bytes))
        if not self.sockets:
            raise ConnectionError("no fake socket remains")
        return self.sockets.pop(0)


class AlwaysFailConnector:
    def __init__(self) -> None:
        self.calls = 0

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket:
        del url, headers, maximum_message_size_bytes
        self.calls += 1
        raise ConnectionError("control plane unavailable")


def _stored(entry: CommandJournalEntry) -> StoredCommandJournalEntry:
    return StoredCommandJournalEntry(entry=entry, command_fingerprint=entry.command_id.hex)


def _report_builder(
    agent_id: UUID,
    boot_id: UUID,
    leases: InMemoryReservationLeaseStore,
    events: InMemoryAgentEventBuffer,
    *,
    records: Sequence[StoredCommandJournalEntry] = (),
    benches: Sequence[ReconciliationBenchSnapshot] = (),
    recent_command_limit: int = 1_000,
) -> AgentReconciliationReportBuilder:
    journal = cast(AgentCommandJournalRepository, FakeJournal(records))
    return AgentReconciliationReportBuilder(
        agent_id=agent_id,
        boot_id=boot_id,
        journal=journal,
        leases=leases,
        inventory=FakeInventory(benches),
        events=events,
        recent_command_limit=recent_command_limit,
        clock=lambda: NOW,
    )


def _manager(
    *,
    agent_id: UUID,
    boot_id: UUID,
    events: InMemoryAgentEventBuffer,
    leases: InMemoryReservationLeaseStore,
    connector: WebSocketConnector,
    commands: FakeCommands | None = None,
    drain: FakeDrain | None = None,
    gateway_url: str = "wss://lab.example/api/v1/agent-gateway",
    credential: str | SecretStr = "agent-secret",
    outgoing_queue_size: int = 16,
    maximum_message_size_bytes: int = 2 * 1024 * 1024,
    event_poll_interval_seconds: float = 0.25,
    event_ack_timeout_seconds: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] = lambda: 100.0,
    allow_insecure_loopback: bool = False,
    heartbeat: AgentHeartbeatSource | None = None,
) -> AgentConnectionManager:
    return AgentConnectionManager(
        agent_id=agent_id,
        boot_id=boot_id,
        agent_name="home-lab",
        agent_version="0.6.0-alpha",
        gateway_url=gateway_url,
        credential=credential,
        events=events,
        leases=leases,
        commands=commands or FakeCommands(),
        drain=drain or FakeDrain(),
        reconciliation=_report_builder(agent_id, boot_id, leases, events),
        connector=connector,
        heartbeat=heartbeat,
        outgoing_queue_size=outgoing_queue_size,
        event_poll_interval_seconds=event_poll_interval_seconds,
        event_ack_timeout_seconds=event_ack_timeout_seconds,
        maximum_message_size_bytes=maximum_message_size_bytes,
        reconnect_jitter_ratio=0,
        allow_insecure_loopback=allow_insecure_loopback,
        clock=lambda: NOW,
        monotonic=monotonic,
        sleep=sleep,
    )


def _control_message(
    agent_id: UUID,
    message_type: MessageType,
    payload: object,
    *,
    sequence_number: int,
    correlation_id: UUID | None = None,
    message_id: UUID | None = None,
) -> str:
    envelope = parse_control_plane_message(
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": message_id or uuid4(),
            "message_type": message_type,
            "agent_id": agent_id,
            "sent_at": NOW,
            "correlation_id": correlation_id,
            "sequence_number": sequence_number,
            "payload": payload,
        }
    )
    return envelope.model_dump_json()


def _welcome(agent_id: UUID, hello: Mapping[str, Any]) -> str:
    return _control_message(
        agent_id,
        MessageType.WELCOME,
        WelcomePayload(
            connection_id=uuid4(),
            accepted_protocol_version=PROTOCOL_VERSION,
            server_time=NOW,
            heartbeat_interval_seconds=10,
            heartbeat_timeout_seconds=30,
            offline_timeout_seconds=60,
        ),
        sequence_number=1,
        correlation_id=UUID(cast(str, hello["message_id"])),
    )


def _command(agent_id: UUID, command_id: UUID) -> RemoteCommand:
    return RemoteCommand(
        id=command_id,
        agent_id=agent_id,
        bench_id="home-lab/bench-a",
        command_type=RemoteCommandType.PROBE,
        status=RemoteCommandStatus.DISPATCHED,
        created_at=NOW - timedelta(minutes=1),
        dispatched_at=NOW - timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key=f"command-{command_id}",
    )


def test_reconciliation_builder_reports_durable_agent_truth_deterministically() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()
        accepted = CommandJournalEntry(
            command_id=UUID(int=1),
            idempotency_key="accepted",
            command_type=RemoteCommandType.PROBE,
            bench_id="home-lab/bench-a",
            status=RemoteCommandStatus.ACCEPTED,
            received_at=NOW - timedelta(minutes=3),
        )
        succeeded = CommandJournalEntry(
            command_id=UUID(int=2),
            idempotency_key="succeeded",
            command_type=RemoteCommandType.RESET,
            bench_id="home-lab/bench-b",
            status=RemoteCommandStatus.SUCCEEDED,
            received_at=NOW - timedelta(minutes=4),
            started_at=NOW - timedelta(minutes=2),
            completed_at=NOW - timedelta(minutes=1),
            result={"reset": True},
        )
        latest_completion = CommandJournalEntry(
            command_id=UUID(int=3),
            idempotency_key="latest-completion",
            command_type=RemoteCommandType.RESET,
            bench_id="home-lab/bench-c",
            status=RemoteCommandStatus.SUCCEEDED,
            received_at=NOW - timedelta(hours=1),
            started_at=NOW - timedelta(minutes=10),
            completed_at=NOW,
            result={"latest": True},
        )
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        active_lease = ReservationLease(
            reservation_id=uuid4(),
            agent_id=agent_id,
            bench_id="home-lab/bench-a",
            owner="ci:test",
            valid_from=NOW - timedelta(minutes=1),
            valid_until=NOW + timedelta(minutes=5),
            lease_version=1,
        )
        await leases.apply(active_lease)
        await leases.release(
            agent_id=agent_id,
            reservation_id=uuid4(),
            bench_id="home-lab/released",
            lease_version=1,
            released_at=NOW,
        )
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        await events.append("HARDWARE_ERROR", {"code": "probe-timeout"})
        bench = ReconciliationBenchSnapshot(
            local_bench_id="bench-a",
            name="Bench A",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            capabilities=frozenset({"probe"}),
        )
        builder = _report_builder(
            agent_id,
            boot_id,
            leases,
            events,
            records=(_stored(succeeded), _stored(latest_completion), _stored(accepted)),
            benches=(bench,),
            recent_command_limit=1,
        )

        report = await builder.build()

        assert report.agent_id == agent_id
        assert report.boot_id == boot_id
        assert report.generated_at == NOW
        assert [(item.command_id, item.updated_at) for item in report.active_commands] == [
            (accepted.command_id, accepted.received_at)
        ]
        assert report.recent_commands[0].command_id == latest_completion.command_id
        assert report.recent_commands[0].result == {"latest": True}
        assert report.local_reservation_leases == (active_lease,)
        assert report.bench_snapshots == (bench,)
        assert report.buffered_event_count == 1

    asyncio.run(scenario())


def test_connection_dispatches_ordered_control_messages_and_reconciliation() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()
        command_id = uuid4()
        activation_message_id = uuid4()
        lease = ReservationLease(
            reservation_id=uuid4(),
            agent_id=agent_id,
            bench_id="home-lab/bench-a",
            owner="ci:test",
            valid_from=NOW - timedelta(minutes=1),
            valid_until=NOW + timedelta(minutes=5),
            lease_version=1,
        )

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (
                _welcome(agent_id, hello),
                _control_message(
                    agent_id,
                    MessageType.RESERVATION_ACTIVATED,
                    ReservationActivatedPayload(lease=lease),
                    sequence_number=2,
                    message_id=activation_message_id,
                ),
                _control_message(
                    agent_id,
                    MessageType.COMMAND_REQUEST,
                    CommandRequestPayload(command=_command(agent_id, command_id)),
                    sequence_number=3,
                ),
                _control_message(
                    agent_id,
                    MessageType.COMMAND_CANCEL,
                    CommandCancelPayload(command_id=command_id, reason="operator request"),
                    sequence_number=4,
                ),
                _control_message(
                    agent_id,
                    MessageType.DRAIN_AGENT,
                    DrainAgentPayload(drain=True, deadline=NOW + timedelta(minutes=1)),
                    sequence_number=5,
                ),
                _control_message(
                    agent_id,
                    MessageType.RECONCILIATION_REQUEST,
                    ReconciliationRequestPayload(
                        request_id=UUID(int=900),
                        expected_boot_id=boot_id,
                        last_control_plane_sequence=0,
                    ),
                    sequence_number=6,
                ),
                _control_message(
                    agent_id,
                    MessageType.RESERVATION_RELEASED,
                    ReservationReleasedPayload(
                        reservation_id=lease.reservation_id,
                        bench_id=lease.bench_id,
                        lease_version=lease.lease_version,
                        released_at=NOW,
                    ),
                    sequence_number=7,
                ),
            )

        socket = FakeSocket(on_hello=on_hello)
        connector = FakeConnector((socket,))
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        commands = FakeCommands()
        drain = FakeDrain()
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=connector,
            commands=commands,
            drain=drain,
        )

        run = asyncio.create_task(manager.run_once())
        await socket.wait_for_message(MessageType.RECONCILIATION_REPORT)
        while manager.last_control_plane_sequence < 7:
            await asyncio.sleep(0)
        await manager.wait_for_command_tasks()
        await manager.stop()

        assert await run
        assert [request.command.id for request in commands.handled] == [command_id]
        assert [request.command_id for request in commands.cancelled] == [command_id]
        assert drain.requests == [
            DrainAgentPayload(drain=True, deadline=NOW + timedelta(minutes=1))
        ]
        stored = await leases.get(lease.bench_id)
        assert stored is not None and not isinstance(stored, ReservationLease)
        assert connector.calls[0][0].endswith(str(agent_id))
        assert connector.calls[0][1] == {"Authorization": "Bearer agent-secret"}

        outbound = socket.sent
        assert outbound[0]["message_type"] == MessageType.AGENT_HELLO.value
        assert outbound[0]["sequence_number"] == 1
        reports = [
            value
            for value in outbound
            if value["message_type"] == MessageType.RECONCILIATION_REPORT.value
        ]
        receipts = [
            value for value in outbound if value["message_type"] == MessageType.AGENT_STATUS.value
        ]
        assert receipts[0]["sequence_number"] == 2
        assert receipts[0]["correlation_id"] == str(activation_message_id)
        assert receipts[0]["payload"]["lease_application"] == {
            "reservation_id": str(lease.reservation_id),
            "agent_id": str(agent_id),
            "bench_id": lease.bench_id,
            "lease_version": lease.lease_version,
            "confirmed_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        assert reports[0]["sequence_number"] == 3
        assert reports[0]["correlation_id"] == str(UUID(int=900))

    asyncio.run(scenario())


def test_failed_event_batch_is_retained_and_replayed_after_reconnect() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        first = FakeSocket(on_hello=on_hello, fail_first_event_batch=True)
        second = FakeSocket(on_hello=on_hello)
        connector = FakeConnector((first, second))
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        event = await events.append("HARDWARE_ERROR", {"code": "usb-disconnected"})
        assert event is not None
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=connector,
        )

        with pytest.raises(ConnectionError, match="injected event send failure"):
            await manager.run_once()
        assert (await events.stats()).buffered_events == 1

        run = asyncio.create_task(manager.run_once())
        await second.wait_for_message(MessageType.EVENT_BATCH)
        while (await events.stats()).buffered_events:
            await asyncio.sleep(0)
        await manager.stop()
        assert await run

        batch = next(
            value for value in second.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        )
        assert batch["sequence_number"] == 2
        assert [item["id"] for item in batch["payload"]["events"]] == [str(event.id)]
        assert first.attempted[-1]["sequence_number"] == 2
        assert first.attempted[-1]["message_id"] == batch["message_id"]

    asyncio.run(scenario())


def test_sent_event_batch_without_ack_is_retained_and_replayed_with_the_same_id() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        first = FakeSocket(
            on_hello=on_hello,
            acknowledge_event_batches=False,
        )
        second = FakeSocket(on_hello=on_hello)
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        event = await events.append("HARDWARE_ERROR", {"code": "ack-lost"})
        assert event is not None
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((first, second)),
        )

        first_run = asyncio.create_task(manager.run_once())
        await first.wait_for_message(MessageType.EVENT_BATCH)
        assert (await events.stats()).buffered_events == 1
        await first.close()
        with pytest.raises(SocketClosed, match="socket closed"):
            await first_run
        assert (await events.stats()).buffered_events == 1

        second_run = asyncio.create_task(manager.run_once())
        await second.wait_for_message(MessageType.EVENT_BATCH)
        while (await events.stats()).buffered_events:
            await asyncio.sleep(0)
        await manager.stop()
        assert await second_run

        first_batch = next(
            value for value in first.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        )
        second_batch = next(
            value for value in second.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        )
        assert second_batch["message_id"] == first_batch["message_id"]
        assert second_batch["payload"] == first_batch["payload"]
        assert second_batch["sequence_number"] == 2

    asyncio.run(scenario())


def test_event_ack_must_match_the_in_flight_batch_and_watermark() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        socket = FakeSocket(
            on_hello=on_hello,
            acknowledge_event_batches=False,
        )
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        event = await events.append("HARDWARE_ERROR", {"code": "bad-ack"})
        assert event is not None
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
        )

        run = asyncio.create_task(manager.run_once())
        await socket.wait_for_message(MessageType.EVENT_BATCH)
        batch = next(
            value for value in socket.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        )
        batch_message_id = UUID(cast(str, batch["message_id"]))
        socket.queue_incoming(
            _control_message(
                agent_id,
                MessageType.EVENT_ACK,
                EventAckPayload(
                    batch_message_id=batch_message_id,
                    acknowledged_event_sequence=event.sequence_number + 1,
                ),
                sequence_number=2,
                correlation_id=batch_message_id,
            )
        )

        with pytest.raises(AgentHandshakeError, match="watermark"):
            await run
        assert (await events.stats()).buffered_events == 1

    asyncio.run(scenario())


def test_missing_event_ack_times_out_without_deleting_durable_events() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        socket = FakeSocket(
            on_hello=on_hello,
            acknowledge_event_batches=False,
        )
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        assert await events.append("HARDWARE_ERROR", {"code": "ack-timeout"})
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
            event_poll_interval_seconds=0.005,
            event_ack_timeout_seconds=0.02,
            monotonic=time.monotonic,
        )

        with pytest.raises(AgentConnectionError, match="acknowledge"):
            await asyncio.wait_for(manager.run_once(), timeout=1)
        assert (await events.stats()).buffered_events == 1
        assert any(value["message_type"] == MessageType.EVENT_BATCH.value for value in socket.sent)

    asyncio.run(scenario())


def test_heartbeats_continue_while_an_event_batch_waits_for_ack() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        async def tick_immediately(_delay: float) -> None:
            await asyncio.sleep(0)

        socket = FakeSocket(
            on_hello=on_hello,
            acknowledge_event_batches=False,
        )
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        assert await events.append("HARDWARE_ERROR", {"code": "awaiting-ack"})
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
            sleep=tick_immediately,
            heartbeat=FakeHeartbeat(),
        )

        run = asyncio.create_task(manager.run_once())
        await socket.wait_for_message(MessageType.AGENT_HEARTBEAT)
        await manager.stop()
        assert await run
        assert (await events.stats()).buffered_events == 1
        event_batch = next(
            value for value in socket.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        )
        heartbeat = next(
            value
            for value in socket.sent
            if value["message_type"] == MessageType.AGENT_HEARTBEAT.value
        )
        assert event_batch["sequence_number"] == 2
        assert heartbeat["sequence_number"] > event_batch["sequence_number"]

    asyncio.run(scenario())


def test_reader_does_not_acknowledge_or_cancel_before_durable_command_dispatch() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()
        command_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (
                _welcome(agent_id, hello),
                _control_message(
                    agent_id,
                    MessageType.COMMAND_REQUEST,
                    CommandRequestPayload(command=_command(agent_id, command_id)),
                    sequence_number=2,
                ),
                _control_message(
                    agent_id,
                    MessageType.COMMAND_CANCEL,
                    CommandCancelPayload(command_id=command_id, reason="cancel immediately"),
                    sequence_number=3,
                ),
            )

        commands = GatedCommands()
        socket = FakeSocket(on_hello=on_hello)
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
            commands=commands,
        )

        run = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(commands.dispatch_started.wait(), timeout=1)
        assert manager.last_control_plane_sequence == 1
        assert commands.cancelled == []

        commands.allow_durable_acceptance.set()
        while manager.last_control_plane_sequence < 3:
            await asyncio.sleep(0)
        await manager.stop()
        assert await run

        assert [request.command.id for request in commands.handled] == [command_id]
        assert [request.command_id for request in commands.cancelled] == [command_id]
        assert commands.cancel_observed_durable == [True]

    asyncio.run(scenario())


def test_heartbeats_share_the_single_ordered_bounded_writer() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        async def tick_immediately(_delay: float) -> None:
            await asyncio.sleep(0)

        socket = FakeSocket(on_hello=on_hello)
        connector = FakeConnector((socket,))
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=connector,
            sleep=tick_immediately,
            heartbeat=FakeHeartbeat(),
        )

        run = asyncio.create_task(manager.run_once())
        await socket.wait_for_message(MessageType.AGENT_HEARTBEAT)
        await manager.stop()
        assert await run

        heartbeat = next(
            value
            for value in socket.sent
            if value["message_type"] == MessageType.AGENT_HEARTBEAT.value
        )
        assert heartbeat["sequence_number"] == 2
        assert heartbeat["payload"] == {
            "agent_id": str(agent_id),
            "boot_id": str(boot_id),
            "uptime_seconds": 0,
            "active_operations": 2,
            "connected_benches": 5,
            "degraded_benches": 1,
            "event_buffer_size": 0,
            "timestamp": NOW.isoformat().replace("+00:00", "Z"),
        }

    asyncio.run(scenario())


def test_event_replay_splits_batches_to_the_outbound_byte_limit() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello),)

        socket = FakeSocket(on_hello=on_hello)
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        first = await events.append("HARDWARE_ERROR", {"detail": "x" * 800})
        second = await events.append("HARDWARE_ERROR", {"detail": "y" * 800})
        assert first is not None and second is not None
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
            maximum_message_size_bytes=1_600,
        )

        async def wait_for_two_batches() -> None:
            while (
                sum(value["message_type"] == MessageType.EVENT_BATCH.value for value in socket.sent)
                < 2
            ):
                await asyncio.sleep(0)

        run = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(wait_for_two_batches(), timeout=1)
        await manager.stop()
        assert await run

        batches = [
            value for value in socket.sent if value["message_type"] == MessageType.EVENT_BATCH.value
        ]
        assert [batch["sequence_number"] for batch in batches] == [2, 3]
        assert [len(batch["payload"]["events"]) for batch in batches] == [1, 1]
        assert [batch["payload"]["events"][0]["id"] for batch in batches] == [
            str(first.id),
            str(second.id),
        ]
        assert all(len(json.dumps(batch).encode("utf-8")) <= 1_600 for batch in batches)
        assert (await events.stats()).buffered_events == 0

    asyncio.run(scenario())


def test_stale_reconciliation_request_for_another_boot_is_not_acknowledged() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (
                _welcome(agent_id, hello),
                _control_message(
                    agent_id,
                    MessageType.RECONCILIATION_REQUEST,
                    ReconciliationRequestPayload(
                        request_id=uuid4(),
                        expected_boot_id=uuid4(),
                        last_control_plane_sequence=1,
                    ),
                    sequence_number=2,
                ),
            )

        socket = FakeSocket(on_hello=on_hello)
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=FakeConnector((socket,)),
        )

        with pytest.raises(AgentHandshakeError, match="different Agent boot"):
            await manager.run_once()

        assert manager.last_control_plane_sequence == 1
        assert all(
            value["message_type"] != MessageType.RECONCILIATION_REPORT.value
            for value in socket.sent
        )

    asyncio.run(scenario())


def test_transport_policy_queue_bound_secret_masking_and_backoff() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        connector = AlwaysFailConnector()

        with pytest.raises(AgentTransportSecurityError, match="loopback"):
            _manager(
                agent_id=agent_id,
                boot_id=boot_id,
                events=events,
                leases=leases,
                connector=connector,
                gateway_url="ws://lab.example/api/v1/agent-gateway",
            )

        delays: list[float] = []
        holder: dict[str, AgentConnectionManager] = {}

        async def fake_sleep(delay: float) -> None:
            if delay == 10.0:
                await asyncio.Event().wait()
            delays.append(delay)
            if len(delays) == 3:
                await holder["manager"].stop()

        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=connector,
            gateway_url="wss://lab.example/api/v1/agent-gateway",
            credential=SecretStr("top-secret-token"),
            outgoing_queue_size=1,
            sleep=fake_sleep,
        )
        holder["manager"] = manager
        assert "top-secret-token" not in repr(manager)
        assert "**********" in repr(manager)

        status = AgentStatusPayload(
            agent_id=agent_id,
            boot_id=boot_id,
            status=AgentStatus.ONLINE,
            changed_at=NOW,
        )
        await manager.send(MessageType.AGENT_STATUS, status)
        with pytest.raises(AgentOutgoingQueueFullError, match="queue is full"):
            await manager.send(MessageType.AGENT_STATUS, status)

        await manager.run()
        assert connector.calls == 3
        assert delays == [1.0, 2.0, 4.0]

        local_events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        local_leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        local = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=local_events,
            leases=local_leases,
            connector=AlwaysFailConnector(),
            gateway_url="ws://127.0.0.1:8080/api/v1/agent-gateway",
            allow_insecure_loopback=True,
        )
        assert local.endpoint.startswith("ws://127.0.0.1:8080/")

    asyncio.run(scenario())


def test_reconnect_backoff_keeps_growing_when_peer_flaps_after_welcome() -> None:
    async def scenario() -> None:
        agent_id = uuid4()
        boot_id = uuid4()

        def on_hello(hello: dict[str, Any]) -> Sequence[str]:
            return (_welcome(agent_id, hello), "not valid JSON")

        connector = FakeConnector(tuple(FakeSocket(on_hello=on_hello) for _index in range(3)))
        events = InMemoryAgentEventBuffer(agent_id, clock=lambda: NOW)
        leases = InMemoryReservationLeaseStore(agent_id, clock=lambda: NOW)
        delays: list[float] = []
        holder: dict[str, AgentConnectionManager] = {}

        async def fake_sleep(delay: float) -> None:
            if delay == 10.0:
                await asyncio.Event().wait()
            delays.append(delay)
            if len(delays) == 3:
                await holder["manager"].stop()

        manager = _manager(
            agent_id=agent_id,
            boot_id=boot_id,
            events=events,
            leases=leases,
            connector=connector,
            sleep=fake_sleep,
        )
        holder["manager"] = manager

        await manager.run()

        assert len(connector.calls) == 3
        assert delays == [1.0, 2.0, 4.0]

    asyncio.run(scenario())
