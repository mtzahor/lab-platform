from __future__ import annotations

import asyncio
import hashlib
import http.client as http_client
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import lab_platform.agent.distributed as distributed
import lab_platform.agent_runtime.connection_manager as connection_manager
import lab_platform.agent_runtime.sqlite_state as sqlite_state
import pytest
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    AgentStatus,
    AgentStatusPayload,
    MessageType,
    WelcomePayload,
    parse_control_plane_message,
)
from lab_platform.agent_runtime import (
    EVENT_BUFFER_OVERFLOW,
    AgentConnectionManager,
    AgentHandshakeError,
    AgentHeartbeatState,
    AgentMessageTooLargeError,
    AgentOutgoingQueueFullError,
    AgentTransportSecurityError,
    InMemoryAgentEventBuffer,
    InMemoryReservationLeaseStore,
)
from lab_platform.agent_runtime.leases import (
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.agent_runtime.sqlite_state import (
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.control_plane_core.errors import (
    ArtifactTransferFailedError,
    RemoteCommandRejectedError,
)
from lab_platform.models import (
    BufferedAgentEvent,
    BufferedEventPriority,
    RemoteCommand,
    RemoteCommandType,
    ReservationLease,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence import SQLiteDatabase
from pydantic import SecretStr

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)


class _NoopConnector:
    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> Any:
        del url, headers, maximum_message_size_bytes
        raise AssertionError("connection is not expected")


class _CloseableSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Reconciliation:
    def __init__(self, agent_id: UUID, boot_id: UUID) -> None:
        self.agent_id = agent_id
        self.boot_id = boot_id

    async def build(self, *, generated_at: datetime | None = None) -> Any:
        del generated_at
        raise AssertionError("reconciliation is not expected")


class _Commands:
    async def dispatch(self, request: Any) -> Any:
        del request
        raise AssertionError("dispatch is not expected")

    async def cancel(self, payload: Any) -> Any:
        return payload


class _Drain:
    async def apply_drain(self, payload: Any) -> None:
        del payload


def _manager_dependencies(
    *,
    agent_id: UUID | None = None,
    boot_id: UUID | None = None,
) -> tuple[UUID, UUID, dict[str, Any]]:
    selected_agent_id = agent_id or uuid4()
    selected_boot_id = boot_id or uuid4()
    events = InMemoryAgentEventBuffer(selected_agent_id, clock=lambda: NOW)
    leases = InMemoryReservationLeaseStore(selected_agent_id, clock=lambda: NOW)
    values: dict[str, Any] = {
        "agent_id": selected_agent_id,
        "boot_id": selected_boot_id,
        "agent_name": "coverage-agent",
        "agent_version": "0.6.0",
        "gateway_url": "wss://lab.example/api/v1/agent-gateway",
        "credential": SecretStr("credential"),
        "events": events,
        "leases": leases,
        "commands": _Commands(),
        "drain": _Drain(),
        "reconciliation": _Reconciliation(selected_agent_id, selected_boot_id),
        "connector": _NoopConnector(),
        "clock": lambda: NOW,
        "monotonic": lambda: 100.0,
    }
    return selected_agent_id, selected_boot_id, values


def _manager(**updates: Any) -> AgentConnectionManager:
    _agent_id, _boot_id, values = _manager_dependencies()
    values.update(updates)
    return AgentConnectionManager(**values)


class _DownloadResponse:
    def __init__(self, chunks: list[bytes], content_length: str | None) -> None:
        self._chunks = iter(chunks)
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self) -> _DownloadResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


class _UploadResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.read_called = False

    def read(self) -> bytes:
        self.read_called = True
        return b""


class _HttpConnection:
    instances: list[_HttpConnection] = []
    response_status = 204

    def __init__(self, host: str, port: int | None, *, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request: tuple[str, str] | None = None
        self.headers: list[tuple[str, str]] = []
        self.sent = bytearray()
        self.closed = False
        self.response = _UploadResponse(self.response_status)
        self.instances.append(self)

    def putrequest(self, method: str, path: str) -> None:
        self.request = (method, path)

    def putheader(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def endheaders(self) -> None:
        return None

    def send(self, chunk: bytes) -> None:
        self.sent.extend(chunk)

    def getresponse(self) -> _UploadResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def _descriptor(content: bytes, *, url: str = "https://lab.example/download") -> Any:
    return distributed.ArtifactTransferDescriptor(
        artifact_id=uuid4(),
        download_url=url,
        transfer_token=SecretStr("transfer-token"),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        expires_at=NOW + timedelta(minutes=5),
    )


def test_streaming_artifact_download_checks_headers_bounds_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"streamed-artifact"
    transport = distributed.StreamingArtifactHttpTransport()
    responses = iter(
        (
            _DownloadResponse([content[:4], content[4:]], str(len(content))),
            _DownloadResponse([content], str(len(content) + 1)),
            _DownloadResponse([content], None),
        )
    )
    monkeypatch.setattr(distributed, "urlopen", lambda *_args, **_kwargs: next(responses))

    destination = tmp_path / "download.bin"
    assert transport.download(
        _descriptor(content), destination, maximum_size_bytes=len(content)
    ) == (len(content), hashlib.sha256(content).hexdigest())
    assert destination.read_bytes() == content

    with pytest.raises(ArtifactTransferFailedError, match="length"):
        transport.download(
            _descriptor(content), tmp_path / "wrong-length.bin", maximum_size_bytes=len(content)
        )
    with pytest.raises(ArtifactTransferFailedError, match="size bound"):
        transport.download(
            _descriptor(content, url="https://lab.example/oversized"),
            tmp_path / "oversized.bin",
            maximum_size_bytes=len(content) - 1,
        )


def test_streaming_artifact_upload_streams_http_and_https_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"upload-content"
    source = tmp_path / "upload.bin"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    _HttpConnection.instances.clear()
    _HttpConnection.response_status = 204
    monkeypatch.setattr(http_client, "HTTPConnection", _HttpConnection)
    monkeypatch.setattr(http_client, "HTTPSConnection", _HttpConnection)
    transport = distributed.StreamingArtifactHttpTransport()

    transport.upload(
        "http://lab.example:8080/upload?part=1",
        "token",
        source,
        expected_sha256=digest,
        maximum_size_bytes=100,
    )
    transport.upload(
        "https://lab.example",
        "token",
        source,
        expected_sha256=digest,
        maximum_size_bytes=100,
    )
    first, second = _HttpConnection.instances
    assert first.request == ("PUT", "/upload?part=1")
    assert second.request == ("PUT", "/")
    assert bytes(first.sent) == content
    assert ("Authorization", "Bearer token") in first.headers
    assert first.closed and second.closed

    _HttpConnection.response_status = 503
    with pytest.raises(ArtifactTransferFailedError, match="rejected"):
        transport.upload(
            "https://lab.example/upload",
            "token",
            source,
            expected_sha256=digest,
            maximum_size_bytes=100,
        )
    assert _HttpConnection.instances[-1].closed


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"credential": "  "}, "credential"),
        ({"outgoing_queue_size": 0}, "outgoing_queue_size"),
        ({"event_batch_size": 10_001}, "cannot exceed"),
        ({"maximum_message_size_bytes": True}, "maximum_message_size_bytes"),
        ({"event_poll_interval_seconds": 0}, "event_poll_interval_seconds"),
        ({"event_ack_timeout_seconds": False}, "event_ack_timeout_seconds"),
        ({"handshake_timeout_seconds": -1}, "handshake_timeout_seconds"),
        ({"reconnect_initial_delay_seconds": 0}, "reconnect_initial_delay_seconds"),
        ({"reconnect_maximum_delay_seconds": 0.5}, "less than"),
        ({"reconnect_jitter_ratio": 1.1}, "between zero and one"),
        ({"reconnect_stability_seconds": 0}, "reconnect_stability_seconds"),
        ({"last_acknowledged_command_sequence": True}, "must be non-negative"),
        ({"maximum_clock_skew_seconds": -1}, "must be non-negative"),
    ),
)
def test_connection_manager_rejects_invalid_bounds(
    updates: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _manager(**updates)


def test_connection_manager_rejects_foreign_state_sources() -> None:
    agent_id, boot_id, values = _manager_dependencies()
    values["leases"] = InMemoryReservationLeaseStore(uuid4())
    with pytest.raises(ValueError, match="different Agent"):
        AgentConnectionManager(**values)

    _agent_id, _boot_id, values = _manager_dependencies(
        agent_id=agent_id,
        boot_id=boot_id,
    )
    values["reconciliation"] = _Reconciliation(agent_id, uuid4())
    with pytest.raises(ValueError, match="different Agent process"):
        AgentConnectionManager(**values)


def test_connection_manager_endpoint_and_numeric_helpers_cover_security_edges() -> None:
    agent_id = uuid4()
    assert connection_manager._agent_endpoint(
        f"ws://localhost.:8080/gateway/{agent_id}",
        agent_id,
        allow_insecure_loopback=True,
    ).endswith(str(agent_id))
    assert connection_manager._is_loopback_host("127.0.0.1")
    assert not connection_manager._is_loopback_host("lab.example")

    for security_url, message in (
        ("wss://lab.example:bad", "invalid"),
        ("https://lab.example", "use WSS"),
        ("wss://user:pass@lab.example", "credentials"),
        ("wss://lab.example/path?query=yes", "query or fragment"),
        ("ws://lab.example", "loopback"),
    ):
        with pytest.raises(AgentTransportSecurityError, match=message):
            connection_manager._agent_endpoint(
                security_url,
                agent_id,
                allow_insecure_loopback=False,
            )

    for integer_value in (True, 0, -1):
        with pytest.raises(ValueError, match="positive integer"):
            connection_manager._positive_integer(integer_value, field="count")
    with pytest.raises(ValueError, match="cannot exceed"):
        connection_manager._bounded_integer(2, field="count", maximum=1)
    for number_value in (True, "one", 0):
        with pytest.raises(ValueError, match="positive"):
            connection_manager._positive_number(cast(Any, number_value), field="delay")
    with pytest.raises(ValueError, match="timezone-aware"):
        connection_manager._as_utc(datetime(2026, 1, 1), field="clock")


@pytest.mark.anyio
async def test_connection_manager_queue_decode_and_socket_edges() -> None:
    manager = _manager(outgoing_queue_size=1, maximum_message_size_bytes=700)
    item = connection_manager._OutgoingItem(
        message_id=uuid4(),
        message_type=MessageType.AGENT_STATUS,
        payload=AgentStatusPayload(
            agent_id=manager._agent_id,
            boot_id=manager._boot_id,
            status=AgentStatus.ONLINE,
            changed_at=NOW,
        ),
        correlation_id=None,
        persistent=True,
    )
    ephemeral = connection_manager._OutgoingItem(
        message_id=uuid4(),
        message_type=MessageType.AGENT_STATUS,
        payload=item.payload,
        correlation_id=None,
        persistent=False,
    )
    assert await manager._enqueue(item, raise_when_full=True)
    assert not await manager._enqueue(ephemeral, raise_when_full=False)
    with pytest.raises(AgentOutgoingQueueFullError):
        await manager._enqueue(ephemeral, raise_when_full=True)
    assert await manager._queue_head(timeout=0) is item
    with pytest.raises(RuntimeError, match="ordering"):
        await manager._remove_queue_head(ephemeral)
    await manager._remove_queue_head(item)
    assert await manager._queue_head(timeout=0) is None
    assert await manager._queue_head(timeout=0.001) is None

    await manager._enqueue(ephemeral, raise_when_full=True)
    await manager._drop_ephemeral_messages()
    assert manager.queued_messages == 0
    assert not await manager.request_reconnect()
    socket = _CloseableSocket()
    manager._active_socket = cast(Any, socket)
    assert await manager.request_reconnect()
    assert socket.closed

    with pytest.raises(ValueError, match="reserved"):
        manager._validate_outgoing(
            connection_manager._OutgoingItem(
                message_id=uuid4(),
                message_type=MessageType.AGENT_HELLO,
                payload={},
                correlation_id=None,
                persistent=False,
            )
        )

    welcome = parse_control_plane_message(
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": uuid4(),
            "message_type": MessageType.WELCOME,
            "agent_id": manager._agent_id,
            "sent_at": NOW,
            "sequence_number": 1,
            "payload": WelcomePayload(
                connection_id=uuid4(),
                accepted_protocol_version=PROTOCOL_VERSION,
                server_time=NOW,
                heartbeat_interval_seconds=10,
                heartbeat_timeout_seconds=30,
                offline_timeout_seconds=60,
            ),
        }
    ).model_dump_json()
    decoded = manager._decode_control_plane_message(welcome.encode())
    assert decoded.message_type is MessageType.WELCOME
    for raw, error in (
        (b"\xff", AgentHandshakeError),
        (cast(Any, 42), AgentHandshakeError),
        ("x" * 701, AgentMessageTooLargeError),
        ("not-json", AgentHandshakeError),
    ):
        with pytest.raises(error):
            manager._decode_control_plane_message(raw)


