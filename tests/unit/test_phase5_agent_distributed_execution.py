from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent.distributed import (
    AgentArtifactCache,
    AgentDistributedRuntime,
    ArtifactTransferDescriptor,
    LabAgentInventoryAdapter,
    LocalAgentCommandExecutor,
    LocalAgentSafetyAdapter,
    StreamingArtifactHttpTransport,
    _AuxiliaryAdapter,
    _bench_health,
    _connectivity,
    _DrainAdapter,
    _global_status,
    _hash_file,
    _http_origin,
    _LeaseWorkflowAuthorizer,
    _local_bench_id,
    _optional_mapping,
    _optional_text,
    _relative_to_storage,
    _RemoteWorkflowLockAdapter,
    _require_descriptor_agent,
    _require_same_origin,
    _required_mapping,
    _required_text,
    _validate_command_payload,
)
from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    ArtifactUploadRequestEnvelope,
    ArtifactUploadRequestPayload,
    BenchConnectivity,
    BenchHealth,
    BenchSnapshotPayload,
    ConfigRefreshRequestEnvelope,
    ConfigRefreshRequestPayload,
    InventoryRefreshRequestPayload,
    MessageType,
)
from lab_platform.agent_runtime.sqlite_state import (
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.control_plane_core.errors import (
    ArtifactTransferFailedError,
    RemoteCommandRejectedError,
)
from lab_platform.core.bench_catalog import BenchRecord
from lab_platform.core.errors import (
    BenchNotFoundError,
    BenchOfflineError,
    BenchOperationInProgressError,
    CapabilityNotSupportedError,
    ConfigurationError,
    InvalidArtifactError,
    ReservationNotActiveError,
    ReservationOwnerMismatchError,
)
from lab_platform.models import (
    BenchOperationLock,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    RemoteCommandType,
    ReservationLease,
)
from lab_platform.persistence import SQLiteDatabase, SQLiteOperationLockRepository
from pydantic import SecretStr, ValidationError

AGENT_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime.now(UTC)


class _ArtifactTransport:
    def __init__(self, content: bytes = b"phase-five-firmware") -> None:
        self.content = content
        self.uploads: list[tuple[str, str, Path, str, int]] = []

    def download(
        self,
        descriptor: ArtifactTransferDescriptor,
        destination: Path,
        *,
        maximum_size_bytes: int,
    ) -> tuple[int, str]:
        assert len(self.content) <= maximum_size_bytes
        destination.write_bytes(self.content)
        return len(self.content), hashlib.sha256(self.content).hexdigest()

    def upload(
        self,
        url: str,
        token: str,
        source: Path,
        *,
        expected_sha256: str,
        maximum_size_bytes: int,
    ) -> None:
        self.uploads.append((url, token, source, expected_sha256, maximum_size_bytes))


class _Sender:
    def __init__(self) -> None:
        self.messages: list[tuple[MessageType, object]] = []

    async def send(
        self,
        message_type: MessageType,
        payload: object,
        *,
        correlation_id: UUID | None = None,
    ) -> UUID:
        assert correlation_id is None
        self.messages.append((message_type, payload))
        return UUID(int=len(self.messages))


class _ExecutorCount:
    active_count = 2


class _FakeCatalog:
    def __init__(self, records: list[BenchRecord]) -> None:
        self.records = records

    def list(self) -> list[BenchRecord]:
        return self.records


class _FakeLocks:
    def __init__(self) -> None:
        self.lock: BenchOperationLock | None = None

    async def get(self, _bench_id: str) -> BenchOperationLock | None:
        return self.lock


class _FakeArtifactService:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def content_path(self, _artifact_id: UUID) -> Path:
        return self.path


class _FakeAgent:
    def __init__(self, records: list[BenchRecord], artifact_path: Path | None = None) -> None:
        self.catalog = _FakeCatalog(records)
        if artifact_path is not None:
            self.artifact_service = _FakeArtifactService(artifact_path)


class _Handler:
    def __init__(self) -> None:
        self.draining = False

    def set_draining(self, value: bool) -> None:
        self.draining = value


def _record(*, online: bool = True, health: HealthStatus = HealthStatus.HEALTHY) -> BenchRecord:
    return BenchRecord(
        id="bench-01",
        backend_id="simlab",
        name="bench-01",
        online=online,
        health=health,
        capabilities={"probe", "reset", "firmware", "serial"},
        labels={"board": "esp32"},
        created_at=NOW,
        updated_at=NOW,
        last_seen_at=NOW,
    )


def _command(
    command_type: RemoteCommandType,
    payload: dict[str, Any] | None = None,
) -> RemoteCommand:
    return RemoteCommand(
        agent_id=AGENT_ID,
        bench_id="integration-agent/bench-01",
        command_type=command_type,
        payload=payload or {},
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key=f"test-{command_type.value}-{uuid4()}",
        operation_id=uuid4(),
    )


def _descriptor(content: bytes, **updates: object) -> ArtifactTransferDescriptor:
    values: dict[str, object] = {
        "artifact_id": uuid4(),
        "agent_id": AGENT_ID,
        "download_url": "https://control.example/api/v1/artifact-transfers/1/content",
        "transfer_token": SecretStr("transfer-secret"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "expires_at": NOW + timedelta(minutes=5),
        "target_path": "inputs/firmware.bin",
        "name": "firmware.bin",
    }
    values.update(updates)
    return ArtifactTransferDescriptor.model_validate(values)


def _write_local_config(root: Path) -> None:
    (root / "agent.yaml").write_text(
        "agent:\n  name: integration-agent\n  log_level: ERROR\nplugins: []\n",
        encoding="utf-8",
    )
    (root / "simlab.yaml").write_text(
        "simlab:\n  benches: 1\n  speed_multiplier: 1000\n  flash_duration_seconds: 0\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_direct_remote_executor_runs_every_local_command(tmp_path: Path) -> None:
    _write_local_config(tmp_path)
    agent = create_agent(tmp_path)
    await agent.start()
    content = b"phase-five-firmware"
    transport = _ArtifactTransport(content)
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=1024,
        transport=transport,
    )
    locks = SQLiteOperationLockRepository(agent._database)
    events = SQLiteAgentEventBuffer(agent._database, AGENT_ID)
    executor = LocalAgentCommandExecutor(
        agent,
        locks=locks,
        workflows=agent.workflow_service,
        workflow_runner=agent._workflow_runner,
        artifacts=cache,
        events=events,
    )
    progress: list[tuple[int | None, str | None]] = []

    async def report(
        *,
        progress: int | None = None,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        del result
        progress_values = (progress, message)
        progress_log.append(progress_values)

    progress_log = progress
    try:

        class DistributedInventoryPublisher:
            started = True
            calls = 0

            async def publish_inventory(self, *, force: bool = False) -> UUID:
                assert not force
                self.calls += 1
                return UUID(int=self.calls)

        publisher = DistributedInventoryPublisher()
        agent.distributed_runtime = cast(Any, publisher)
        agent.catalog.mark_probe_required("bench-01")
        probe = await executor.execute(
            _command(RemoteCommandType.PROBE),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert probe is not None and probe["status"] == "online"
        assert publisher.calls == 1

        reset = await executor.execute(
            _command(RemoteCommandType.RESET),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert reset == {"reset": True}

        serial = await executor.execute(
            _command(
                RemoteCommandType.READ_SERIAL,
                {"request": {"timeout_seconds": 0.1, "max_lines": 2}},
            ),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert serial is not None and serial["line_count"] == 2

        descriptor = _descriptor(content)
        flashed = await executor.execute(
            _command(
                RemoteCommandType.FLASH,
                {"artifact": descriptor.model_dump(mode="json"), "version": "0.6.0"},
            ),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert flashed is not None and flashed["sha256"] == descriptor.sha256

        refreshed = await executor.execute(
            _command(RemoteCommandType.REFRESH_INVENTORY),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert refreshed == {"bench_count": 1, "offline_benches": []}

        target = uuid4()
        cancelled = await executor.execute(
            _command(RemoteCommandType.CANCEL_OPERATION, {"command_id": str(target)}),
            local_operation_id=uuid4(),
            report_progress=report,
        )
        assert cancelled == {"cancelled_command_id": str(target)}
        await executor.cancel(command_id=target, local_operation_id=uuid4(), reason=None)
        assert executor.active_count == 0
        assert progress

        with pytest.raises(RemoteCommandRejectedError, match="does not support"):
            await executor._run_direct(
                _command(RemoteCommandType.RUN_WORKFLOW),
                local_operation_id=uuid4(),
                report_progress=report,
            )
    finally:
        agent.distributed_runtime = None
        await agent.shutdown()


@pytest.mark.anyio
async def test_inventory_safety_lease_lock_and_drain_adapters(tmp_path: Path) -> None:
    _write_local_config(tmp_path)
    agent = create_agent(tmp_path)
    await agent.start()
    try:
        inventory = LabAgentInventoryAdapter(agent, boot_id=uuid4())
        sender = _Sender()
        inventory.bind(cast(Any, sender), cast(Any, _ExecutorCount()))
        snapshot = await inventory.snapshot()
        assert len(snapshot) == 1 and snapshot[0].kind.value == "SIMULATED"
        heartbeat = await inventory.heartbeat_state()
        assert heartbeat.active_operations == 2
        assert await inventory.publish() == UUID(int=1)
        assert await inventory.publish() == UUID(int=1)
        assert len(sender.messages) == 1
        await inventory.refresh_inventory(InventoryRefreshRequestPayload(request_id=uuid4()))
        assert [kind for kind, _payload in sender.messages] == [
            MessageType.BENCH_SNAPSHOT,
            MessageType.BENCH_SNAPSHOT,
        ]
        with pytest.raises(RuntimeError, match="not bound"):
            await LabAgentInventoryAdapter(agent, boot_id=uuid4()).publish()
    finally:
        await agent.shutdown()

    locks = _FakeLocks()
    safety = LocalAgentSafetyAdapter(cast(LabAgent, _FakeAgent([_record()])), cast(Any, locks))
    request = SimpleNamespace(command=_command(RemoteCommandType.PROBE))
    await safety.validate(request, required_capability="probe")

    with pytest.raises(BenchNotFoundError):
        await LocalAgentSafetyAdapter(cast(LabAgent, _FakeAgent([])), cast(Any, locks)).validate(
            request, required_capability=None
        )
    with pytest.raises(BenchOfflineError):
        await LocalAgentSafetyAdapter(
            cast(LabAgent, _FakeAgent([_record(online=False)])), cast(Any, locks)
        ).validate(request, required_capability=None)
    with pytest.raises(CapabilityNotSupportedError):
        await safety.validate(request, required_capability="power")

    locks.lock = BenchOperationLock(bench_id="bench-01", operation_id=uuid4(), acquired_at=NOW)
    with pytest.raises(BenchOperationInProgressError):
        await safety.validate(request, required_capability=None)

    database = SQLiteDatabase(tmp_path / "adapters.db")
    database.initialize()
    lease_store = SQLiteReservationLeaseStore(database, AGENT_ID)
    lease = ReservationLease(
        reservation_id=uuid4(),
        agent_id=AGENT_ID,
        bench_id="integration-agent/bench-01",
        owner="ci-owner",
        valid_from=NOW - timedelta(seconds=1),
        valid_until=NOW + timedelta(minutes=5),
        lease_version=1,
    )
    await lease_store.apply(lease)
    authorizer = _LeaseWorkflowAuthorizer(lease_store)
    assert await authorizer.require_active("bench-01", "ci-owner") == lease.reservation_id
    with pytest.raises(ReservationOwnerMismatchError):
        await authorizer.require_active("bench-01", "wrong-owner")
    with pytest.raises(ReservationNotActiveError):
        await authorizer.require_active("missing", "ci-owner")

    persistent_locks = SQLiteOperationLockRepository(database)
    workflow_locks = _RemoteWorkflowLockAdapter(persistent_locks)
    operation_id = uuid4()
    acquired = await workflow_locks.acquire("bench-02", operation_id, "owner")
    assert acquired.operation_id == operation_id
    assert await workflow_locks.release("bench-02", operation_id)

    handler = _Handler()
    drain = _DrainAdapter(cast(Any, handler))
    await drain.apply_drain(SimpleNamespace(drain=True))
    assert handler.draining
    database.close()


@pytest.mark.anyio
async def test_inventory_publications_are_serialized_and_keep_newest_snapshot(
    tmp_path: Path,
) -> None:
    _write_local_config(tmp_path)
    agent = create_agent(tmp_path)
    await agent.start()

    class GatedSender:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.calls = 0
            self.payloads: list[BenchSnapshotPayload] = []

        async def send(
            self,
            message_type: MessageType,
            payload: object,
            *,
            correlation_id: UUID | None = None,
        ) -> UUID:
            assert message_type is MessageType.BENCH_SNAPSHOT
            assert correlation_id is None
            assert isinstance(payload, BenchSnapshotPayload)
            self.calls += 1
            call = self.calls
            if call == 1:
                self.first_started.set()
                await self.release_first.wait()
            self.payloads.append(payload)
            return UUID(int=call)

    try:
        inventory = LabAgentInventoryAdapter(agent, boot_id=uuid4())
        sender = GatedSender()
        inventory.bind(cast(Any, sender), cast(Any, _ExecutorCount()))
        first = asyncio.create_task(inventory.publish(force=True))
        await sender.first_started.wait()

        agent.catalog.mark_probe_required("bench-01")
        second = asyncio.create_task(inventory.publish(force=True))
        await asyncio.sleep(0)
        assert sender.calls == 1

        sender.release_first.set()
        await asyncio.gather(first, second)
        assert [payload.benches[0].connectivity for payload in sender.payloads] == [
            BenchConnectivity.ONLINE,
            BenchConnectivity.OFFLINE,
        ]
    finally:
        await agent.shutdown()


@pytest.mark.anyio
async def test_auxiliary_upload_enforces_scope_expiry_and_size(tmp_path: Path) -> None:
    content = b"artifact-output"
    artifact_path = tmp_path / "output.bin"
    artifact_path.write_bytes(content)
    transport = _ArtifactTransport(content)
    auxiliary = _AuxiliaryAdapter(
        cast(LabAgent, _FakeAgent([], artifact_path)),
        control_plane_url="https://control.example",
        transport=transport,
        maximum_size_bytes=1024,
    )
    artifact_id = uuid4()

    def envelope(**updates: object) -> ArtifactUploadRequestEnvelope:
        values: dict[str, object] = {
            "artifact_id": artifact_id,
            "local_artifact_id": artifact_id,
            "transfer_id": uuid4(),
            "upload_url": "https://control.example/api/v1/artifact-transfers/1/content",
            "transfer_token": SecretStr("upload-token"),
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "maximum_size_bytes": 1024,
            "expires_at": NOW + timedelta(minutes=5),
        }
        values.update(updates)
        return ArtifactUploadRequestEnvelope(
            protocol_version=PROTOCOL_VERSION,
            message_id=uuid4(),
            message_type=MessageType.ARTIFACT_UPLOAD_REQUEST,
            agent_id=AGENT_ID,
            sent_at=NOW,
            sequence_number=1,
            payload=ArtifactUploadRequestPayload.model_validate(values),
        )

    await auxiliary.handle_auxiliary_message(envelope())
    assert transport.uploads[0][2] == artifact_path

    config_envelope = ConfigRefreshRequestEnvelope(
        protocol_version=PROTOCOL_VERSION,
        message_id=uuid4(),
        message_type=MessageType.CONFIG_REFRESH_REQUEST,
        agent_id=AGENT_ID,
        sent_at=NOW,
        sequence_number=2,
        payload=ConfigRefreshRequestPayload(config_version=1),
    )
    await auxiliary.handle_auxiliary_message(config_envelope)

    with pytest.raises(ArtifactTransferFailedError, match="configured control plane"):
        await auxiliary.handle_auxiliary_message(envelope(upload_url="https://evil.invalid/file"))
    with pytest.raises(ArtifactTransferFailedError, match="expired"):
        await auxiliary.handle_auxiliary_message(
            envelope(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(ArtifactTransferFailedError, match="exceeds"):
        await auxiliary.handle_auxiliary_message(envelope(maximum_size_bytes=2048))
    with pytest.raises(RuntimeError, match="Unsupported"):
        await auxiliary.handle_auxiliary_message(cast(Any, object()))


def test_transfer_descriptor_and_distributed_helpers_reject_unsafe_input(tmp_path: Path) -> None:
    content = b"content"
    descriptor = _descriptor(content, sha256=hashlib.sha256(content).hexdigest().upper())
    assert descriptor.sha256 == hashlib.sha256(content).hexdigest()
    assert descriptor.expires_at is not None and descriptor.expires_at.tzinfo is UTC

    for target in ("/absolute/file", "../escape"):
        with pytest.raises(ValidationError, match="safe relative"):
            _descriptor(content, target_path=target)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _descriptor(content, expires_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="64 hexadecimal"):
        _descriptor(content, sha256="x" * 64)

    assert _http_origin("wss://Control.Example:8443/ws") == (
        "https",
        "control.example",
        8443,
    )
    assert (
        _require_same_origin(
            "https://control.example:8443/file", _http_origin("wss://control.example:8443/ws")
        ).path
        == "/file"
    )
    for value in ("ftp://control.example", "https:///missing-host"):
        with pytest.raises(ConfigurationError):
            _http_origin(value)
    with pytest.raises(ArtifactTransferFailedError):
        _require_same_origin(
            "https://user:password@control.example/file",
            ("https", "control.example", None),
        )

    assert _local_bench_id("agent/bench") == "bench"
    for value in ("bench", "/bench", "agent/", "agent/nested/bench"):
        with pytest.raises(BenchNotFoundError):
            _local_bench_id(value)

    payload: Mapping[str, Any] = {"mapping": {"x": 1}, "text": " value "}
    assert _required_mapping(payload, "mapping") == {"x": 1}
    assert _optional_mapping(payload, "missing") == {}
    assert _required_text(payload, "text") == "value"
    assert _optional_text(payload, "missing") is None
    for helper, key in (
        (_required_mapping, "text"),
        (_optional_mapping, "text"),
        (_required_text, "missing"),
        (_optional_text, "mapping"),
    ):
        with pytest.raises(RemoteCommandRejectedError):
            helper(payload, key)

    _validate_command_payload(_command(RemoteCommandType.PROBE))
    _validate_command_payload(_command(RemoteCommandType.READ_SERIAL, {"request": {}}))
    _validate_command_payload(
        _command(RemoteCommandType.CANCEL_OPERATION, {"command_id": str(uuid4())})
    )
    with pytest.raises(RemoteCommandRejectedError):
        _validate_command_payload(_command(RemoteCommandType.FLASH, {"artifact": "bad"}))
    with pytest.raises(InvalidArtifactError):
        _require_descriptor_agent(_descriptor(content, agent_id=uuid4()), AGENT_ID)

    file_path = tmp_path / "hash.bin"
    file_path.write_bytes(content)
    assert _hash_file(file_path, maximum_size_bytes=len(content))[0] == len(content)
    with pytest.raises(ArtifactTransferFailedError, match="size bound"):
        _hash_file(file_path, maximum_size_bytes=len(content) - 1)
    assert _relative_to_storage(Path("state"), tmp_path) == tmp_path / "state"
    assert _relative_to_storage(file_path, tmp_path) == file_path

    assert _global_status(_record()) is GlobalBenchStatus.ONLINE
    assert _global_status(_record(health=HealthStatus.WARNING)) is GlobalBenchStatus.DEGRADED
    assert _global_status(_record(online=False)) is GlobalBenchStatus.OFFLINE
    assert _connectivity(GlobalBenchStatus.ONLINE) is BenchConnectivity.ONLINE
    assert _connectivity(GlobalBenchStatus.DEGRADED) is BenchConnectivity.DEGRADED
    assert _connectivity(GlobalBenchStatus.OFFLINE) is BenchConnectivity.OFFLINE
    assert _bench_health(HealthStatus.HEALTHY) is BenchHealth.HEALTHY
    assert _bench_health(HealthStatus.WARNING) is BenchHealth.WARNING
    assert _bench_health(HealthStatus.UNHEALTHY) is BenchHealth.UNHEALTHY


@pytest.mark.anyio
async def test_composed_distributed_runtime_reports_status_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "agent.yaml").write_text(
        f"""
agent:
  name: distributed-test
  log_level: ERROR
control_plane:
  enabled: true
  url: ws://127.0.0.1:9/api/v1/agent-gateway/{AGENT_ID}
  allow_insecure_loopback: true
  reconnect:
    initial_delay_seconds: 0.01
    maximum_delay_seconds: 0.02
    jitter: false
    stability_seconds: 0.01
identity:
  agent_id: {AGENT_ID}
  credential_env_var: PHASE5_TEST_AGENT_CREDENTIAL
plugins: []
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  benches: 1\n  speed_multiplier: 1000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PHASE5_TEST_AGENT_CREDENTIAL", "lpa_" + "x" * 43)
    agent = create_agent(tmp_path)
    await agent.start()
    try:
        runtime = agent.distributed_runtime
        assert isinstance(runtime, AgentDistributedRuntime)
        assert runtime.started
        await asyncio.sleep(0.03)
        status = await runtime.status()
        assert status["enabled"] is True
        assert status["agent_id"] == str(AGENT_ID)
        assert status["local_benches_online"] == 1
        assert status["journal_entries"] == 0
        await runtime.reconnect()
        await runtime.start()
    finally:
        await agent.shutdown()
    assert agent.distributed_runtime is None


def test_streaming_transport_rejects_bad_upload_urls_and_changed_content(tmp_path: Path) -> None:
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"content")
    transport = StreamingArtifactHttpTransport()
    digest = hashlib.sha256(b"content").hexdigest()

    with pytest.raises(ArtifactTransferFailedError, match="checksum changed"):
        transport.upload(
            "https://control.example/upload",
            "token",
            source,
            expected_sha256="0" * 64,
            maximum_size_bytes=100,
        )
    with pytest.raises(ArtifactTransferFailedError, match="unsupported scheme"):
        transport.upload(
            "ftp://control.example/upload",
            "token",
            source,
            expected_sha256=digest,
            maximum_size_bytes=100,
        )
    with pytest.raises(ArtifactTransferFailedError, match="no host"):
        transport.upload(
            "https:///upload",
            "token",
            source,
            expected_sha256=digest,
            maximum_size_bytes=100,
        )
