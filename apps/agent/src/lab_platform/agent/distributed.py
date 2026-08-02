from __future__ import annotations

import asyncio
import hashlib
import http.client
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import SplitResult, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from lab_platform.agent_protocol import (
    ArtifactCreatedPayload,
    ArtifactUploadRequestEnvelope,
    BenchConnectivity,
    BenchHealth,
    BenchKind,
    BenchSnapshotPayload,
    ConfigRefreshRequestEnvelope,
    InventoryRefreshRequestPayload,
    MessageType,
)
from lab_platform.agent_runtime import (
    AgentBenchSafetyPort,
    AgentCommandExecutor,
    AgentCommandHandler,
    AgentConnectionManager,
    AgentHeartbeatState,
    AgentReconciliationReportBuilder,
    CommandProgressReporter,
    SQLiteAgentCommandJournal,
)
from lab_platform.agent_runtime.sqlite_state import (
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.control_plane_core.errors import (
    ArtifactTransferFailedError,
    RemoteCommandRejectedError,
)
from lab_platform.core import VERSION
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
from lab_platform.core.workflows import WorkflowRunner, WorkflowService
from lab_platform.models import (
    BenchOperationLock,
    BufferedEventPriority,
    CommandJournalEntry,
    FirmwareInput,
    GlobalBenchKind,
    GlobalBenchStatus,
    HealthStatus,
    ReconciliationBenchSnapshot,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
    SerialReadRequest,
    WorkflowDefinition,
    WorkflowRunStatus,
)
from lab_platform.persistence import (
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationLockRepository,
)
from lab_platform.persistence.workflows import SQLiteWorkflowRepository
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

if TYPE_CHECKING:
    from lab_platform.agent.runtime import LabAgent
    from lab_platform.agent_protocol import SupportedEnvelope
    from lab_platform.core.bench_catalog import BenchRecord


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ArtifactTransferDescriptor(_StrictModel):
    artifact_id: UUID
    input_name: str | None = Field(default=None, min_length=1, max_length=500)
    agent_id: UUID | None = None
    transfer_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=500)
    download_url: str = Field(min_length=1, max_length=4000)
    transfer_token: SecretStr
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(min_length=64, max_length=64)
    content_type: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    target_path: str | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        digest = value.casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Artifact SHA-256 must contain 64 hexadecimal characters")
        return digest

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Artifact transfer expiry must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("target_path")
    @classmethod
    def validate_target_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute() or "." in path.parts or ".." in path.parts:
            raise ValueError("Artifact target path must be a safe relative path")
        return value


class ArtifactHttpTransport(Protocol):
    def download(
        self,
        descriptor: ArtifactTransferDescriptor,
        destination: Path,
        *,
        maximum_size_bytes: int,
    ) -> tuple[int, str]: ...

    def upload(
        self,
        url: str,
        token: str,
        source: Path,
        *,
        expected_sha256: str,
        maximum_size_bytes: int,
    ) -> None: ...


class StreamingArtifactHttpTransport:
    """Bounded HTTP transfer adapter; binary content never enters the WebSocket."""

    def download(
        self,
        descriptor: ArtifactTransferDescriptor,
        destination: Path,
        *,
        maximum_size_bytes: int,
    ) -> tuple[int, str]:
        request = Request(
            descriptor.download_url,
            headers={"Authorization": f"Bearer {descriptor.transfer_token.get_secret_value()}"},
            method="GET",
        )
        digest = hashlib.sha256()
        received = 0
        with urlopen(request, timeout=60) as response, destination.open("xb") as stream:  # noqa: S310
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None and int(raw_length) != descriptor.size_bytes:
                raise ArtifactTransferFailedError(
                    "Artifact response length does not match transfer metadata."
                )
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > descriptor.size_bytes or received > maximum_size_bytes:
                    raise ArtifactTransferFailedError("Artifact download exceeded its size bound.")
                digest.update(chunk)
                stream.write(chunk)
        return received, digest.hexdigest()

    def upload(
        self,
        url: str,
        token: str,
        source: Path,
        *,
        expected_sha256: str,
        maximum_size_bytes: int,
    ) -> None:
        size, digest = _hash_file(source, maximum_size_bytes=maximum_size_bytes)
        if digest != expected_sha256:
            raise ArtifactTransferFailedError("Local artifact checksum changed before upload.")
        endpoint = urlsplit(url)
        connection_type: type[http.client.HTTPConnection]
        if endpoint.scheme == "https":
            connection_type = http.client.HTTPSConnection
        elif endpoint.scheme == "http":
            connection_type = http.client.HTTPConnection
        else:
            raise ArtifactTransferFailedError("Artifact upload URL uses an unsupported scheme.")
        if endpoint.hostname is None:
            raise ArtifactTransferFailedError("Artifact upload URL has no host.")
        connection = connection_type(endpoint.hostname, endpoint.port, timeout=60)
        path = endpoint.path or "/"
        if endpoint.query:
            path = f"{path}?{endpoint.query}"
        try:
            connection.putrequest("PUT", path)
            connection.putheader("Authorization", f"Bearer {token}")
            connection.putheader("Content-Length", str(size))
            connection.putheader("Content-Type", "application/octet-stream")
            connection.endheaders()
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            response.read()
            if response.status < 200 or response.status >= 300:
                raise ArtifactTransferFailedError(
                    "Control plane rejected the artifact upload.",
                    http_status=response.status,
                )
        finally:
            connection.close()