def test_connection_manager_event_batch_binary_search_and_jitter() -> None:
    manager = _manager(
        maximum_message_size_bytes=900,
        reconnect_initial_delay_seconds=1,
        reconnect_maximum_delay_seconds=8,
        reconnect_jitter_ratio=0.5,
        random_source=lambda: -10,
    )
    events = tuple(
        BufferedAgentEvent(
            id=uuid4(),
            agent_id=manager._agent_id,
            sequence_number=index,
            event_type="STATE",
            payload={"value": "x" * 120},
            priority=BufferedEventPriority.STATE,
            created_at=NOW,
        )
        for index in range(1, 9)
    )
    selected, encoded = manager._event_batch_message(
        events,
        message_id=uuid4(),
        sequence_number=2,
    )
    assert 1 <= len(selected) < len(events)
    assert len(encoded.encode()) <= 900
    assert manager.reconnect_delay(0) == 0.5
    manager._random = lambda: 10
    assert manager.reconnect_delay(20) == 8
    with pytest.raises(ValueError, match="non-negative"):
        manager.reconnect_delay(True)


@pytest.mark.anyio
async def test_heartbeat_state_and_in_flight_watermark_validation() -> None:
    heartbeat = await connection_manager.EmptyHeartbeatSource().heartbeat_state()
    assert heartbeat == AgentHeartbeatState()
    for values, message in (
        ({"active_operations": -1}, "non-negative"),
        ({"connected_benches": True}, "non-negative"),
        ({"connected_benches": 1, "degraded_benches": 2}, "cannot exceed"),
    ):
        with pytest.raises(ValueError, match=message):
            AgentHeartbeatState(**values)
    batch = connection_manager._InFlightEventBatch(
        message_id=uuid4(),
        events=(
            BufferedAgentEvent(
                id=uuid4(),
                agent_id=uuid4(),
                sequence_number=7,
                event_type="STATE",
                payload={},
                priority=BufferedEventPriority.STATE,
                created_at=NOW,
            ),
        ),
    )
    assert batch.acknowledged_event_sequence == 7


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _lease(
    agent_id: UUID,
    *,
    reservation_id: UUID | None = None,
    bench_id: str = "agent/bench-1",
    version: int = 1,
) -> ReservationLease:
    return ReservationLease(
        reservation_id=reservation_id or uuid4(),
        agent_id=agent_id,
        bench_id=bench_id,
        owner="owner",
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        lease_version=version,
    )


