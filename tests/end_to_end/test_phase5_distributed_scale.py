from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from lab_platform.agent_protocol import (
    PROTOCOL_VERSION,
    AgentBenchSnapshot,
    AgentStatusPayload,
    BenchConnectivity,
    BenchHealth,
    BenchKind,
    BenchSnapshotPayload,
    CommandCancelPayload,
    CommandRequestPayload,
    DrainAgentPayload,
    MessageType,
    WelcomePayload,
    parse_control_plane_message,
)
from lab_platform.agent_runtime import (
    AgentCommandHandler,
    AgentConnectionManager,
    AgentOutgoingQueueFullError,
    AgentReconciliationReportBuilder,
    CommandProgressReporter,
    SQLiteAgentCommandJournal,
    SQLiteAgentEventBuffer,
    SQLiteReservationLeaseStore,
)
from lab_platform.agent_runtime.connection_manager import AgentWebSocket, WebSocketConnector
from lab_platform.control_plane_core import AgentEnrollmentService
from lab_platform.control_plane_core.commands import (
    CommandDeliveryReceipt,
    RemoteCommandService,
)
from lab_platform.control_plane_core.distributed_ci import (
    DistributedCiCreateRequest,
    DistributedCiSessionService,
)
from lab_platform.control_plane_core.inventory import InventoryService
from lab_platform.control_plane_core.presence import AgentPresenceService
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    LeaseApplicationReceipt,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowCoordinator,
    DistributedWorkflowRequest,
    WorkflowArtifactTransferDescriptor,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    BenchRequest,
    BufferedEventPriority,
    CiProvider,
    CiSessionStatus,
    GlobalBenchRecord,
    GlobalBenchStatus,
    ReconciliationBenchSnapshot,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
    WorkflowDefinition,
)
from lab_platform.persistence import (
    SQLiteAgentEnrollmentRepository,
    SQLiteCentralReservationLeaseRepository,
    SQLiteCiSessionRepository,
    SQLiteDatabase,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.distributed import (
    SQLiteDistributedOperationRepository,
    SQLiteGlobalBenchRepository,
    SQLiteRemoteArtifactRepository,
)
from lab_platform.persistence.distributed_adapters import (
    SQLiteAgentPresenceAdapter,
    SQLiteDistributedDirectory,
    SQLiteInventoryAdapter,
    SQLiteRemoteCommandServiceRepository,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
CONNECTED_AT = NOW + timedelta(seconds=1)
DISCONNECTED_AT = NOW + timedelta(seconds=2)
RECONNECTED_AT = NOW + timedelta(seconds=3)
ROUTED_AT = NOW + timedelta(seconds=4)


class SequentialUUIDs:
    def __init__(self, first: int = 1) -> None:
        self._next = first

    def __call__(self) -> UUID:
        identifier = UUID(int=self._next)
        self._next += 1
        return identifier


class SecretFactory:
    def __init__(self, prefix: str, fill: str) -> None:
        self._prefix = prefix
        self._fill = fill
        self._next = 1

    def __call__(self) -> str:
        value = f"{self._prefix}{self._next:08d}{self._fill * 40}"
        self._next += 1
        return value


@dataclass(frozen=True, slots=True)
class ScaleStack:
    database: SQLiteDatabase
    agents: tuple[AgentRecord, ...]
    boot_ids: Mapping[UUID, UUID]
    connection_ids: Mapping[UUID, UUID]
    snapshots: Mapping[UUID, BenchSnapshotPayload]
    presence_repository: SQLiteAgentPresenceAdapter
    presence: AgentPresenceService
    inventory_repository: SQLiteInventoryAdapter
    inventory: InventoryService


class ConfirmingLeaseSynchronizer:
    def __init__(self) -> None:
        self.applied: list[tuple[UUID, str, int]] = []
        self.released: list[tuple[UUID, str, int]] = []

    async def apply_lease(self, lease: ReservationLease) -> LeaseApplicationReceipt:
        self.applied.append((lease.reservation_id, lease.bench_id, lease.lease_version))
        return LeaseApplicationReceipt(
            reservation_id=lease.reservation_id,
            agent_id=lease.agent_id,
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            confirmed_at=ROUTED_AT,
        )

    async def release_lease(self, lease: ReservationLease) -> None:
        self.released.append((lease.reservation_id, lease.bench_id, lease.lease_version))


class RecordingCommandTransport:
    def __init__(self, connection_ids: Mapping[UUID, UUID]) -> None:
        self._connection_ids = dict(connection_ids)
        self._sequences: Counter[UUID] = Counter()
        self._lock = asyncio.Lock()
        self.sent: list[tuple[UUID, CommandRequestPayload, UUID]] = []

    async def is_connected(self, agent_id: UUID) -> bool:
        return agent_id in self._connection_ids

    async def send_command(
        self,
        agent_id: UUID,
        payload: CommandRequestPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        async with self._lock:
            self._sequences[agent_id] += 1
            sequence = self._sequences[agent_id]
            self.sent.append((agent_id, payload, correlation_id))
        return CommandDeliveryReceipt(
            connection_id=self._connection_ids[agent_id],
            sequence_number=sequence,
            dispatched_at=ROUTED_AT,
        )

    async def send_cancel(
        self,
        agent_id: UUID,
        payload: CommandCancelPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt:
        del payload, correlation_id
        async with self._lock:
            self._sequences[agent_id] += 1
            sequence = self._sequences[agent_id]
        return CommandDeliveryReceipt(
            connection_id=self._connection_ids[agent_id],
            sequence_number=sequence,
            dispatched_at=ROUTED_AT,
        )


class NoArtifactTransfers:
    async def issue_download(
        self,
        *,
        agent_id: UUID,
        input_name: str,
        artifact_id: UUID,
        target_path: str,
        idempotency_key: str,
    ) -> WorkflowArtifactTransferDescriptor:
        del agent_id, input_name, artifact_id, target_path, idempotency_key
        raise AssertionError("The scale workflow has no artifact inputs")


class NoArtifactUploads:
    async def request_artifact_upload(self, agent_id: UUID, artifact_id: UUID) -> object:
        del agent_id, artifact_id
        return None


async def _build_scale_stack(path: Path) -> ScaleStack:
    database = SQLiteDatabase(path)
    database.initialize()
    enrollment_repository = SQLiteAgentEnrollmentRepository(database)
    enrollment = AgentEnrollmentService(
        enrollment_repository,
        clock=lambda: NOW,
        token_factory=SecretFactory("lpe_", "e"),
        credential_factory=SecretFactory("lpa_", "a"),
        id_factory=SequentialUUIDs(),
    )
    inventory_repository = SQLiteInventoryAdapter(database)
    inventory = InventoryService(inventory_repository)
    presence_repository = SQLiteAgentPresenceAdapter(database)
    presence = AgentPresenceService(
        presence_repository,
        offline_inventory=inventory,
    )
    boot_ids: dict[UUID, UUID] = {}
    connection_ids: dict[UUID, UUID] = {}
    snapshots: dict[UUID, BenchSnapshotPayload] = {}
    agents: list[AgentRecord] = []

    for agent_index in range(10):
        issued = await enrollment.issue_token(
            name=f"sim-agent-{agent_index:02d}",
            expires_at=NOW + timedelta(hours=1),
            allowed_labels={"pool": "scale", "shard": str(agent_index)},
        )
        enrolled = await enrollment.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=100_000 + agent_index),
            agent_version="0.6.0-alpha",
            protocol_version=PROTOCOL_VERSION,
            location=f"sim-location-{agent_index % 2}",
        )
        authenticated = await enrollment.authenticate(
            enrolled.agent.id,
            enrolled.plaintext.get_secret_value(),
        )
        assert authenticated.agent.id == enrolled.agent.id
        boot_id = UUID(int=200_000 + agent_index)
        connection_id = UUID(int=300_000 + agent_index)
        registration = await presence.register_authenticated_connection(
            authenticated.agent,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version=PROTOCOL_VERSION,
            sequence_number=1,
            observed_at=CONNECTED_AT,
            observed_monotonic=1,
        )
        snapshot = _snapshot(boot_id, agent_index, generated_at=CONNECTED_AT)
        reconciled = await inventory.reconcile_snapshot(
            registration.agent,
            snapshot,
            observed_at=CONNECTED_AT,
            expected_boot_id=boot_id,
        )
        assert len(reconciled.added_ids) == 100
        agents.append(registration.agent)
        boot_ids[registration.agent.id] = boot_id
        connection_ids[registration.agent.id] = connection_id
        snapshots[registration.agent.id] = snapshot

    return ScaleStack(
        database=database,
        agents=tuple(agents),
        boot_ids=boot_ids,
        connection_ids=connection_ids,
        snapshots=snapshots,
        presence_repository=presence_repository,
        presence=presence,
        inventory_repository=inventory_repository,
        inventory=inventory,
    )


def _snapshot(
    boot_id: UUID,
    agent_index: int,
    *,
    generated_at: datetime,
) -> BenchSnapshotPayload:
    return BenchSnapshotPayload(
        boot_id=boot_id,
        generated_at=generated_at,
        benches=tuple(
            AgentBenchSnapshot(
                local_bench_id=f"sim-{bench_index:03d}",
                name=f"SimLab {agent_index:02d}/{bench_index:03d}",
                backend_id="simlab",
                kind=BenchKind.SIMULATED,
                target_type="esp32",
                connectivity=BenchConnectivity.ONLINE,
                health=BenchHealth.HEALTHY,
                capabilities=frozenset({"probe", "reset", "serial", "firmware"}),
                labels={
                    "board": "esp32",
                    "pool": "scale",
                    "agent-index": str(agent_index),
                },
                firmware_version="0.6.0-scale",
            )
            for bench_index in range(100)
        ),
    )


def _scale_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "simlab-scale-reset",
            "version": 2,
            "requirements": {
                "capabilities": ["reset"],
                "labels": {"board": "esp32", "pool": "scale"},
            },
            "steps": [{"action": "reset"}],
        }
    )