class AgentArtifactCache:
    """Immutable checksum cache rooted below the Agent-owned data directory."""

    def __init__(
        self,
        root: Path,
        control_plane_url: str,
        *,
        maximum_size_bytes: int,
        transport: ArtifactHttpTransport | None = None,
    ) -> None:
        if maximum_size_bytes <= 0:
            raise ValueError("Artifact cache size bound must be positive")
        self._root = root.resolve()
        self._origin = _http_origin(control_plane_url)
        self._maximum_size = maximum_size_bytes
        self._transport = transport or StreamingArtifactHttpTransport()
        self._locks: dict[str, asyncio.Lock] = {}
        self._maintenance_lock = asyncio.Lock()
        self._pin_counts: dict[str, int] = {}

    async def fetch(
        self,
        descriptor: ArtifactTransferDescriptor,
        *,
        pin: bool = False,
    ) -> Path:
        _require_same_origin(descriptor.download_url, self._origin)
        if descriptor.expires_at is not None and descriptor.expires_at <= datetime.now(UTC):
            raise ArtifactTransferFailedError("Artifact download capability has expired.")
        if descriptor.size_bytes > self._maximum_size:
            raise ArtifactTransferFailedError("Artifact exceeds the Agent cache size bound.")
        destination = self._root / descriptor.sha256 / "content"
        lock = self._locks.setdefault(descriptor.sha256, asyncio.Lock())
        if pin:
            self._pin_counts[descriptor.sha256] = self._pin_counts.get(descriptor.sha256, 0) + 1
        async with lock:
            try:
                if destination.is_file():
                    size, digest = await asyncio.to_thread(
                        _hash_file,
                        destination,
                        maximum_size_bytes=self._maximum_size,
                    )
                    if size == descriptor.size_bytes and digest == descriptor.sha256:
                        await asyncio.to_thread(os.utime, destination, None)
                        await self._make_space(0, retaining=descriptor.sha256)
                        return destination
                    destination.unlink(missing_ok=True)
                await self._make_space(
                    descriptor.size_bytes,
                    retaining=descriptor.sha256,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor_fd, temporary_name = tempfile.mkstemp(
                    prefix=".download-",
                    suffix=".part",
                    dir=destination.parent,
                )
                os.close(descriptor_fd)
                temporary = Path(temporary_name)
                temporary.unlink(missing_ok=True)
                try:
                    size, digest = await asyncio.to_thread(
                        self._transport.download,
                        descriptor,
                        temporary,
                        maximum_size_bytes=self._maximum_size,
                    )
                    if size != descriptor.size_bytes or digest != descriptor.sha256:
                        raise ArtifactTransferFailedError(
                            "Downloaded artifact failed size or checksum verification."
                        )
                    temporary.replace(destination)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            except BaseException:
                if pin:
                    self.release(descriptor.sha256)
                raise
            return destination

    def release(self, sha256: str) -> None:
        count = self._pin_counts.get(sha256, 0)
        if count <= 1:
            self._pin_counts.pop(sha256, None)
        else:
            self._pin_counts[sha256] = count - 1

    async def _make_space(self, required_bytes: int, *, retaining: str) -> None:
        async with self._maintenance_lock:
            self._root.mkdir(parents=True, exist_ok=True)
            entries: list[tuple[int, int, str, Path]] = []
            total = 0
            for path in self._root.glob("*/content"):
                if not path.is_file():
                    continue
                stat = path.stat()
                digest = path.parent.name
                total += stat.st_size
                entries.append((stat.st_atime_ns, stat.st_mtime_ns, digest, path))
            target_total = total + required_bytes
            for _atime, _mtime, digest, path in sorted(entries):
                if target_total <= self._maximum_size:
                    break
                if digest == retaining or self._pin_counts.get(digest, 0) > 0:
                    continue
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                with suppress(OSError):
                    path.parent.rmdir()
                target_total -= size
            if target_total > self._maximum_size:
                raise ArtifactTransferFailedError(
                    "Agent artifact cache is full of artifacts used by active operations."
                )


class LabAgentInventoryAdapter:
    def __init__(self, agent: LabAgent, *, boot_id: UUID) -> None:
        self._agent = agent
        self._boot_id = boot_id
        self._sender: AgentConnectionManager | None = None
        self._executor: LocalAgentCommandExecutor | None = None
        self._last_snapshot_signature: tuple[str, ...] | None = None
        self._last_message_id: UUID | None = None
        self._publish_lock = asyncio.Lock()

    def bind(
        self,
        sender: AgentConnectionManager,
        executor: LocalAgentCommandExecutor,
    ) -> None:
        self._sender = sender
        self._executor = executor

    async def snapshot(self) -> Sequence[ReconciliationBenchSnapshot]:
        kinds = {item.id: item.type for item in self._agent.config.effective_backends}
        firmware = {item.id: item.firmware_version for item in self._agent._bench_cache}
        return tuple(
            ReconciliationBenchSnapshot(
                local_bench_id=record.id,
                name=record.name,
                backend_id=record.backend_id,
                kind=(
                    GlobalBenchKind.SIMULATED
                    if kinds.get(record.backend_id) == "simlab"
                    else GlobalBenchKind.PHYSICAL
                ),
                target_type=record.target_type,
                status=_global_status(record),
                health=record.health,
                capabilities=frozenset(record.capabilities),
                labels=record.labels,
                firmware_version=firmware.get(record.id),
            )
            for record in self._agent.catalog.list()
        )

    async def heartbeat_state(self) -> AgentHeartbeatState:
        records = self._agent.catalog.list()
        online = [record for record in records if record.online]
        return AgentHeartbeatState(
            active_operations=(self._executor.active_count if self._executor is not None else 0),
            connected_benches=len(online),
            degraded_benches=sum(
                1 for record in online if record.health is not HealthStatus.HEALTHY
            ),
        )

    async def refresh_inventory(self, _payload: InventoryRefreshRequestPayload) -> None:
        await self._agent.refresh_catalog(publish_distributed=False)
        await self.publish(force=True)

    async def publish(self, *, force: bool = False) -> UUID:
        if self._sender is None:
            raise RuntimeError("Inventory adapter is not bound to a connection manager")
        async with self._publish_lock:
            benches = await self.snapshot()
            signature = tuple(bench.model_dump_json() for bench in benches)
            if (
                not force
                and signature == self._last_snapshot_signature
                and self._last_message_id is not None
            ):
                return self._last_message_id
            kinds = {
                GlobalBenchKind.SIMULATED: BenchKind.SIMULATED,
                GlobalBenchKind.PHYSICAL: BenchKind.PHYSICAL,
            }
            from lab_platform.agent_protocol import AgentBenchSnapshot

            message_id = await self._sender.send(
                MessageType.BENCH_SNAPSHOT,
                BenchSnapshotPayload(
                    boot_id=self._boot_id,
                    generated_at=datetime.now(UTC),
                    benches=tuple(
                        AgentBenchSnapshot(
                            local_bench_id=bench.local_bench_id,
                            name=bench.name,
                            backend_id=bench.backend_id,
                            kind=kinds[bench.kind],
                            target_type=bench.target_type,
                            connectivity=_connectivity(bench.status),
                            health=_bench_health(bench.health),
                            capabilities=bench.capabilities,
                            labels=bench.labels,
                            firmware_version=bench.firmware_version,
                        )
                        for bench in benches
                    ),
                ),
            )
            self._last_snapshot_signature = signature
            self._last_message_id = message_id
            return message_id


class LocalAgentSafetyAdapter(AgentBenchSafetyPort):
    def __init__(
        self,
        agent: LabAgent,
        operation_locks: SQLiteOperationLockRepository,
    ) -> None:
        self._agent = agent
        self._locks = operation_locks

    async def validate(
        self,
        request: Any,
        *,
        required_capability: str | None,
    ) -> None:
        local_id = _local_bench_id(request.command.bench_id)
        record = next(
            (item for item in self._agent.catalog.list() if item.id == local_id),
            None,
        )
        if record is None:
            raise BenchNotFoundError("Remote command refers to an unknown local bench.")
        if not record.online:
            raise BenchOfflineError("Remote command bench is offline.", bench_id=local_id)
        if required_capability is not None and required_capability.casefold() not in {
            item.casefold() for item in record.capabilities
        }:
            raise CapabilityNotSupportedError(
                "Remote command capability is unavailable.",
                bench_id=local_id,
                capability=required_capability,
            )
        lock = await self._locks.get(local_id)
        if (
            lock is not None
            and request.command.command_type is not RemoteCommandType.CANCEL_OPERATION
        ):
            raise BenchOperationInProgressError(
                "A local operation lock already owns the bench.",
                bench_id=local_id,
                operation_id=str(lock.operation_id),
            )
        _validate_command_payload(request.command)


class _LeaseWorkflowAuthorizer:
    def __init__(self, leases: SQLiteReservationLeaseStore) -> None:
        self._leases = leases

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        for stored in await self._leases.list():
            if not isinstance(stored, ReservationLease):
                continue
            if _local_bench_id(stored.bench_id) != bench_id:
                continue
            if stored.owner != owner:
                raise ReservationOwnerMismatchError(
                    "Workflow owner does not match the distributed reservation lease."
                )
            validated = await self._leases.validate(
                agent_id=stored.agent_id,
                reservation_id=stored.reservation_id,
                bench_id=stored.bench_id,
                lease_version=stored.lease_version,
            )
            return validated.reservation_id
        raise ReservationNotActiveError(
            "No active distributed reservation lease owns the workflow bench.",
            bench_id=bench_id,
        )


class _RemoteWorkflowLockAdapter:
    def __init__(self, locks: SQLiteOperationLockRepository) -> None:
        self._locks = locks

    async def acquire(
        self,
        bench_id: str,
        operation_id: UUID,
        _owner: str,
    ) -> BenchOperationLock:
        return await self._locks.acquire(
            BenchOperationLock(
                bench_id=bench_id,
                operation_id=operation_id,
                acquired_at=datetime.now(UTC),
            )
        )

    async def release(self, bench_id: str, operation_id: UUID) -> bool:
        return await self._locks.release(bench_id, operation_id)


class LocalAgentCommandExecutor(AgentCommandExecutor):
    def __init__(
        self,
        agent: LabAgent,
        *,
        locks: SQLiteOperationLockRepository,
        workflows: WorkflowService,
        workflow_runner: WorkflowRunner,
        artifacts: AgentArtifactCache,
        events: SQLiteAgentEventBuffer,
    ) -> None:
        self._agent = agent
        self._locks = locks
        self._workflows = workflows
        self._workflow_runner = workflow_runner
        self._artifacts = artifacts
        self._events = events
        self._active_commands: set[UUID] = set()
        self._workflow_runs: dict[UUID, UUID] = {}

    @property
    def active_count(self) -> int:
        return len(self._active_commands)

    async def execute(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any] | None:
        self._active_commands.add(command.id)
        try:
            if command.command_type is RemoteCommandType.RUN_WORKFLOW:
                return await self._run_workflow(command, report_progress=report_progress)
            if command.command_type is RemoteCommandType.REFRESH_INVENTORY:
                refresh = await self._agent.refresh_catalog()
                return {
                    "bench_count": len(refresh.records),
                    "offline_benches": sorted(refresh.offline_bench_ids),
                }
            if command.command_type is RemoteCommandType.CANCEL_OPERATION:
                target = UUID(_required_text(command.payload, "command_id"))
                await self.cancel(
                    command_id=target,
                    local_operation_id=local_operation_id,
                    reason="Remote cancellation command",
                )
                return {"cancelled_command_id": str(target)}
            return await self._run_direct(
                command,
                local_operation_id=local_operation_id,
                report_progress=report_progress,
            )
        finally:
            self._active_commands.discard(command.id)
            self._workflow_runs.pop(command.id, None)

    async def cancel(
        self,
        *,
        command_id: UUID,
        local_operation_id: UUID,
        reason: str | None,
    ) -> None:
        del local_operation_id, reason
        run_id = self._workflow_runs.get(command_id)
        if run_id is not None:
            run = await self._workflows.get_run(run_id)
            if run.status in {WorkflowRunStatus.PENDING, WorkflowRunStatus.RUNNING}:
                await self._workflows.cancel(run_id, run.owner)

    async def _run_direct(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any]:
        bench_id = _local_bench_id(command.bench_id)
        lock = BenchOperationLock(
            bench_id=bench_id,
            operation_id=local_operation_id,
            acquired_at=datetime.now(UTC),
            expires_at=command.expires_at,
        )
        await self._locks.acquire(lock)
        try:
            backend = await self._agent.backend_registry.get_backend_for_bench(bench_id)
            if command.command_type is RemoteCommandType.PROBE:
                await report_progress(progress=20, message="Probing remote bench")
                try:
                    result = await backend.probe(bench_id)
                except Exception:
                    await self._agent._probe_health_recorder.failure(bench_id)
                    raise
                await self._agent._probe_health_recorder.record(result)
                return result.model_dump(mode="json")
            if command.command_type is RemoteCommandType.RESET:
                await report_progress(progress=20, message="Resetting remote target")
                await backend.reset(bench_id)
                return {"reset": True}
            if command.command_type is RemoteCommandType.FLASH:
                descriptor = ArtifactTransferDescriptor.model_validate(
                    _required_mapping(command.payload, "artifact")
                )
                _require_descriptor_agent(descriptor, command.agent_id)
                path = await self._artifacts.fetch(descriptor, pin=True)
                try:
                    firmware = FirmwareInput(
                        filename=descriptor.name
                        or Path(descriptor.target_path or "artifact.bin").name,
                        local_path=path,
                        sha256=descriptor.sha256,
                        size_bytes=descriptor.size_bytes,
                        version=_optional_text(command.payload, "version"),
                    )
                    final_version: str | None = None
                    async for update in backend.flash_firmware(bench_id, firmware):
                        final_version = update.firmware_version or final_version
                        await report_progress(progress=update.percent, message=update.message)
                    return {"sha256": descriptor.sha256, "firmware_version": final_version}
                finally:
                    self._artifacts.release(descriptor.sha256)
            if command.command_type is RemoteCommandType.READ_SERIAL:
                request = SerialReadRequest.model_validate(command.payload.get("request", {}))
                lines: list[dict[str, Any]] = []
                async for line in backend.read_serial(bench_id, request):
                    if len(lines) < 500:
                        payload = line.model_dump(mode="json")
                        payload["text"] = str(payload["text"])[:2000]
                        lines.append(payload)
                return {"lines": lines, "line_count": len(lines)}
            raise RemoteCommandRejectedError(
                "Agent executor does not support this command type.",
                command_type=command.command_type.value,
            )
        finally:
            await self._locks.release(bench_id, local_operation_id)

    async def _run_workflow(
        self,
        command: RemoteCommand,
        *,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, Any]:
        definition_key = "definition" if "definition" in command.payload else "workflow"
        definition = WorkflowDefinition.model_validate(
            _required_mapping(command.payload, definition_key)
        )
        definition = await self._workflows.register(definition)
        owner = _required_text(command.payload, "owner")
        inputs = dict(_optional_mapping(command.payload, "inputs"))
        artifact_paths: dict[UUID, Path] = {}
        raw_artifacts = command.payload.get(
            "artifact_transfers",
            command.payload.get("artifacts", []),
        )
        if not isinstance(raw_artifacts, list):
            raise InvalidArtifactError("Workflow artifact descriptors must be a list.")
        pinned_digests: list[str] = []
        try:
            for raw_descriptor in raw_artifacts:
                descriptor = ArtifactTransferDescriptor.model_validate(raw_descriptor)
                _require_descriptor_agent(descriptor, command.agent_id)
                artifact_paths[descriptor.artifact_id] = await self._artifacts.fetch(
                    descriptor,
                    pin=True,
                )
                pinned_digests.append(descriptor.sha256)

            def resolve(reference: Any) -> Path:
                path = artifact_paths.get(reference.artifact_id)
                if path is None:
                    raise InvalidArtifactError("Workflow input artifact was not transferred.")
                return path

            run = await self._workflows.start(
                definition.name,
                version=definition.version,
                bench_id=_local_bench_id(command.bench_id),
                owner=owner,
                inputs=inputs,
                artifact_resolver=resolve,
            )
            self._workflow_runs[command.id] = run.id
            await report_progress(progress=1, message="Remote workflow started")
            wait_task = asyncio.create_task(self._workflows.wait(run.id))
            last_step: int | None = None
            while not wait_task.done():
                await asyncio.sleep(0.1)
                current = await self._workflows.get_run(run.id)
                if current.current_step is not None and current.current_step != last_step:
                    last_step = current.current_step
                    await report_progress(
                        progress=min(
                            99,
                            int((current.current_step + 1) * 100 / len(definition.steps)),
                        ),
                        message=(
                            f"Workflow step {current.current_step + 1}/{len(definition.steps)}"
                        ),
                    )
            completed = await wait_task
            steps = await self._workflows.list_step_results(run.id)
            await self._publish_workflow_artifacts(command, steps)
            if completed.status is not WorkflowRunStatus.SUCCEEDED:
                raise RemoteCommandRejectedError(
                    completed.error_message or "Remote workflow failed.",
                    workflow_run_id=str(completed.id),
                    workflow_status=completed.status.value,
                    workflow_error_code=completed.error_code,
                )
            return {
                "workflow_run": completed.model_dump(mode="json"),
                "steps": [step.model_dump(mode="json") for step in steps],
            }
        finally:
            for digest in pinned_digests:
                self._artifacts.release(digest)

    async def _publish_workflow_artifacts(
        self,
        command: RemoteCommand,
        steps: Sequence[Any],
    ) -> None:
        artifact_ids = {artifact_id for step in steps for artifact_id in step.artifact_ids}
        for artifact_id in sorted(artifact_ids, key=str):
            record = await self._agent.artifact_service.get(artifact_id)
            metadata = RemoteArtifactMetadata(
                id=record.id,
                agent_id=command.agent_id,
                local_artifact_id=record.id,
                command_id=command.id,
                operation_id=command.operation_id,
                name=record.name,
                artifact_type=record.artifact_type,
                content_type=record.content_type,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
                created_at=record.created_at,
            )
            await self._events.append(
                MessageType.ARTIFACT_CREATED.value,
                ArtifactCreatedPayload(artifact=metadata).model_dump(mode="json"),
                priority=BufferedEventPriority.TERMINAL,
                event_id=metadata.id,
            )


class _DrainAdapter:
    def __init__(self, handler: AgentCommandHandler) -> None:
        self._handler = handler

    async def apply_drain(self, payload: Any) -> None:
        self._handler.set_draining(bool(payload.drain))


class _AuxiliaryAdapter:
    def __init__(
        self,
        agent: LabAgent,
        *,
        control_plane_url: str,
        transport: ArtifactHttpTransport,
        maximum_size_bytes: int,
    ) -> None:
        self._agent = agent
        self._origin = _http_origin(control_plane_url)
        self._transport = transport
        self._maximum_size = maximum_size_bytes

    async def handle_auxiliary_message(self, envelope: SupportedEnvelope) -> None:
        if isinstance(envelope, ConfigRefreshRequestEnvelope):
            return
        if not isinstance(envelope, ArtifactUploadRequestEnvelope):
            raise RuntimeError("Unsupported auxiliary Agent message")
        payload = envelope.payload
        _require_same_origin(payload.upload_url, self._origin)
        if payload.expires_at <= datetime.now(UTC):
            raise ArtifactTransferFailedError("Artifact upload capability has expired.")
        if payload.maximum_size_bytes > self._maximum_size:
            raise ArtifactTransferFailedError(
                "Control-plane upload request exceeds the Agent artifact bound."
            )
        path = await self._agent.artifact_service.content_path(
            payload.local_artifact_id or payload.artifact_id
        )
        await asyncio.to_thread(
            self._transport.upload,
            payload.upload_url,
            payload.transfer_token.get_secret_value(),
            path,
            expected_sha256=payload.expected_sha256,
            maximum_size_bytes=min(payload.maximum_size_bytes, self._maximum_size),
        )


class AgentDistributedRuntime:
    """Agent-side Phase 5 composition, activated only by explicit configuration."""

    def __init__(
        self,
        agent: LabAgent,
        *,
        database: SQLiteDatabase,
        workflow_repository: SQLiteWorkflowRepository,
        operation_locks: SQLiteOperationLockRepository,
        events: SQLiteEventRepository,
        storage_root: Path,
        transport: ArtifactHttpTransport | None = None,
    ) -> None:
        config = agent.config
        if not config.control_plane.enabled:
            raise ConfigurationError("Distributed Agent runtime is not enabled.")
        if config.identity.agent_id is None or config.control_plane.url is None:
            raise ConfigurationError("Distributed Agent identity or control-plane URL is missing.")
        credential = os.environ.get(config.identity.credential_env_var)
        if credential is None or not credential.strip():
            raise ConfigurationError(
                "Agent credential environment variable is not set.",
                credential_env_var=config.identity.credential_env_var,
            )
        self.agent_id = config.identity.agent_id
        self.boot_id = uuid4()
        self._agent = agent
        self._database = database
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._transport = transport or StreamingArtifactHttpTransport()

        leases = SQLiteReservationLeaseStore(database, self.agent_id)
        event_buffer = SQLiteAgentEventBuffer(database, self.agent_id)
        journal = SQLiteAgentCommandJournal(database)
        inventory = LabAgentInventoryAdapter(agent, boot_id=self.boot_id)
        authorizer = _LeaseWorkflowAuthorizer(leases)
        workflow_locks = _RemoteWorkflowLockAdapter(operation_locks)
        remote_runner = WorkflowRunner(
            workflow_repository,
            agent.backend_registry,
            authorizer,
            events,
            workflow_locks,
            clock=agent._clock.now,
            step_timeout_seconds=config.ci.step_timeout_seconds,
            artifact_service=agent.artifact_service,
            serial_buffer_lines=config.serial.stream_buffer_lines,
            serial_artifact_max_bytes=config.serial.artifact_max_size_mb * 1024 * 1024,
            serial_redact_patterns=config.serial.redact_patterns,
        )
        remote_workflows = WorkflowService(
            workflow_repository,
            agent.backend_registry,
            authorizer,
            remote_runner,
            events,
            workflow_locks,
            clock=agent._clock.now,
        )
        data_root = _relative_to_storage(config.agent.data_directory, storage_root)
        artifact_limit = config.artifacts.max_upload_size_mb * 1024 * 1024
        cache = AgentArtifactCache(
            data_root / "artifacts",
            config.control_plane.url,
            maximum_size_bytes=artifact_limit,
            transport=self._transport,
        )
        executor = LocalAgentCommandExecutor(
            agent,
            locks=operation_locks,
            workflows=remote_workflows,
            workflow_runner=remote_runner,
            artifacts=cache,
            events=event_buffer,
        )
        safety = LocalAgentSafetyAdapter(agent, operation_locks)
        handler = AgentCommandHandler(
            agent_id=self.agent_id,
            journal=journal,
            leases=leases,
            events=event_buffer,
            safety=safety,
            executor=executor,
            maximum_clock_skew_seconds=config.control_plane.maximum_clock_skew_seconds,
        )
        reconciliation = AgentReconciliationReportBuilder(
            agent_id=self.agent_id,
            boot_id=self.boot_id,
            journal=journal,
            leases=leases,
            inventory=inventory,
            events=event_buffer,
        )
        auxiliary = _AuxiliaryAdapter(
            agent,
            control_plane_url=config.control_plane.url,
            transport=self._transport,
            maximum_size_bytes=artifact_limit,
        )
        reconnect = config.control_plane.reconnect
        self.manager = AgentConnectionManager(
            agent_id=self.agent_id,
            boot_id=self.boot_id,
            agent_name=config.agent.name,
            agent_version=VERSION,
            gateway_url=config.control_plane.url,
            credential=credential,
            events=event_buffer,
            leases=leases,
            commands=handler,
            drain=_DrainAdapter(handler),
            reconciliation=reconciliation,
            heartbeat=inventory,
            inventory_refresh=inventory,
            auxiliary_messages=auxiliary,
            capabilities=frozenset({"remote_operations", "artifact_upload", "workflow_execution"}),
            maximum_clock_skew_seconds=config.control_plane.maximum_clock_skew_seconds,
            outgoing_queue_size=config.control_plane.outgoing_queue_size,
            event_batch_size=config.control_plane.event_batch_size,
            event_ack_timeout_seconds=config.control_plane.event_ack_timeout_seconds,
            maximum_message_size_bytes=config.control_plane.maximum_message_size_mb * 1024 * 1024,
            reconnect_initial_delay_seconds=reconnect.initial_delay_seconds,
            reconnect_maximum_delay_seconds=reconnect.maximum_delay_seconds,
            reconnect_jitter_ratio=0.2 if reconnect.jitter else 0.0,
            reconnect_stability_seconds=reconnect.stability_seconds,
            allow_insecure_loopback=config.control_plane.allow_insecure_loopback,
        )
        inventory.bind(self.manager, executor)
        self._inventory = inventory
        self._executor = executor
        self._remote_runner = remote_runner
        self._handler = handler
        self._events = event_buffer
        self._leases = leases
        self._journal = journal

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        await self._journal.recover_interrupted(
            boot_id=self.boot_id,
            recovered_at=self._agent._clock.now(),
        )
        await self._inventory.publish()
        self._task = asyncio.create_task(
            self.manager.run(),
            name=f"agent-control-plane-{self.agent_id}",
        )
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self.manager.stop()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.manager.wait_for_command_tasks()
        await self._remote_runner.shutdown()
        self._started = False

    async def reconnect(self) -> None:
        await self.manager.request_reconnect()

    async def publish_inventory(self, *, force: bool = False) -> UUID:
        """Publish a changed local inventory snapshot to the control plane."""

        return await self._inventory.publish(force=force)

    async def journal_entries(
        self,
        *,
        status: RemoteCommandStatus | None = None,
        limit: int = 100,
    ) -> list[CommandJournalEntry]:
        records = await self._journal.list(status=status, limit=limit)
        return [record.entry for record in records]

    async def status(self) -> dict[str, object]:
        stats = await self._events.stats()
        heartbeat = await self._inventory.heartbeat_state()
        return {
            "enabled": True,
            "agent_id": str(self.agent_id),
            "boot_id": str(self.boot_id),
            "connected": self.manager.connected,
            "endpoint": self.manager.endpoint,
            "queued_messages": self.manager.queued_messages,
            "connection_status": 1 if self.manager.connected else 0,
            "reconnect_attempts": self.manager.reconnect_attempts,
            "command_queue_size": self.manager.queued_messages,
            "event_buffer_size": stats.buffered_events,
            "active_operations": self._executor.active_count,
            "local_benches_online": (heartbeat.connected_benches - heartbeat.degraded_benches),
            "local_benches_degraded": heartbeat.degraded_benches,
            "lease_count": len(await self._leases.list()),
            "journal_entries": len(await self._journal.list(limit=10_000)),
        }


def _global_status(record: BenchRecord) -> GlobalBenchStatus:
    if not record.online:
        return GlobalBenchStatus.OFFLINE
    if record.health is not HealthStatus.HEALTHY:
        return GlobalBenchStatus.DEGRADED
    return GlobalBenchStatus.ONLINE


def _connectivity(status: GlobalBenchStatus) -> BenchConnectivity:
    return {
        GlobalBenchStatus.ONLINE: BenchConnectivity.ONLINE,
        GlobalBenchStatus.DEGRADED: BenchConnectivity.DEGRADED,
        GlobalBenchStatus.OFFLINE: BenchConnectivity.OFFLINE,
    }[status]


def _bench_health(status: HealthStatus) -> BenchHealth:
    return {
        HealthStatus.HEALTHY: BenchHealth.HEALTHY,
        HealthStatus.WARNING: BenchHealth.WARNING,
        HealthStatus.UNHEALTHY: BenchHealth.UNHEALTHY,
    }[status]


def _local_bench_id(global_bench_id: str) -> str:
    prefix, separator, local_id = global_bench_id.partition("/")
    if not separator or not prefix or not local_id or "/" in local_id:
        raise BenchNotFoundError(
            "Distributed commands require a canonical <agent-slug>/<local-id> bench ID.",
            bench_id=global_bench_id,
        )
    return local_id


def _validate_command_payload(command: RemoteCommand) -> None:
    if command.command_type is RemoteCommandType.FLASH:
        ArtifactTransferDescriptor.model_validate(_required_mapping(command.payload, "artifact"))
    elif command.command_type is RemoteCommandType.READ_SERIAL:
        SerialReadRequest.model_validate(command.payload.get("request", {}))
    elif command.command_type is RemoteCommandType.RUN_WORKFLOW:
        definition_key = "definition" if "definition" in command.payload else "workflow"
        WorkflowDefinition.model_validate(_required_mapping(command.payload, definition_key))
        _required_text(command.payload, "owner")
        _optional_mapping(command.payload, "inputs")
    elif command.command_type is RemoteCommandType.CANCEL_OPERATION:
        UUID(_required_text(command.payload, "command_id"))


def _require_descriptor_agent(
    descriptor: ArtifactTransferDescriptor,
    agent_id: UUID,
) -> None:
    if descriptor.agent_id is not None and descriptor.agent_id != agent_id:
        raise InvalidArtifactError("Artifact download capability is scoped to a different Agent.")


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise RemoteCommandRejectedError(f"Remote command field {key!r} must be an object.")
    return cast(Mapping[str, Any], value)


def _optional_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, Mapping):
        raise RemoteCommandRejectedError(f"Remote command field {key!r} must be an object.")
    return cast(Mapping[str, Any], value)


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RemoteCommandRejectedError(f"Remote command field {key!r} must be text.")
    return value.strip()


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RemoteCommandRejectedError(f"Remote command field {key!r} must be text.")
    return value.strip()


def _http_origin(control_plane_url: str) -> tuple[str, str, int | None]:
    endpoint = urlsplit(control_plane_url)
    scheme = {"wss": "https", "ws": "http"}.get(endpoint.scheme, endpoint.scheme)
    if scheme not in {"http", "https"} or endpoint.hostname is None:
        raise ConfigurationError("Control-plane URL cannot define an artifact HTTP origin.")
    return scheme, endpoint.hostname.casefold(), endpoint.port


def _require_same_origin(url: str, expected: tuple[str, str, int | None]) -> SplitResult:
    endpoint = urlsplit(url)
    received = (endpoint.scheme, (endpoint.hostname or "").casefold(), endpoint.port)
    if received != expected or endpoint.username is not None or endpoint.password is not None:
        raise ArtifactTransferFailedError(
            "Artifact transfer URL does not belong to the configured control plane."
        )
    return endpoint


def _hash_file(path: Path, *, maximum_size_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > maximum_size_bytes:
                raise ArtifactTransferFailedError("Artifact file exceeds its size bound.")
            digest.update(chunk)
    return size, digest.hexdigest()


def _relative_to_storage(path: Path, storage_root: Path) -> Path:
    return path if path.is_absolute() else storage_root / path