@pytest.mark.anyio
async def test_sqlite_lease_store_covers_release_validation_and_integrity_edges(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "leases.db")
    agent_id = uuid4()
    store = SQLiteReservationLeaseStore(database, agent_id, clock=lambda: NOW)
    reservation_id = uuid4()
    lease = _lease(agent_id, reservation_id=reservation_id)

    with pytest.raises(ReservationLeaseVersionMismatchError, match="at least one"):
        await store.validate(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=lease.bench_id,
            lease_version=0,
        )
    with pytest.raises(ReservationLeaseInvalidError, match="No reservation"):
        await store.validate(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=lease.bench_id,
            lease_version=1,
        )
    assert await store.apply(lease) == lease
    with pytest.raises(ReservationLeaseVersionMismatchError, match="latest stored"):
        await store.validate(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=lease.bench_id,
            lease_version=2,
        )
    with pytest.raises(ReservationLeaseInvalidError, match="command reservation"):
        await store.validate(
            agent_id=agent_id,
            reservation_id=uuid4(),
            bench_id=lease.bench_id,
            lease_version=1,
        )

    apply_release = _lease(agent_id, bench_id="agent/apply-release")
    await store.apply(apply_release)
    released_by_apply = apply_release.model_copy(update={"released_at": NOW})
    assert await store.apply(released_by_apply) == sqlite_state._stored_from_lease(
        released_by_apply
    )

    tombstone = await store.release(
        agent_id=agent_id,
        reservation_id=reservation_id,
        bench_id=lease.bench_id,
        lease_version=1,
        released_at=NOW,
    )
    assert tombstone.released_at == NOW
    with pytest.raises(ReservationLeaseInvalidError, match="released"):
        await store.validate(
            agent_id=agent_id,
            reservation_id=reservation_id,
            bench_id=lease.bench_id,
            lease_version=1,
        )

    conflict_id = uuid4()
    await store.apply(_lease(agent_id, reservation_id=conflict_id, bench_id="agent/first"))
    with pytest.raises(ReservationLeaseInvalidError, match="identity conflicts"):
        await store.apply(_lease(agent_id, reservation_id=conflict_id, bench_id="agent/second"))

    release_conflict_id = uuid4()
    await store.apply(
        _lease(
            agent_id,
            reservation_id=release_conflict_id,
            bench_id="agent/third",
            version=2,
        )
    )
    with pytest.raises(ReservationLeaseInvalidError, match="release identity conflicts"):
        await store.release(
            agent_id=agent_id,
            reservation_id=release_conflict_id,
            bench_id="agent/fourth",
            lease_version=2,
            released_at=NOW,
        )
    database.close()