def test_simlab_scale_inventory_reconnect_routes_and_queued_ci_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = await _build_scale_stack(tmp_path / "phase5-scale.db")
        try:
            benches = await stack.inventory.list_benches()
            assert len(stack.agents) == 10
            assert len(benches) == 1_000
            assert len({bench.id for bench in benches}) == 1_000
            assert Counter(bench.agent_id for bench in benches) == {
                agent.id: 100 for agent in stack.agents
            }

            # A second authenticated identity cannot seize an existing global bench ID,
            # even when bypassing the protocol's Agent-slug construction rule.
            original = benches[0]
            other = next(agent for agent in stack.agents if agent.id != original.agent_id)
            collision = GlobalBenchRecord(
                id=original.id,
                agent_id=other.id,
                agent_slug=original.agent_slug,
                local_bench_id=original.local_bench_id,
                name="malicious collision",
                backend_id="simlab",
                kind=original.kind,
                status=GlobalBenchStatus.ONLINE,
                health=original.health,
                capabilities=original.capabilities,
                labels=original.labels,
                last_seen_at=RECONNECTED_AT,
                created_at=original.created_at,
                updated_at=RECONNECTED_AT,
            )
            with pytest.raises(ValueError, match="bound to another"):
                await SQLiteGlobalBenchRepository(stack.database).upsert(collision)
            assert (await stack.inventory.get_bench(original.id)).agent_id == original.agent_id

            # Simulate a network partition of the entire ten-Agent fleet. Last-known
            # inventory remains present but unavailable, then identical same-boot snapshots
            # restore it without duplicating any global identities.
            for agent in stack.agents:
                disconnected = await stack.presence.disconnect(
                    agent.id,
                    connection_id=stack.connection_ids[agent.id],
                    boot_id=stack.boot_ids[agent.id],
                    observed_at=DISCONNECTED_AT,
                )
                assert disconnected
            offline = await stack.inventory.list_benches(status=GlobalBenchStatus.OFFLINE)
            assert len(offline) == 1_000

            restored_agents: list[AgentRecord] = []
            restored_connection_ids: dict[UUID, UUID] = {}
            for agent_index, prior in enumerate(stack.agents):
                canonical = await stack.presence.get_agent(prior.id)
                connection_id = UUID(int=400_000 + agent_index)
                restored = await stack.presence.register_authenticated_connection(
                    canonical,
                    connection_id=connection_id,
                    boot_id=stack.boot_ids[prior.id],
                    protocol_version=PROTOCOL_VERSION,
                    sequence_number=1,
                    observed_at=RECONNECTED_AT,
                    observed_monotonic=3,
                )
                snapshot = stack.snapshots[prior.id].model_copy(
                    update={"generated_at": RECONNECTED_AT}
                )
                await stack.inventory.reconcile_snapshot(
                    restored.agent,
                    snapshot,
                    observed_at=RECONNECTED_AT,
                    expected_boot_id=stack.boot_ids[prior.id],
                )
                restored_agents.append(restored.agent)
                restored_connection_ids[restored.agent.id] = connection_id
            benches = await stack.inventory.list_benches()
            assert len(benches) == 1_000
            assert all(bench.status is GlobalBenchStatus.ONLINE for bench in benches)

            directory = SQLiteDistributedDirectory(
                stack.presence_repository,
                stack.inventory_repository,
            )
            synchronizer = ConfirmingLeaseSynchronizer()
            reservation_repository = SQLiteCentralReservationLeaseRepository(stack.database)
            reservations = CentralReservationLeaseService(
                reservation_repository,
                directory,
                synchronizer,
                clock=lambda: ROUTED_AT,
                id_factory=SequentialUUIDs(1_000_000),
            )
            transport = RecordingCommandTransport(restored_connection_ids)
            command_repository = SQLiteRemoteCommandServiceRepository(stack.database)
            commands = RemoteCommandService(
                command_repository,
                directory,
                transport,
                clock=lambda: ROUTED_AT,
            )
            coordinator = DistributedWorkflowCoordinator(
                stack.inventory,
                stack.presence,
                reservations,
                NoArtifactTransfers(),
                commands,
                clock=lambda: ROUTED_AT,
            )
            workflow = _scale_workflow()
            workflow_repository = SQLiteWorkflowRepository(
                stack.database,
                initialize_schema=False,
            )
            await workflow_repository.save_definition(workflow)
            ci_repository = SQLiteCiSessionRepository(stack.database)
            ci_sessions = DistributedCiSessionService(
                ci_repository,
                workflow_repository,
                coordinator,
                command_repository,
                reservations,
                commands,
                SQLiteRemoteArtifactRepository(stack.database),
                NoArtifactUploads(),
                clock=lambda: ROUTED_AT,
                maintenance_batch_size=600,
            )

            # Exercise the Phase 5 development-scale CI queue through the real session
            # service and SQLite repository. Every request is Agent-agnostic and remains
            # queued until a CI runner explicitly starts it.
            ci_requests = tuple(
                DistributedCiCreateRequest(
                    provider=CiProvider.GITHUB_ACTIONS,
                    external_run_id=f"scale-ci-{index:03d}",
                    requested_by="ci/scale",
                    bench_request=BenchRequest(
                        required_capabilities={"reset"},
                        required_labels={"board": "esp32", "pool": "scale"},
                        required_agent_labels={"pool": "scale"},
                    ),
                    idempotency_key=f"scale-ci-{index:03d}",
                    timeout_seconds=3_600,
                )
                for index in range(500)
            )
            queued_ci = await asyncio.gather(
                *(ci_sessions.create(request) for request in ci_requests)
            )
            assert len(queued_ci) == 500
            assert len({session.id for session in queued_ci}) == 500
            assert all(
                session.status is CiSessionStatus.WAITING_FOR_BENCH
                and session.bench_id is None
                and session.reservation_id is None
                for session in queued_ci
            )
            assert (
                len(
                    await ci_sessions.list(
                        status=CiSessionStatus.WAITING_FOR_BENCH,
                        provider=CiProvider.GITHUB_ACTIONS,
                        limit=600,
                    )
                )
                == 500
            )

            # Retried CI webhooks cannot inflate the queue.
            replayed_ci = await asyncio.gather(
                *(ci_sessions.create(request) for request in ci_requests)
            )
            assert [session.id for session in replayed_ci] == [session.id for session in queued_ci]
            assert (
                len(
                    await ci_repository.list(
                        status=CiSessionStatus.WAITING_FOR_BENCH,
                        limit=600,
                    )
                )
                == 500
            )
            maintenance = await ci_sessions.recover_incomplete()
            assert maintenance.examined == 500
            assert maintenance.synchronized == 0
            assert maintenance.timed_out == 0
            assert maintenance.finalized == 0
            assert synchronizer.applied == []
            assert transport.sent == []

            by_agent: dict[UUID, list[GlobalBenchRecord]] = {
                agent.id: [] for agent in restored_agents
            }
            for bench in benches:
                by_agent[bench.agent_id].append(bench)
            routed_benches = tuple(
                bench
                for agent in restored_agents
                for bench in sorted(by_agent[agent.id], key=lambda item: item.id)[:10]
            )
            requests = tuple(
                DistributedWorkflowRequest(
                    definition=workflow,
                    owner=f"ci/scale/{index:03d}",
                    idempotency_key=f"scale-route-{index:03d}",
                    bench_id=bench.id,
                )
                for index, bench in enumerate(routed_benches)
            )

            dispatched = await asyncio.gather(*(coordinator.run(request) for request in requests))

            assert len(dispatched) == 100
            assert [result.bench.id for result in dispatched] == [
                bench.id for bench in routed_benches
            ]
            assert all(
                result.agent.id == result.bench.agent_id
                and result.command.agent_id == result.bench.agent_id
                and result.command.status is RemoteCommandStatus.DISPATCHED
                for result in dispatched
            )
            assert Counter(result.agent.id for result in dispatched) == {
                agent.id: 10 for agent in restored_agents
            }
            assert len({result.reservation.reservation.id for result in dispatched}) == 100
            assert len({result.command.id for result in dispatched}) == 100
            assert len({result.operation.id for result in dispatched}) == 100
            assert len(synchronizer.applied) == 100
            assert len(transport.sent) == 100

            # With no Agent identifier in the request, selection remains stable across the
            # remaining 900 benches. It ranks the next globally stable bench ID.
            used = {result.bench.id for result in dispatched}
            expected_next = min(bench.id for bench in benches if bench.id not in used)
            automatic_request = DistributedWorkflowRequest(
                definition=workflow,
                owner="ci/automatic",
                idempotency_key="scale-route-automatic",
            )
            automatic = await coordinator.run(automatic_request)
            assert automatic.bench.id == expected_next
            assert automatic.agent.id == automatic.bench.agent_id

            replayed = await asyncio.gather(*(coordinator.run(request) for request in requests))
            assert [result.command.id for result in replayed] == [
                result.command.id for result in dispatched
            ]
            assert len(transport.sent) == 101
            assert len(await reservation_repository.list()) == 101
            assert len(await command_repository.list_commands(limit=500)) == 101
            assert (
                len(await SQLiteDistributedOperationRepository(stack.database).list(limit=500))
                == 101
            )
            assert (
                len(
                    await ci_repository.list(
                        status=CiSessionStatus.WAITING_FOR_BENCH,
                        limit=600,
                    )
                )
                == 500
            )
        finally:
            stack.database.close()

    asyncio.run(scenario())


