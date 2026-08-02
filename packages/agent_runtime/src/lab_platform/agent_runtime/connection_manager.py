from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeAlias, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID, uuid4

from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    AgentHeartbeatPayload,
    AgentHelloPayload,
    AgentStatus,
    AgentStatusPayload,
    ArtifactUploadRequestEnvelope,
    CommandCancelEnvelope,
    CommandCancelPayload,
    CommandRequestEnvelope,
    CommandRequestPayload,
    ConfigRefreshRequestEnvelope,
    DrainAgentEnvelope,
    DrainAgentPayload,
    EnvelopeBase,
    EventAckEnvelope,
    EventBatchPayload,
    IncomingSequenceTracker,
    InventoryRefreshRequestEnvelope,
    InventoryRefreshRequestPayload,
    MessageType,
    ReconciliationReportPayload,
    ReconciliationRequestEnvelope,
    ReservationActivatedEnvelope,
    ReservationLeaseAppliedPayload,
    ReservationReleasedEnvelope,
    SequenceDisposition,
    SupportedEnvelope,
    WelcomeEnvelope,
    parse_agent_message,
    parse_control_plane_message,
)
from lab_platform.agent_runtime.command_handler import (
    CommandDispatchResult,
    CommandHandlingResult,
)
from lab_platform.agent_runtime.event_buffer import AgentEventBufferRepository
from lab_platform.agent_runtime.leases import ReservationLeaseStore
from lab_platform.models import BufferedAgentEvent, ReconciliationReport
from pydantic import SecretStr

Clock: TypeAlias = Callable[[], datetime]
MonotonicClock: TypeAlias = Callable[[], float]
Sleep: TypeAlias = Callable[[float], Awaitable[None]]
RandomSource: TypeAlias = Callable[[], float]
MessageIdFactory: TypeAlias = Callable[[], UUID]


class AgentConnectionError(RuntimeError):
    """Base error for the Agent's persistent control-plane connection."""


class AgentTransportSecurityError(AgentConnectionError):
    """The configured gateway URL violates the Agent transport policy."""


class AgentHandshakeError(AgentConnectionError):
    """The peer did not complete the Phase 5 handshake correctly."""


class AgentMessageTooLargeError(AgentConnectionError):
    """A WebSocket message exceeded the configured bounded size."""


class AgentOutgoingQueueFullError(AgentConnectionError):
    """The bounded in-memory transport queue cannot accept more work."""


class AgentWebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class WebSocketConnector(Protocol):
    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket: ...


class _WebsocketsConnect(Protocol):
    def __call__(
        self,
        url: str,
        *,
        additional_headers: Mapping[str, str],
        max_size: int,
        ping_interval: None,
        compression: None,
    ) -> Awaitable[AgentWebSocket]: ...


class WebsocketsConnector:
    """Default adapter for the optional-at-import-time ``websockets`` dependency."""

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket:
        try:
            module = importlib.import_module("websockets.asyncio.client")
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency is installed in release
            raise AgentConnectionError("The websockets dependency is not installed") from exc
        connect = cast(_WebsocketsConnect, module.connect)
        return await connect(
            url,
            additional_headers=headers,
            max_size=maximum_message_size_bytes,
            ping_interval=None,
            compression=None,
        )


class AgentCommandPort(Protocol):
    async def dispatch(self, request: CommandRequestPayload) -> CommandDispatchResult: ...

    async def cancel(self, payload: CommandCancelPayload) -> object: ...


class AgentDrainPort(Protocol):
    async def apply_drain(self, payload: DrainAgentPayload) -> None: ...


class AgentInventoryRefreshPort(Protocol):
    async def refresh_inventory(self, payload: InventoryRefreshRequestPayload) -> None: ...


class AgentAuxiliaryMessagePort(Protocol):
    async def handle_auxiliary_message(self, envelope: SupportedEnvelope) -> None: ...


class AgentReconciliationSource(Protocol):
    @property
    def agent_id(self) -> UUID: ...

    @property
    def boot_id(self) -> UUID: ...

    async def build(self, *, generated_at: datetime | None = None) -> ReconciliationReport: ...