@pytest.mark.anyio
async def test_sqlite_event_buffer_input_and_metadata_invariants(tmp_path: Path) -> None:
    database = _database(tmp_path / "event-invariants.db")
    agent_id = uuid4()
    for capacity in (True, 0):
        with pytest.raises(ValueError, match="positive integer"):
            SQLiteAgentEventBuffer(database, agent_id, capacity=capacity)

    buffer = SQLiteAgentEventBuffer(database, agent_id, capacity=2, clock=lambda: NOW)
    assert buffer.agent_id == agent_id
    assert buffer.capacity == 2
    with pytest.raises(ValueError, match="peek limit"):
        await buffer.peek(limit=0)
    with pytest.raises(ValueError, match="acknowledged sequence"):
        await buffer.acknowledge_through(True)

    with database.transaction(immediate=True) as connection:
        state = buffer._load_state(connection)
        state["overflow_marker_id"] = str(uuid4())
        with pytest.raises(RuntimeError, match="overflow marker metadata"):
            buffer._record_overflow(connection, state, BufferedEventPriority.STATE)

        state = sqlite_state._initial_event_state(agent_id, 3)
        buffer._save_state(connection, state)
        with pytest.raises(ValueError, match="capacity changed"):
            buffer._load_state(connection)
    database.close()


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda _state: 42, "must be text"),
        (lambda _state: "[]", "must be an object"),
        (lambda state: {**state, "version": 99}, "version is unsupported"),
        (lambda state: {**state, "agent_id": str(uuid4())}, "another Agent"),
        (lambda state: {**state, "capacity": True}, "capacity is invalid"),
        (
            lambda state: {
                **state,
                "last_acknowledged_sequence": 2,
                "last_peeked_sequence": 1,
            },
            "ACK watermark",
        ),
        (lambda state: {**state, "dropped": []}, "drop counters"),
        (lambda state: {**state, "coalesce_sequences": []}, "coalescing map"),
        (lambda state: {**state, "known": []}, "known-event map"),
    ),
)
def test_sqlite_event_metadata_validation(
    mutate: Any,
    message: str,
) -> None:
    agent_id = uuid4()
    state = sqlite_state._initial_event_state(agent_id, 3)
    value = mutate(state)
    raw = value if isinstance(value, (str, int)) else json.dumps(value)
    with pytest.raises(ValueError, match=message):
        sqlite_state._parse_event_state(raw, agent_id)