class EmptyInventory:
    async def snapshot(self) -> Sequence[ReconciliationBenchSnapshot]:
        return ()


class AllowingSafety:
    def __init__(self) -> None:
        self.validations = 0

    async def validate(
        self,
        request: CommandRequestPayload,
        *,
        required_capability: str | None,
    ) -> None:
        del request, required_capability
        self.validations += 1


class GatedExecutor:
    def __init__(self) -> None:
        self.executions = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = asyncio.Event()

    async def execute(
        self,
        command: RemoteCommand,
        *,
        local_operation_id: UUID,
        report_progress: CommandProgressReporter,
    ) -> Mapping[str, object]:
        del local_operation_id
        self.executions += 1
        self.started.set()
        await self.release.wait()
        await report_progress(progress=50, message="reconnected")
        self.completed.set()
        return {"command_id": str(command.id), "executions": self.executions}

    async def cancel(
        self,
        *,
        command_id: UUID,
        local_operation_id: UUID,
        reason: str | None,
    ) -> None:
        del command_id, local_operation_id, reason


class NoopDrain:
    async def apply_drain(self, payload: DrainAgentPayload) -> None:
        del payload


class SocketDisconnected(ConnectionError):
    pass


class ScriptedSocket:
    def __init__(
        self,
        agent_id: UUID,
        request: CommandRequestPayload,
        *,
        disconnect_after_command: bool,
    ) -> None:
        self._agent_id = agent_id
        self._request = request
        self._disconnect_after_command = disconnect_after_command
        self._incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.waiting_after_script = asyncio.Event()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        document = cast(dict[str, Any], json.loads(message))
        self.sent.append(document)
        if document["message_type"] != MessageType.AGENT_HELLO.value:
            return
        hello_id = UUID(cast(str, document["message_id"]))
        self._incoming.put_nowait(
            _control_message(
                self._agent_id,
                MessageType.WELCOME,
                WelcomePayload(
                    connection_id=UUID(int=700_000 + len(self.sent)),
                    accepted_protocol_version=PROTOCOL_VERSION,
                    server_time=ROUTED_AT,
                    heartbeat_interval_seconds=60,
                    heartbeat_timeout_seconds=120,
                    offline_timeout_seconds=180,
                ),
                sequence_number=1,
                correlation_id=hello_id,
            )
        )
        self._incoming.put_nowait(
            _control_message(
                self._agent_id,
                MessageType.COMMAND_REQUEST,
                self._request,
                sequence_number=2,
                correlation_id=self._request.command.id,
            )
        )
        if self._disconnect_after_command:
            self._incoming.put_nowait(SocketDisconnected("injected network partition"))

    async def recv(self) -> str | bytes:
        if self._incoming.empty():
            self.waiting_after_script.set()
        value = await self._incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._incoming.put_nowait(SocketDisconnected("socket closed"))