@dataclass(frozen=True, slots=True)
class AgentHeartbeatState:
    active_operations: int = 0
    connected_benches: int = 0
    degraded_benches: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("active_operations", self.active_operations),
            ("connected_benches", self.connected_benches),
            ("degraded_benches", self.degraded_benches),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.degraded_benches > self.connected_benches:
            raise ValueError("degraded_benches cannot exceed connected_benches")


class AgentHeartbeatSource(Protocol):
    async def heartbeat_state(self) -> AgentHeartbeatState: ...


class EmptyHeartbeatSource:
    async def heartbeat_state(self) -> AgentHeartbeatState:
        return AgentHeartbeatState()


@dataclass(frozen=True, slots=True)
class _OutgoingItem:
    message_id: UUID
    message_type: MessageType
    payload: object
    correlation_id: UUID | None
    persistent: bool


@dataclass(frozen=True, slots=True)
class _InFlightEventBatch:
    message_id: UUID
    events: tuple[BufferedAgentEvent, ...]

    @property
    def acknowledged_event_sequence(self) -> int:
        return self.events[-1].sequence_number


class AgentConnectionManager:
    """Own one authenticated WebSocket and safely reconnect the Agent forever.

    Transport sequence numbers are assigned only by the single writer and restart at one for
    every connection. One durable event batch remains in ``events`` until the control plane
    acknowledges that exact batch after routing it. A disconnect or missing acknowledgement
    therefore replays the same batch and event IDs after the next handshake.
    """

    def __init__(
        self,
        *,
        agent_id: UUID,
        boot_id: UUID,
        agent_name: str,
        agent_version: str,
        gateway_url: str,
        credential: str | SecretStr,
        events: AgentEventBufferRepository,
        leases: ReservationLeaseStore,
        commands: AgentCommandPort,
        drain: AgentDrainPort,
        reconciliation: AgentReconciliationSource,
        connector: WebSocketConnector | None = None,
        heartbeat: AgentHeartbeatSource | None = None,
        inventory_refresh: AgentInventoryRefreshPort | None = None,
        auxiliary_messages: AgentAuxiliaryMessagePort | None = None,
        capabilities: frozenset[str] = frozenset(),
        last_acknowledged_command_sequence: int = 0,
        maximum_clock_skew_seconds: int = 0,
        outgoing_queue_size: int = 256,
        event_batch_size: int = 500,
        event_poll_interval_seconds: float = 0.25,
        event_ack_timeout_seconds: float = 30.0,
        maximum_message_size_bytes: int = 2 * 1024 * 1024,
        handshake_timeout_seconds: float = 10.0,
        reconnect_initial_delay_seconds: float = 1.0,
        reconnect_maximum_delay_seconds: float = 60.0,
        reconnect_jitter_ratio: float = 0.2,
        reconnect_stability_seconds: float = 30.0,
        allow_insecure_loopback: bool = False,
        clock: Clock | None = None,
        monotonic: MonotonicClock | None = None,
        sleep: Sleep | None = None,
        random_source: RandomSource | None = None,
        message_id_factory: MessageIdFactory | None = None,
    ) -> None:
        endpoint = _agent_endpoint(
            gateway_url,
            agent_id,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        raw_credential = (
            credential.get_secret_value() if isinstance(credential, SecretStr) else credential
        )
        if not isinstance(raw_credential, str) or not raw_credential.strip():
            raise ValueError("Agent credential must be a non-empty secret")
        if leases.agent_id != agent_id:
            raise ValueError("Reservation lease store belongs to a different Agent")
        if reconciliation.agent_id != agent_id or reconciliation.boot_id != boot_id:
            raise ValueError("Reconciliation source belongs to a different Agent process")
        _positive_integer(outgoing_queue_size, field="outgoing_queue_size")
        _bounded_integer(event_batch_size, field="event_batch_size", maximum=10_000)
        _positive_integer(
            maximum_message_size_bytes,
            field="maximum_message_size_bytes",
        )
        _positive_number(
            event_poll_interval_seconds,
            field="event_poll_interval_seconds",
        )
        _positive_number(event_ack_timeout_seconds, field="event_ack_timeout_seconds")
        _positive_number(handshake_timeout_seconds, field="handshake_timeout_seconds")
        _positive_number(
            reconnect_initial_delay_seconds,
            field="reconnect_initial_delay_seconds",
        )
        _positive_number(
            reconnect_maximum_delay_seconds,
            field="reconnect_maximum_delay_seconds",
        )
        if reconnect_maximum_delay_seconds < reconnect_initial_delay_seconds:
            raise ValueError(
                "reconnect_maximum_delay_seconds cannot be less than the initial delay"
            )
        if not 0 <= reconnect_jitter_ratio <= 1:
            raise ValueError("reconnect_jitter_ratio must be between zero and one")
        _positive_number(
            reconnect_stability_seconds,
            field="reconnect_stability_seconds",
        )
        if (
            isinstance(last_acknowledged_command_sequence, bool)
            or last_acknowledged_command_sequence < 0
        ):
            raise ValueError("last_acknowledged_command_sequence must be non-negative")
        if isinstance(maximum_clock_skew_seconds, bool) or maximum_clock_skew_seconds < 0:
            raise ValueError("maximum_clock_skew_seconds must be non-negative")

        self._agent_id = agent_id
        self._boot_id = boot_id
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._endpoint = endpoint
        self._credential = SecretStr(raw_credential.strip())
        self._events = events
        self._leases = leases
        self._commands = commands
        self._drain = drain
        self._reconciliation = reconciliation
        self._connector = connector or WebsocketsConnector()
        self._heartbeat = heartbeat or EmptyHeartbeatSource()
        self._inventory_refresh = inventory_refresh
        self._auxiliary_messages = auxiliary_messages
        self._capabilities = capabilities
        self._last_control_plane_sequence = last_acknowledged_command_sequence
        self._connection_claimed_sequence = last_acknowledged_command_sequence
        self._maximum_clock_skew_seconds = maximum_clock_skew_seconds
        self._queue_size = outgoing_queue_size
        self._event_batch_size = event_batch_size
        self._event_poll_interval = event_poll_interval_seconds
        self._event_ack_timeout = event_ack_timeout_seconds
        self._maximum_message_size = maximum_message_size_bytes
        self._handshake_timeout = handshake_timeout_seconds
        self._reconnect_initial = reconnect_initial_delay_seconds
        self._reconnect_maximum = reconnect_maximum_delay_seconds
        self._reconnect_jitter = reconnect_jitter_ratio
        self._reconnect_stability = reconnect_stability_seconds
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._random = random_source or random.random
        self._message_id_factory = message_id_factory or uuid4
        self._process_started_at = self._monotonic()

        self._outgoing: deque[_OutgoingItem] = deque()
        self._outgoing_changed = asyncio.Condition()
        self._stop_requested = asyncio.Event()
        self._active_socket: AgentWebSocket | None = None
        self._connected = False
        self._connection_attempts = 0
        self._reconnect_attempts = 0
        self._last_session_duration = 0.0
        self._command_tasks: set[asyncio.Task[CommandHandlingResult]] = set()
        self._in_flight_event_batch: _InFlightEventBatch | None = None
        self._last_event_ack: tuple[UUID, int] | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(agent_id={self._agent_id!r}, "
            f"boot_id={self._boot_id!r}, endpoint={self._endpoint!r}, credential=**********)"
        )

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_control_plane_sequence(self) -> int:
        return self._last_control_plane_sequence

    @property
    def queued_messages(self) -> int:
        return len(self._outgoing)

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    def reconnect_delay(self, attempt: int) -> float:
        if isinstance(attempt, bool) or attempt < 0:
            raise ValueError("Reconnect attempt must be a non-negative integer")
        maximum_exponent = max(
            0,
            math.ceil(math.log2(self._reconnect_maximum / self._reconnect_initial)),
        )
        base = (
            self._reconnect_maximum
            if attempt >= maximum_exponent
            else self._reconnect_initial * (2**attempt)
        )
        if self._reconnect_jitter == 0:
            return base
        sample = min(1.0, max(0.0, self._random()))
        factor = (1 - self._reconnect_jitter) + (2 * self._reconnect_jitter * sample)
        return min(self._reconnect_maximum, float(base * factor))

    async def send(
        self,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Queue one typed Agent-to-control-plane message without unbounded waiting."""

        message_id = self._message_id_factory()
        item = _OutgoingItem(
            message_id=message_id,
            message_type=message_type,
            payload=payload,
            correlation_id=correlation_id,
            persistent=True,
        )
        self._validate_outgoing(item)
        await self._enqueue(item, raise_when_full=True)
        return message_id

    async def stop(self) -> None:
        self._stop_requested.set()
        async with self._outgoing_changed:
            self._outgoing_changed.notify_all()
        socket = self._active_socket
        if socket is not None:
            with suppress(Exception):
                await socket.close()

    async def request_reconnect(self) -> bool:
        """Close the current socket so the run loop establishes a fresh session."""

        socket = self._active_socket
        if socket is None:
            return False
        await socket.close()
        return True

    async def wait_for_command_tasks(self) -> None:
        """Wait for the command handlers that are active at the time of this call."""

        tasks = tuple(self._command_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        attempt = 0
        while not self._stop_requested.is_set():
            if self._connection_attempts > 0:
                self._reconnect_attempts += 1
            self._connection_attempts += 1
            try:
                handshake_completed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stop_requested.is_set():
                    break
                if self._last_session_duration >= self._reconnect_stability:
                    attempt = 0
                delay = self.reconnect_delay(attempt)
                attempt += 1
                await self._sleep_or_stop(delay)
            else:
                if self._stop_requested.is_set():
                    break
                attempt = 0 if handshake_completed else attempt + 1
                await self._sleep_or_stop(self.reconnect_delay(attempt))

    async def run_once(self) -> bool:
        """Connect and serve one session; return whether its WELCOME was accepted."""

        if self._stop_requested.is_set():
            return False
        self._last_session_duration = 0.0
        self._connection_claimed_sequence = self._last_control_plane_sequence
        socket = await self._connector.connect(
            self._endpoint,
            headers={"Authorization": f"Bearer {self._credential.get_secret_value()}"},
            maximum_message_size_bytes=self._maximum_message_size,
        )
        self._active_socket = socket
        handshake_completed = False
        session_started_at: float | None = None
        try:
            hello = self._make_hello()
            await socket.send(self._serialize_envelope(hello))
            tracker = IncomingSequenceTracker()
            welcome = await self._receive_welcome(socket, tracker, hello.message_id)
            self._last_control_plane_sequence = welcome.sequence_number
            handshake_completed = True
            self._connected = True
            session_started_at = self._monotonic()
            await self._serve_connection(
                socket,
                tracker,
                welcome.payload.heartbeat_interval_seconds,
            )
            return handshake_completed
        finally:
            if session_started_at is not None:
                self._last_session_duration = max(
                    0.0,
                    self._monotonic() - session_started_at,
                )
            self._connected = False
            self._active_socket = None
            await self._drop_ephemeral_messages()
            with suppress(Exception):
                await socket.close()

    def _make_hello(self) -> EnvelopeBase:
        payload = AgentHelloPayload(
            agent_version=self._agent_version,
            protocol_version=PROTOCOL_VERSION,
            agent_name=self._agent_name,
            boot_id=self._boot_id,
            capabilities=self._capabilities,
            last_acknowledged_command_sequence=self._connection_claimed_sequence,
        )
        return self._make_envelope(
            _OutgoingItem(
                message_id=self._message_id_factory(),
                message_type=MessageType.AGENT_HELLO,
                payload=payload,
                correlation_id=None,
                persistent=False,
            ),
            sequence_number=1,
        )

    async def _receive_welcome(
        self,
        socket: AgentWebSocket,
        tracker: IncomingSequenceTracker,
        hello_message_id: UUID,
    ) -> WelcomeEnvelope:
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=self._handshake_timeout)
        except TimeoutError as exc:
            raise AgentHandshakeError("Timed out waiting for WELCOME") from exc
        envelope = self._decode_control_plane_message(raw)
        if not isinstance(envelope, WelcomeEnvelope):
            raise AgentHandshakeError("First control-plane message must be WELCOME")
        if envelope.agent_id != self._agent_id or envelope.sequence_number != 1:
            raise AgentHandshakeError("WELCOME identity or initial sequence is invalid")
        if envelope.correlation_id != hello_message_id:
            raise AgentHandshakeError("WELCOME does not correlate to AGENT_HELLO")
        tracker.observe(envelope)
        return envelope

    async def _serve_connection(
        self,
        socket: AgentWebSocket,
        tracker: IncomingSequenceTracker,
        heartbeat_interval_seconds: int,
    ) -> None:
        reader = asyncio.create_task(
            self._reader(socket, tracker),
            name=f"agent-cp-reader-{self._agent_id}",
        )
        writer = asyncio.create_task(
            self._writer(socket),
            name=f"agent-cp-writer-{self._agent_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(heartbeat_interval_seconds),
            name=f"agent-heartbeat-{self._agent_id}",
        )
        tasks = {reader, writer, heartbeat}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._stop_requested.is_set():
                for task in done:
                    if not task.cancelled():
                        task.exception()
                return
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    raise error
            raise AgentConnectionError("Agent WebSocket task ended unexpectedly")
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _reader(
        self,
        socket: AgentWebSocket,
        tracker: IncomingSequenceTracker,
    ) -> None:
        while not self._stop_requested.is_set():
            envelope = self._decode_control_plane_message(await socket.recv())
            if envelope.agent_id != self._agent_id or isinstance(envelope, WelcomeEnvelope):
                raise AgentHandshakeError("Control-plane stream identity or handshake is invalid")
            disposition = tracker.observe(envelope)
            if disposition is SequenceDisposition.DUPLICATE:
                continue
            await self._dispatch(envelope)
            self._last_control_plane_sequence = envelope.sequence_number

    async def _writer(self, socket: AgentWebSocket) -> None:
        next_sequence = 2
        sent_batch_id: UUID | None = None
        sent_batch_at: float | None = None
        while not self._stop_requested.is_set():
            batch = self._in_flight_event_batch
            if batch is None:
                sent_batch_id = None
                sent_batch_at = None
                events = await self._events.peek(limit=self._event_batch_size)
                if events:
                    message_id = self._message_id_factory()
                    replayed_events, encoded = self._event_batch_message(
                        events,
                        message_id=message_id,
                        sequence_number=next_sequence,
                    )
                    batch = _InFlightEventBatch(
                        message_id=message_id,
                        events=replayed_events,
                    )
                    self._in_flight_event_batch = batch
                else:
                    encoded = None
            else:
                encoded = None

            if batch is not None and sent_batch_id != batch.message_id:
                if encoded is None:
                    replayed_events, encoded = self._event_batch_message(
                        batch.events,
                        message_id=batch.message_id,
                        sequence_number=next_sequence,
                    )
                    if replayed_events != batch.events:  # pragma: no cover - local invariant
                        raise RuntimeError("In-flight event batch no longer fits the transport")
                await socket.send(encoded)
                sent_batch_id = batch.message_id
                sent_batch_at = self._monotonic()
                next_sequence += 1
            elif batch is not None and sent_batch_at is not None:
                if self._monotonic() - sent_batch_at >= self._event_ack_timeout:
                    raise AgentConnectionError(
                        "Timed out waiting for the control plane to acknowledge an event batch"
                    )

            wait_timeout = self._event_poll_interval
            if batch is not None and sent_batch_at is not None:
                remaining = self._event_ack_timeout - (self._monotonic() - sent_batch_at)
                wait_timeout = min(wait_timeout, max(0.0, remaining))
            item = await self._queue_head(timeout=wait_timeout)
            if item is None:
                continue
            envelope = self._make_envelope(item, sequence_number=next_sequence)
            await socket.send(self._serialize_envelope(envelope))
            await self._remove_queue_head(item)
            next_sequence += 1

    async def _heartbeat_loop(self, interval_seconds: int) -> None:
        while not self._stop_requested.is_set():
            await self._sleep(float(interval_seconds))
            if self._stop_requested.is_set() or not self._connected:
                return
            state = await self._heartbeat.heartbeat_state()
            event_stats = await self._events.stats()
            observed_at = _as_utc(self._clock(), field="Agent heartbeat clock")
            item = _OutgoingItem(
                message_id=self._message_id_factory(),
                message_type=MessageType.AGENT_HEARTBEAT,
                payload=AgentHeartbeatPayload(
                    agent_id=self._agent_id,
                    boot_id=self._boot_id,
                    uptime_seconds=max(0, int(self._monotonic() - self._process_started_at)),
                    active_operations=state.active_operations,
                    connected_benches=state.connected_benches,
                    degraded_benches=state.degraded_benches,
                    event_buffer_size=event_stats.buffered_events,
                    timestamp=observed_at,
                ),
                correlation_id=None,
                persistent=False,
            )
            await self._enqueue(item, raise_when_full=False)

    async def _dispatch(self, envelope: SupportedEnvelope) -> None:
        if isinstance(envelope, EventAckEnvelope):
            await self._acknowledge_event_batch(envelope)
        elif isinstance(envelope, CommandRequestEnvelope):
            dispatch = await self._commands.dispatch(envelope.payload)
            if dispatch.execution is not None:
                self._command_tasks.add(dispatch.execution)
                dispatch.execution.add_done_callback(self._command_task_finished)
        elif isinstance(envelope, CommandCancelEnvelope):
            await self._commands.cancel(envelope.payload)
        elif isinstance(envelope, ReservationActivatedEnvelope):
            confirmed_at = _as_utc(self._clock(), field="Agent lease clock")
            await self._leases.apply(
                envelope.payload.lease,
                observed_at=confirmed_at,
                maximum_clock_skew_seconds=self._maximum_clock_skew_seconds,
            )
            lease = envelope.payload.lease
            await self.send(
                MessageType.AGENT_STATUS,
                AgentStatusPayload(
                    agent_id=self._agent_id,
                    boot_id=self._boot_id,
                    status=AgentStatus.ONLINE,
                    changed_at=confirmed_at,
                    lease_application=ReservationLeaseAppliedPayload(
                        reservation_id=lease.reservation_id,
                        agent_id=lease.agent_id,
                        bench_id=lease.bench_id,
                        lease_version=lease.lease_version,
                        confirmed_at=confirmed_at,
                    ),
                ),
                correlation_id=envelope.message_id,
            )
        elif isinstance(envelope, ReservationReleasedEnvelope):
            payload = envelope.payload
            await self._leases.release(
                agent_id=self._agent_id,
                reservation_id=payload.reservation_id,
                bench_id=payload.bench_id,
                lease_version=payload.lease_version,
                released_at=payload.released_at,
            )
        elif isinstance(envelope, DrainAgentEnvelope):
            await self._drain.apply_drain(envelope.payload)
        elif isinstance(envelope, ReconciliationRequestEnvelope):
            expected_boot_id = envelope.payload.expected_boot_id
            if expected_boot_id is not None and expected_boot_id != self._boot_id:
                raise AgentHandshakeError("Reconciliation request targets a different Agent boot")
            if envelope.payload.last_control_plane_sequence != self._connection_claimed_sequence:
                raise AgentHandshakeError(
                    "Reconciliation request carries a stale control-plane sequence"
                )
            report = await self._reconciliation.build()
            await self.send(
                MessageType.RECONCILIATION_REPORT,
                ReconciliationReportPayload(report=report),
                correlation_id=envelope.payload.request_id,
            )
        elif isinstance(envelope, InventoryRefreshRequestEnvelope):
            if self._inventory_refresh is None:
                raise AgentConnectionError("No inventory refresh port is configured")
            await self._inventory_refresh.refresh_inventory(envelope.payload)
        elif isinstance(envelope, (ArtifactUploadRequestEnvelope, ConfigRefreshRequestEnvelope)):
            if self._auxiliary_messages is None:
                raise AgentConnectionError("No auxiliary control-message port is configured")
            await self._auxiliary_messages.handle_auxiliary_message(envelope)
        else:  # pragma: no cover - the protocol union is exhaustively handled above
            raise AgentConnectionError(
                f"Unsupported control-plane message type: {envelope.message_type.value}"
            )

    async def _acknowledge_event_batch(self, envelope: EventAckEnvelope) -> None:
        payload = envelope.payload
        acknowledgement = (
            payload.batch_message_id,
            payload.acknowledged_event_sequence,
        )
        if envelope.correlation_id != payload.batch_message_id:
            raise AgentHandshakeError("EVENT_ACK does not correlate to its event batch")

        batch = self._in_flight_event_batch
        if batch is None:
            if acknowledgement == self._last_event_ack:
                return
            raise AgentHandshakeError("EVENT_ACK does not match an in-flight event batch")
        if (
            payload.batch_message_id != batch.message_id
            or payload.acknowledged_event_sequence != batch.acknowledged_event_sequence
        ):
            raise AgentHandshakeError("EVENT_ACK batch identity or watermark is invalid")

        await self._events.acknowledge_through(payload.acknowledged_event_sequence)
        self._last_event_ack = acknowledgement
        self._in_flight_event_batch = None

    def _command_task_finished(self, task: asyncio.Task[CommandHandlingResult]) -> None:
        self._command_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _validate_outgoing(self, item: _OutgoingItem) -> None:
        if item.message_type is MessageType.AGENT_HELLO:
            raise ValueError("AGENT_HELLO is reserved for the connection handshake")
        self._serialize_envelope(self._make_envelope(item, sequence_number=1))

    def _event_batch_message(
        self,
        events: tuple[BufferedAgentEvent, ...],
        *,
        message_id: UUID,
        sequence_number: int,
    ) -> tuple[tuple[BufferedAgentEvent, ...], str]:
        if not events:  # pragma: no cover - callers only invoke this for a non-empty peek
            raise RuntimeError("Agent event replay produced an empty batch")

        def encode(count: int) -> tuple[tuple[BufferedAgentEvent, ...], str]:
            selected = events[:count]
            envelope = self._make_envelope(
                _OutgoingItem(
                    message_id=message_id,
                    message_type=MessageType.EVENT_BATCH,
                    payload=EventBatchPayload(events=selected),
                    correlation_id=None,
                    persistent=True,
                ),
                sequence_number=sequence_number,
            )
            return selected, self._serialize_envelope(envelope)

        # Probe upward from one event so a configured 2 MiB transport never first serializes a
        # potentially 100+ MiB count-bounded batch. Then binary-search only the small boundary.
        best_events, best_encoded = encode(1)
        fitted = 1
        upper_bound = len(events)
        while fitted < len(events):
            candidate = min(len(events), fitted * 2)
            try:
                candidate_events, candidate_encoded = encode(candidate)
            except AgentMessageTooLargeError:
                upper_bound = candidate - 1
                break
            best_events, best_encoded = candidate_events, candidate_encoded
            fitted = candidate
        if fitted == len(events):
            return best_events, best_encoded

        lower_bound = fitted + 1
        while lower_bound <= upper_bound:
            candidate = (lower_bound + upper_bound) // 2
            try:
                candidate_events, candidate_encoded = encode(candidate)
            except AgentMessageTooLargeError:
                upper_bound = candidate - 1
            else:
                best_events, best_encoded = candidate_events, candidate_encoded
                lower_bound = candidate + 1
        return best_events, best_encoded

    def _make_envelope(
        self,
        item: _OutgoingItem,
        *,
        sequence_number: int,
    ) -> EnvelopeBase:
        envelope = parse_agent_message(
            {
                "protocol_version": PROTOCOL_VERSION,
                "message_id": item.message_id,
                "message_type": item.message_type,
                "agent_id": self._agent_id,
                "sent_at": _as_utc(self._clock(), field="Agent protocol clock"),
                "correlation_id": item.correlation_id,
                "sequence_number": sequence_number,
                "payload": item.payload,
            }
        )
        return envelope

    def _serialize_envelope(self, envelope: EnvelopeBase) -> str:
        encoded = envelope.model_dump_json()
        if len(encoded.encode("utf-8")) > self._maximum_message_size:
            raise AgentMessageTooLargeError("Agent message exceeds the configured transport limit")
        return encoded

    def _decode_control_plane_message(self, raw: str | bytes) -> SupportedEnvelope:
        if isinstance(raw, bytes):
            encoded = raw
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgentHandshakeError("Control-plane message is not UTF-8") from exc
        elif isinstance(raw, str):
            text = raw
            encoded = raw.encode("utf-8")
        else:
            raise AgentHandshakeError("Control-plane WebSocket message must be text or bytes")
        if len(encoded) > self._maximum_message_size:
            raise AgentMessageTooLargeError("Control-plane message exceeds the configured limit")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentHandshakeError("Control-plane message is not valid JSON") from exc
        return parse_control_plane_message(value)

    async def _enqueue(self, item: _OutgoingItem, *, raise_when_full: bool) -> bool:
        async with self._outgoing_changed:
            if len(self._outgoing) >= self._queue_size:
                if raise_when_full:
                    raise AgentOutgoingQueueFullError("Agent outgoing queue is full")
                return False
            self._outgoing.append(item)
            self._outgoing_changed.notify()
            return True

    async def _queue_head(self, *, timeout: float) -> _OutgoingItem | None:
        async with self._outgoing_changed:
            if not self._outgoing and timeout > 0:
                try:
                    await asyncio.wait_for(self._outgoing_changed.wait(), timeout=timeout)
                except TimeoutError:
                    return None
            return self._outgoing[0] if self._outgoing else None

    async def _remove_queue_head(self, expected: _OutgoingItem) -> None:
        async with self._outgoing_changed:
            if not self._outgoing or self._outgoing[0] is not expected:
                raise RuntimeError("Agent outgoing queue ordering was corrupted")
            self._outgoing.popleft()
            self._outgoing_changed.notify_all()

    async def _drop_ephemeral_messages(self) -> None:
        async with self._outgoing_changed:
            retained = (item for item in self._outgoing if item.persistent)
            self._outgoing = deque(retained)
            self._outgoing_changed.notify_all()

    async def _sleep_or_stop(self, delay: float) -> None:
        async def sleep_once() -> None:
            await self._sleep(delay)

        async def wait_for_stop() -> None:
            await self._stop_requested.wait()

        sleeper = asyncio.create_task(sleep_once())
        stopper = asyncio.create_task(wait_for_stop())
        tasks = {sleeper, stopper}
        try:
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sleeper in done and not sleeper.cancelled():
                error = sleeper.exception()
                if error is not None:
                    raise error
        finally:
            pending = {task for task in tasks if not task.done()}
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


def _agent_endpoint(
    value: str,
    agent_id: UUID,
    *,
    allow_insecure_loopback: bool,
) -> str:
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise AgentTransportSecurityError("Agent gateway URL is invalid") from exc
    if parsed.scheme not in {"ws", "wss"} or hostname is None:
        raise AgentTransportSecurityError("Agent gateway URL must use WSS")
    if parsed.username is not None or parsed.password is not None:
        raise AgentTransportSecurityError("Agent gateway URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise AgentTransportSecurityError("Agent gateway URL must not contain query or fragment")
    if parsed.scheme == "ws" and not (allow_insecure_loopback and _is_loopback_host(hostname)):
        raise AgentTransportSecurityError(
            "Insecure WS Agent transport is allowed only for explicit loopback development"
        )
    path = parsed.path.rstrip("/")
    if path.rsplit("/", maxsplit=1)[-1] != str(agent_id):
        path = f"{path}/{agent_id}"
    return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, path, "", ""))


def _is_loopback_host(value: str) -> bool:
    normalized = value.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _positive_integer(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _bounded_integer(value: int, *, field: str, maximum: int) -> None:
    _positive_integer(value, field=field)
    if value > maximum:
        raise ValueError(f"{field} cannot exceed {maximum:,}")


def _positive_number(value: float, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} must be positive")


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