def test_sqlite_state_private_helpers_reject_corrupt_payloads() -> None:
    agent_id = uuid4()
    state = sqlite_state._initial_event_state(agent_id, 2)
    state["coalesce_sequences"] = {"key": True}
    with pytest.raises(ValueError, match="coalescing entry"):
        sqlite_state._coalesce_sequences(state)

    state = sqlite_state._initial_event_state(agent_id, 2)
    dropped = cast(dict[str, object], state["dropped"])
    dropped[str(int(BufferedEventPriority.PROGRESS))] = -1
    with pytest.raises(ValueError, match="drop counter"):
        sqlite_state._dropped_counts(state)
    with pytest.raises(ValueError, match="next_sequence"):
        sqlite_state._state_int({"next_sequence": True}, "next_sequence")

    marker = BufferedAgentEvent(
        id=uuid4(),
        agent_id=agent_id,
        sequence_number=1,
        event_type=EVENT_BUFFER_OVERFLOW,
        payload={"dropped_progress": True},
        priority=BufferedEventPriority.FAILURE,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="marker payload"):
        sqlite_state._restore_dropped_from_marker(state, marker)
    with pytest.raises(ValueError, match="timezone-aware"):
        sqlite_state._as_utc(datetime(2026, 1, 1), field="timestamp")