class ScriptedConnector:
    def __init__(self, sockets: Sequence[ScriptedSocket]) -> None:
        self._sockets = list(sockets)

    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket:
        del url, headers, maximum_message_size_bytes
        return self._sockets.pop(0)


def _control_message(
    agent_id: UUID,
    message_type: MessageType,
    payload: object,
    *,
    sequence_number: int,
    correlation_id: UUID | None = None,
) -> str:
    return parse_control_plane_message(
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": UUID(int=800_000 + sequence_number),
            "message_type": message_type,
            "agent_id": agent_id,
            "sent_at": ROUTED_AT,
            "correlation_id": correlation_id,
            "sequence_number": sequence_number,
            "payload": payload,
        }
    ).model_dump_json()


def _reconnecting_manager(
    *,
    agent_id: UUID,
    boot_id: UUID,
    connector: WebSocketConnector,
    handler: AgentCommandHandler,
    journal: SQLiteAgentCommandJournal,
    events: SQLiteAgentEventBuffer,
    leases: SQLiteReservationLeaseStore,
    outgoing_queue_size: int = 16,
) -> AgentConnectionManager:
    reconciliation = AgentReconciliationReportBuilder(
        agent_id=agent_id,
        boot_id=boot_id,
        journal=journal,
        leases=leases,
        inventory=EmptyInventory(),
        events=events,
        clock=lambda: ROUTED_AT,
    )
    return AgentConnectionManager(
        agent_id=agent_id,
        boot_id=boot_id,
        agent_name="reconnect-agent",
        agent_version="0.6.0-alpha",
        gateway_url="wss://control.invalid/api/v1/agent-gateway",
        credential="agent-specific-secret",
        events=events,
        leases=leases,
        commands=handler,
        drain=NoopDrain(),
        reconciliation=reconciliation,
        connector=connector,
        outgoing_queue_size=outgoing_queue_size,
        event_poll_interval_seconds=0.01,
        reconnect_jitter_ratio=0,
        clock=lambda: ROUTED_AT,
        monotonic=lambda: 10,
    )