@pytest.mark.anyio
async def test_sqlite_event_buffer_restores_legacy_rows_and_detects_corruption(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "legacy-events.db")
    agent_id = uuid4()
    overflow = BufferedAgentEvent(
        id=uuid4(),
        agent_id=agent_id,
        sequence_number=1,
        event_type=EVENT_BUFFER_OVERFLOW,
        payload={"dropped_progress": 2, "dropped_state": 1},
        priority=BufferedEventPriority.FAILURE,
        created_at=NOW,
    )
    progress = BufferedAgentEvent(
        id=uuid4(),
        agent_id=agent_id,
        sequence_number=2,
        event_type="OPERATION_PROGRESS",
        payload={"operation_id": "op-1"},
        priority=BufferedEventPriority.PROGRESS,
        created_at=NOW,
    )
    with database.transaction(immediate=True) as connection:
        sqlite_state._insert_event(connection, overflow)
        sqlite_state._insert_event(connection, progress)
    restored = SQLiteAgentEventBuffer(database, agent_id, capacity=3, clock=lambda: NOW)
    stats = await restored.stats()
    assert stats.last_issued_sequence == 2
    assert stats.dropped_progress == 2
    assert stats.dropped_state == 1

    with database.transaction(immediate=True) as connection:
        connection.execute(
            "DELETE FROM agent_runtime_metadata WHERE key = ?",
            (restored._metadata_key,),
        )
        with pytest.raises(RuntimeError, match="metadata disappeared"):
            restored._load_state(connection)

    with pytest.raises(ValueError, match="must be an object"):
        sqlite_state._event_from_row(cast(Any, {"payload_json": "[]"}))
    database.close()


class _WorkflowArtifacts:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released: list[str] = []

    async def fetch(self, descriptor: Any, *, pin: bool = False) -> Path:
        assert pin
        return self.path

    def release(self, digest: str) -> None:
        self.released.append(digest)


class _WorkflowEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, Mapping[str, Any], BufferedEventPriority, UUID]] = []

    async def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        priority: BufferedEventPriority,
        event_id: UUID,
    ) -> None:
        self.items.append((event_type, payload, priority, event_id))


class _WorkflowService:
    def __init__(self, status: WorkflowRunStatus, artifact_id: UUID) -> None:
        self.status = status
        self.artifact_id = artifact_id
        self.run_id = uuid4()
        self.cancelled: list[tuple[UUID, str]] = []

    async def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        return definition

    async def start(self, *_args: Any, **kwargs: Any) -> WorkflowRun:
        resolver = kwargs["artifact_resolver"]
        resolver(SimpleNamespace(artifact_id=self.artifact_id))
        return self._run(WorkflowRunStatus.RUNNING, current_step=0)

    async def wait(self, _run_id: UUID) -> WorkflowRun:
        await asyncio.sleep(0.12)
        return self._run(self.status, current_step=0)

    async def get_run(self, _run_id: UUID) -> WorkflowRun:
        return self._run(WorkflowRunStatus.RUNNING, current_step=0)

    async def list_step_results(self, _run_id: UUID) -> list[WorkflowStepResult]:
        return [
            WorkflowStepResult(
                workflow_run_id=self.run_id,
                step_index=0,
                action=WorkflowAction.WAIT,
                status=(
                    WorkflowStepStatus.SUCCEEDED
                    if self.status is WorkflowRunStatus.SUCCEEDED
                    else WorkflowStepStatus.FAILED
                ),
                artifact_ids=[self.artifact_id],
            )
        ]

    async def cancel(self, run_id: UUID, owner: str) -> None:
        self.cancelled.append((run_id, owner))

    def _run(self, status: WorkflowRunStatus, *, current_step: int | None) -> WorkflowRun:
        return WorkflowRun(
            id=self.run_id,
            workflow_name="coverage-workflow",
            workflow_version=1,
            bench_id="bench-1",
            owner="owner",
            reservation_id=uuid4(),
            status=status,
            current_step=current_step,
            created_at=NOW,
            error_code="workflow_failed" if status is WorkflowRunStatus.FAILED else None,
            error_message="injected failure" if status is WorkflowRunStatus.FAILED else None,
        )