def test_disconnect_reconnect_and_process_restart_do_not_duplicate_command_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        agent_id = UUID(int=500_000)
        boot_id = UUID(int=500_001)
        command = RemoteCommand(
            id=UUID(int=500_002),
            agent_id=agent_id,
            bench_id="reconnect-agent/sim-000",
            command_type=RemoteCommandType.PROBE,
            status=RemoteCommandStatus.DISPATCHED,
            created_at=RECONNECTED_AT,
            dispatched_at=ROUTED_AT,
            expires_at=ROUTED_AT + timedelta(minutes=5),
            idempotency_key="partitioned-probe",
        )
        request = CommandRequestPayload(command=command)
        database = SQLiteDatabase(tmp_path / "phase5-command-reconnect.db")
        database.initialize()
        journal = SQLiteAgentCommandJournal(database)
        events = SQLiteAgentEventBuffer(database, agent_id, capacity=32, clock=lambda: ROUTED_AT)
        leases = SQLiteReservationLeaseStore(database, agent_id, clock=lambda: ROUTED_AT)
        safety = AllowingSafety()
        executor = GatedExecutor()
        handler = AgentCommandHandler(
            agent_id=agent_id,
            journal=journal,
            leases=leases,
            events=events,
            safety=safety,
            executor=executor,
            clock=lambda: ROUTED_AT,
        )
        first = ScriptedSocket(agent_id, request, disconnect_after_command=True)
        second = ScriptedSocket(agent_id, request, disconnect_after_command=False)
        manager = _reconnecting_manager(
            agent_id=agent_id,
            boot_id=boot_id,
            connector=ScriptedConnector((first, second)),
            handler=handler,
            journal=journal,
            events=events,
            leases=leases,
        )

        with pytest.raises(SocketDisconnected, match="network partition"):
            await manager.run_once()
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        stored = await journal.get(command.id)
        assert stored is not None
        assert stored.entry.status is RemoteCommandStatus.RUNNING

        reconnect = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(second.waiting_after_script.wait(), timeout=2)
        assert executor.executions == 1
        assert safety.validations == 1

        executor.release.set()
        await asyncio.wait_for(executor.completed.wait(), timeout=2)
        await manager.wait_for_command_tasks()
        await manager.stop()
        assert await reconnect

        completed = await journal.get(command.id)
        assert completed is not None
        assert completed.entry.status is RemoteCommandStatus.SUCCEEDED
        assert completed.entry.result == {"command_id": str(command.id), "executions": 1}

        # A fresh handler represents an Agent process restart. The SQLite journal is the
        # authority, so the completed request is replayed without invoking new hardware.
        restarted_executor = GatedExecutor()
        restarted_safety = AllowingSafety()
        restarted = AgentCommandHandler(
            agent_id=agent_id,
            journal=SQLiteAgentCommandJournal(database),
            leases=SQLiteReservationLeaseStore(database, agent_id, clock=lambda: ROUTED_AT),
            events=events,
            safety=restarted_safety,
            executor=restarted_executor,
            clock=lambda: ROUTED_AT + timedelta(days=1),
        )
        replay = await restarted.handle(request)
        assert replay.replayed
        assert replay.journal_entry is not None
        assert replay.journal_entry.status is RemoteCommandStatus.SUCCEEDED
        assert restarted_executor.executions == 0
        assert restarted_safety.validations == 0
        database.close()

    asyncio.run(scenario())


class UnusedConnector:
    async def connect(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        maximum_message_size_bytes: int,
    ) -> AgentWebSocket:
        del url, headers, maximum_message_size_bytes
        raise AssertionError("The bounded-queue test does not open a connection")


def test_persistent_event_buffer_and_control_queue_are_bounded_under_load(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent_id = UUID(int=600_000)
        boot_id = UUID(int=600_001)
        database = SQLiteDatabase(tmp_path / "phase5-bounds.db")
        database.initialize()
        events = SQLiteAgentEventBuffer(database, agent_id, capacity=32, clock=lambda: ROUTED_AT)
        for index in range(200):
            await events.append(
                "OPERATION_PROGRESS",
                {"command_id": str(UUID(int=610_000 + index)), "progress": index % 100},
                priority=BufferedEventPriority.PROGRESS,
                coalesce_key=f"unique-progress-{index}",
            )
        terminal_ids: set[UUID] = set()
        for index in range(4):
            event = await events.append(
                "OPERATION_SUCCEEDED",
                {"command_id": str(UUID(int=620_000 + index))},
                priority=BufferedEventPriority.TERMINAL,
            )
            assert event is not None
            terminal_ids.add(event.id)

        stats = await events.stats()
        buffered = await events.peek(limit=100)
        assert stats.buffered_data_events == 32
        assert stats.buffered_events == 33  # 32 data records plus one overflow marker
        assert stats.dropped_progress == 172
        assert len(buffered) == 33
        assert terminal_ids.issubset({event.id for event in buffered})

        # Buffer bounds and overflow state survive restart.
        database.close()
        database = SQLiteDatabase(tmp_path / "phase5-bounds.db")
        database.initialize()
        events = SQLiteAgentEventBuffer(database, agent_id, capacity=32, clock=lambda: ROUTED_AT)
        assert (await events.stats()) == stats

        journal = SQLiteAgentCommandJournal(database)
        leases = SQLiteReservationLeaseStore(database, agent_id, clock=lambda: ROUTED_AT)
        handler = AgentCommandHandler(
            agent_id=agent_id,
            journal=journal,
            leases=leases,
            events=events,
            safety=AllowingSafety(),
            executor=GatedExecutor(),
            clock=lambda: ROUTED_AT,
        )
        manager = _reconnecting_manager(
            agent_id=agent_id,
            boot_id=boot_id,
            connector=UnusedConnector(),
            handler=handler,
            journal=journal,
            events=events,
            leases=leases,
            outgoing_queue_size=8,
        )
        payload = AgentStatusPayload(
            agent_id=agent_id,
            boot_id=boot_id,
            status=AgentStatus.ONLINE,
            changed_at=ROUTED_AT,
        )
        for _ in range(8):
            await manager.send(MessageType.AGENT_STATUS, payload)
        with pytest.raises(AgentOutgoingQueueFullError, match="queue is full"):
            await manager.send(MessageType.AGENT_STATUS, payload)
        assert manager.queued_messages == 8
        database.close()

    asyncio.run(scenario())