def _workflow_command(descriptor: Any, definition: WorkflowDefinition) -> RemoteCommand:
    return RemoteCommand(
        agent_id=UUID("11111111-1111-4111-8111-111111111111"),
        bench_id="agent/bench-1",
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={
            "workflow": definition.model_dump(mode="json"),
            "owner": "owner",
            "artifact_transfers": [descriptor.model_dump(mode="json")],
        },
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        idempotency_key=f"workflow-{uuid4()}",
        operation_id=uuid4(),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.SUCCEEDED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.CANCELLED,
    ],
)
async def test_remote_workflow_execution_reports_steps_artifacts_and_failures(
    tmp_path: Path,
    status: WorkflowRunStatus,
) -> None:
    content = b"workflow-input"
    path = tmp_path / f"{status}.bin"
    path.write_bytes(content)
    descriptor = _descriptor(content)
    definition = WorkflowDefinition.model_validate(
        {
            "name": "coverage-workflow",
            "version": 1,
            "requirements": {"capabilities": []},
            "steps": [{"action": "wait", "seconds": 0.01}],
        }
    )
    service = _WorkflowService(status, descriptor.artifact_id)
    artifacts = _WorkflowArtifacts(path)
    events = _WorkflowEvents()
    artifact_record = SimpleNamespace(
        id=descriptor.artifact_id,
        name="result.txt",
        artifact_type="workflow_result",
        content_type="text/plain",
        size_bytes=4,
        sha256="0" * 64,
        created_at=NOW,
    )
    artifact_service = SimpleNamespace(get=lambda _artifact_id: artifact_record)

    async def get_artifact(_artifact_id: UUID) -> Any:
        return artifact_record

    artifact_service.get = get_artifact
    agent = SimpleNamespace(artifact_service=artifact_service)
    executor = distributed.LocalAgentCommandExecutor(
        cast(Any, agent),
        locks=cast(Any, SimpleNamespace()),
        workflows=cast(Any, service),
        workflow_runner=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, artifacts),
        events=cast(Any, events),
    )
    progress_log: list[tuple[int | None, str | None]] = []
    progress_results: list[Mapping[str, Any]] = []

    async def report_progress(
        *,
        progress: int | None = None,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        progress_log.append((progress, message))
        if result is not None:
            progress_results.append(result)

    command = _workflow_command(descriptor, definition)
    if status is not WorkflowRunStatus.SUCCEEDED:
        expected_message = (
            "injected failure" if status is WorkflowRunStatus.FAILED else "Remote workflow failed"
        )
        with pytest.raises(RemoteCommandRejectedError, match=expected_message):
            await executor._run_workflow(command, report_progress=report_progress)
    else:
        result = await executor._run_workflow(command, report_progress=report_progress)
        assert result["workflow_run"]["status"] == WorkflowRunStatus.SUCCEEDED
    assert artifacts.released == [descriptor.sha256]
    assert events.items[0][0] == MessageType.ARTIFACT_CREATED.value
    assert any(value == 99 for value, _message in progress_log)
    assert progress_results[-1]["workflow_run"]["status"] == status
    assert progress_results[-1]["steps"][0]["step_index"] == 0


@pytest.mark.anyio
async def test_remote_workflow_task_cancellation_preserves_latest_snapshot(
    tmp_path: Path,
) -> None:
    content = b"workflow-input"
    path = tmp_path / "cancelled-task.bin"
    path.write_bytes(content)
    descriptor = _descriptor(content)
    definition = WorkflowDefinition.model_validate(
        {
            "name": "coverage-workflow",
            "version": 1,
            "requirements": {"capabilities": []},
            "steps": [{"action": "wait", "seconds": 30}],
        }
    )
    service = _WorkflowService(WorkflowRunStatus.SUCCEEDED, descriptor.artifact_id)
    wait_started = asyncio.Event()

    async def wait_forever(_run_id: UUID) -> WorkflowRun:
        wait_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service.wait = wait_forever  # type: ignore[method-assign]
    artifacts = _WorkflowArtifacts(path)
    executor = distributed.LocalAgentCommandExecutor(
        cast(Any, SimpleNamespace(artifact_service=SimpleNamespace())),
        locks=cast(Any, SimpleNamespace()),
        workflows=cast(Any, service),
        workflow_runner=cast(Any, SimpleNamespace()),
        artifacts=cast(Any, artifacts),
        events=cast(Any, _WorkflowEvents()),
    )
    progress_results: list[Mapping[str, Any]] = []

    async def report_progress(
        *,
        progress: int | None = None,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        del progress, message
        if result is not None:
            progress_results.append(result)

    task = asyncio.create_task(
        executor._run_workflow(
            _workflow_command(descriptor, definition),
            report_progress=report_progress,
        )
    )
    await wait_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert artifacts.released == [descriptor.sha256]
    assert progress_results[-1]["workflow_run"]["status"] == WorkflowRunStatus.RUNNING
    assert progress_results[-1]["steps"][0]["step_index"] == 0
