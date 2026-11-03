from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane_core.presence import ActiveAgentConnection
from lab_platform.control_plane_core.reconciliation import (
    ReconciliationResult,
    ReconciliationService,
    ReportClaimStatus,
)
from lab_platform.models import (
    AgentConnectionRecord,
    AgentRecord,
    AgentStatus,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    ProtocolMessageDirection,
    ProtocolMessageJournalRecord,
    ProtocolMessageOutcome,
    ReconciliationBenchSnapshot,
    ReconciliationReport,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandAttempt,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.distributed import (
    SQLiteProtocolMessageJournalRepository,
    SQLiteReconciliationRepository,
    SQLiteReservationLeaseRepository,
)
from lab_platform.persistence.distributed_adapters import (
    SQLiteAgentDrainRepository,
    SQLiteAgentPresenceAdapter,
    SQLiteArtifactTransferServiceRepository,
    SQLiteDistributedDirectory,
    SQLiteInventoryAdapter,
    SQLitePersistedReconciliationHandler,
    SQLiteProtocolJournalAdapter,
    SQLiteReconciliationBootRepository,
    SQLiteReconciliationInventoryAdapter,
    SQLiteReconciliationLeaseAdapter,
    SQLiteReconciliationReportStore,
    SQLiteRemoteCommandServiceRepository,
    _reconciliation_result_from_json,
    _reconciliation_result_json,
)

NOW = datetime(2026, 7, 28, 8, tzinfo=UTC)
AGENT_ID = UUID("36cc8c54-1468-45a4-8566-2fc09e646f71")
OTHER_AGENT_ID = UUID("4fc9ff80-e6d5-47e6-bf03-a1af26dcab79")
BOOT_ID = UUID("eaf220d1-7a8c-48c5-b30d-66bf6fb43b54")
BENCH_ID = "coverage-agent/bench-1"


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _seed_agent(
    database: SQLiteDatabase,
    *,
    agent_id: UUID = AGENT_ID,
    slug: str = "coverage-agent",
    status: AgentStatus = AgentStatus.OFFLINE,
) -> AgentRecord:
    agent = AgentRecord(
        id=agent_id,
        slug=slug,
        name=f"{slug} name",
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location="Jerusalem",
        labels={"site": "test"},
        registered_at=NOW,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, location, labels_json, "
            "registered_at, enrollment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(agent.id),
                agent.slug,
                agent.name,
                agent.status.value,
                agent.version,
                agent.protocol_version,
                agent.location,
                json.dumps(agent.labels, sort_keys=True, separators=(",", ":")),
                agent.registered_at.isoformat(),
                agent.enrollment_status.value,
            ),
        )
    return agent


def _bench(
    agent: AgentRecord,
    local_id: str = "bench-1",
    *,
    observed_at: datetime = NOW,
    name: str | None = None,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"{agent.slug}/{local_id}",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=local_id,
        name=name or local_id,
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        target_type="esp32",
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"firmware", "serial"}),
        labels={"rack": "a"},
        firmware_version="1.2.3",
        last_seen_at=observed_at,
        created_at=NOW,
        updated_at=observed_at,
    )


def _connection(
    *,
    connection_id: UUID,
    boot_id: UUID = BOOT_ID,
    connected_at: datetime = NOW,
    heartbeat_at: datetime = NOW,
    sequence_number: int = 0,
    disconnected_at: datetime | None = None,
) -> AgentConnectionRecord:
    return AgentConnectionRecord(
        id=connection_id,
        agent_id=AGENT_ID,
        boot_id=boot_id,
        protocol_version="1.0",
        connected_at=connected_at,
        last_heartbeat_at=heartbeat_at,
        disconnected_at=disconnected_at,
        last_sequence_number=sequence_number,
    )


def _command(
    *,
    command_id: UUID,
    idempotency_key: str,
    operation_id: UUID | None = None,
    status: RemoteCommandStatus = RemoteCommandStatus.CREATED,
) -> RemoteCommand:
    dispatched_at = (
        NOW + timedelta(seconds=1) if status is not RemoteCommandStatus.CREATED else None
    )
    return RemoteCommand(
        id=command_id,
        agent_id=AGENT_ID,
        bench_id=BENCH_ID,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={"workflow_name": "adapter-coverage"},
        status=status,
        created_at=NOW,
        dispatched_at=dispatched_at,
        expires_at=NOW + timedelta(minutes=30),
        idempotency_key=idempotency_key,
        operation_id=operation_id,
    )


def _operation(command: RemoteCommand, operation_id: UUID) -> DistributedOperation:
    return DistributedOperation(
        id=operation_id,
        remote_command_id=command.id,
        agent_id=command.agent_id,
        bench_id=command.bench_id,
        operation_type=command.command_type.value,
        created_at=command.created_at,
    )


def _seed_reservation(
    database: SQLiteDatabase,
    reservation_id: UUID,
    *,
    status: str = "active",
) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO reservations "
            "(id, bench_id, owner, created_at, status, requested_at, starts_at, ends_at, "
            "activated_at, source, metadata, release_pending) "
            "VALUES (?, ?, 'coverage', ?, ?, ?, ?, ?, ?, 'api', '{}', 0)",
            (
                str(reservation_id),
                BENCH_ID,
                NOW.isoformat(),
                status,
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=1)).isoformat(),
                NOW.isoformat(),
            ),
        )


def test_sqlite_presence_adapter_fences_connections_and_preserves_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "presence-adapter.db")
        repository = SQLiteAgentPresenceAdapter(database)

        missing = AgentRecord(
            id=AGENT_ID,
            slug="coverage-agent",
            name="missing",
            status=AgentStatus.OFFLINE,
            version="0.6.0-alpha",
            protocol_version="1.0",
            registered_at=NOW,
            enrollment_status=EnrollmentStatus.ENROLLED,
        )
        with pytest.raises(ValueError, match="one-time enrollment"):
            await repository.add_agent(missing)
        assert await repository.get_agent(AGENT_ID) is None
        assert await repository.get_connection(UUID(int=1)) is None
        assert await repository.get_active_connection(AGENT_ID) is None
        assert await repository.list_active_connections() == []

        enrolled = _seed_agent(database)
        assert await repository.add_agent(enrolled) == enrolled
        assert await repository.list_agents() == [enrolled]
        with pytest.raises(ValueError, match="identity conflicts"):
            await repository.add_agent(enrolled.model_copy(update={"slug": "renamed"}))

        connected = enrolled.model_copy(
            update={
                "status": AgentStatus.ONLINE,
                "last_connected_at": NOW,
                "last_seen_at": NOW,
            }
        )
        first_record = _connection(connection_id=UUID(int=101))
        first = ActiveAgentConnection(first_record, 10.0)
        with pytest.raises(ValueError, match="does not exist"):
            await repository.activate(
                connected.model_copy(update={"id": OTHER_AGENT_ID}),
                ActiveAgentConnection(
                    first_record.model_copy(update={"agent_id": OTHER_AGENT_ID}),
                    10.0,
                ),
            )
        with pytest.raises(ValueError, match="identity conflicts"):
            await repository.activate(
                connected.model_copy(update={"slug": "wrong-slug"}),
                first,
            )

        assert await repository.activate(connected, first) is None
        assert await repository.get_connection(first_record.id) == first
        assert await repository.get_active_connection(AGENT_ID) == first
        assert await repository.list_active_connections() == [first]
        assert await repository.activate(connected, first) is None
        conflicting = ActiveAgentConnection(
            first_record.model_copy(update={"last_sequence_number": 1}),
            11.0,
        )
        with pytest.raises(ValueError, match="different state"):
            await repository.activate(connected, conflicting)

        stale_monotonic = ActiveAgentConnection(first_record, 9.0)
        assert not await repository.update_if_current(connected, stale_monotonic)
        unknown_boot = ActiveAgentConnection(
            first_record.model_copy(update={"boot_id": uuid4()}),
            11.0,
        )
        assert not await repository.update_if_current(connected, unknown_boot)

        heartbeat_at = NOW + timedelta(seconds=5)
        heartbeat_record = first_record.model_copy(
            update={
                "last_heartbeat_at": heartbeat_at,
                "last_sequence_number": 1,
                "observed_clock_offset_seconds": 0.25,
            }
        )
        heartbeat_agent = connected.model_copy(update={"last_seen_at": heartbeat_at})
        heartbeat = ActiveAgentConnection(heartbeat_record, 12.0)
        assert await repository.update_if_current(heartbeat_agent, heartbeat)
        assert await repository.get_active_connection(AGENT_ID) == heartbeat
        assert not await repository.update_if_current(connected, first)

        with pytest.raises(ValueError, match="disconnected_at"):
            await repository.disconnect_if_current(heartbeat_agent, heartbeat)
        stale_disconnect_record = heartbeat_record.model_copy(
            update={
                "last_sequence_number": 0,
                "disconnected_at": heartbeat_at + timedelta(seconds=1),
            }
        )
        assert not await repository.disconnect_if_current(
            heartbeat_agent,
            ActiveAgentConnection(stale_disconnect_record, 13.0),
        )

        second_time = heartbeat_at + timedelta(seconds=2)
        second_boot = UUID(int=202)
        second_record = _connection(
            connection_id=UUID(int=102),
            boot_id=second_boot,
            connected_at=second_time,
            heartbeat_at=second_time,
        )
        second_agent = heartbeat_agent.model_copy(
            update={"last_connected_at": second_time, "last_seen_at": second_time}
        )
        superseded = await repository.activate(
            second_agent,
            ActiveAgentConnection(second_record, 20.0),
        )
        assert superseded == ActiveAgentConnection(heartbeat_record, 12.0)
        persisted_first = await repository.get_connection(first_record.id)
        assert persisted_first == ActiveAgentConnection(
            heartbeat_record.model_copy(update={"disconnected_at": second_time}),
            0.0,
        )
        assert not await repository.disconnect_if_current(
            heartbeat_agent,
            ActiveAgentConnection(
                heartbeat_record.model_copy(update={"disconnected_at": second_time}),
                12.0,
            ),
        )

        disconnected_at = second_time + timedelta(seconds=3)
        disconnected_record = second_record.model_copy(update={"disconnected_at": disconnected_at})
        disconnected_agent = second_agent.model_copy(
            update={"status": AgentStatus.OFFLINE, "disconnected_at": disconnected_at}
        )
        disconnected = ActiveAgentConnection(disconnected_record, 21.0)
        assert await repository.disconnect_if_current(disconnected_agent, disconnected)
        assert not await repository.disconnect_if_current(disconnected_agent, disconnected)
        assert await repository.get_active_connection(AGENT_ID) is None

        boots = SQLiteReconciliationBootRepository(database)
        assert await boots.get_boot_context(AGENT_ID) is None
        database.close()

    asyncio.run(scenario())


def test_sqlite_inventory_adapter_is_atomic_idempotent_and_report_compatible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "inventory-adapter.db")
        agent = _seed_agent(database)
        presence = SQLiteAgentPresenceAdapter(database)
        inventory = SQLiteInventoryAdapter(database)
        directory = SQLiteDistributedDirectory(presence, inventory)

        assert await inventory.get(BENCH_ID) is None
        assert await inventory.list() == []
        assert await directory.get_agent(agent.id) == agent
        assert await directory.get_bench(BENCH_ID) is None
        with pytest.raises(ValueError, match="provided together"):
            await inventory.reconcile_agent_snapshot(
                agent,
                (),
                observed_at=NOW,
                snapshot_id=UUID(int=300),
            )

        wrong = _bench(agent).model_copy(update={"agent_id": OTHER_AGENT_ID})
        with pytest.raises(ValueError, match="identity"):
            await inventory.reconcile_agent_snapshot(agent, (wrong,), observed_at=NOW)
        assert await inventory.list() == []

        first_snapshot_id = UUID(int=301)
        first = (_bench(agent), _bench(agent, "bench-2"))
        result = await inventory.reconcile_agent_snapshot(
            agent,
            first,
            observed_at=NOW,
            snapshot_id=first_snapshot_id,
            boot_id=BOOT_ID,
            generated_at=NOW,
        )
        assert result.added_ids == {BENCH_ID, "coverage-agent/bench-2"}
        assert not result.updated_ids
        assert not result.offline_ids
        assert await inventory.get(BENCH_ID) == first[0]
        assert await directory.get_bench(BENCH_ID) == first[0]

        repeated = await inventory.reconcile_agent_snapshot(
            agent,
            first,
            observed_at=NOW,
            snapshot_id=first_snapshot_id,
            boot_id=BOOT_ID,
            generated_at=NOW,
        )
        assert not repeated.added_ids
        assert not repeated.updated_ids
        assert not repeated.offline_ids
        with pytest.raises(ValueError, match="different inventory"):
            await inventory.reconcile_agent_snapshot(
                agent,
                (first[0].model_copy(update={"name": "changed"}), first[1]),
                observed_at=NOW,
                snapshot_id=first_snapshot_id,
                boot_id=BOOT_ID,
                generated_at=NOW,
            )

        later = NOW + timedelta(minutes=2)
        changed = _bench(agent, observed_at=later, name="renamed")
        second = await inventory.reconcile_agent_snapshot(
            agent,
            (changed,),
            observed_at=later,
            snapshot_id=UUID(int=302),
            boot_id=BOOT_ID,
            generated_at=later,
        )
        assert second.updated_ids == {BENCH_ID}
        assert second.offline_ids == {"coverage-agent/bench-2"}
        assert {item.id for item in second.benches} == {
            BENCH_ID,
            "coverage-agent/bench-2",
        }
        with pytest.raises(ValueError, match="stale"):
            await inventory.reconcile_agent_snapshot(
                agent,
                first,
                observed_at=later,
                snapshot_id=UUID(int=303),
                boot_id=BOOT_ID,
                generated_at=NOW + timedelta(minutes=1),
            )

        offlined = await inventory.mark_agent_offline(
            agent.id,
            observed_at=later + timedelta(seconds=1),
        )
        assert all(item.status is GlobalBenchStatus.OFFLINE for item in offlined)

        report_adapter = SQLiteReconciliationInventoryAdapter(presence, inventory)
        with pytest.raises(ValueError, match="does not exist"):
            await report_adapter.reconcile_report_inventory(
                OTHER_AGENT_ID,
                boot_id=BOOT_ID,
                generated_at=later,
                snapshots=(),
                observed_at=later,
            )
        report_time = later + timedelta(minutes=1)
        report_result = await report_adapter.reconcile_report_inventory(
            agent.id,
            boot_id=BOOT_ID,
            generated_at=report_time,
            snapshots=(
                ReconciliationBenchSnapshot(
                    local_bench_id="bench-3",
                    name="from report",
                    backend_id="hardware",
                    kind=GlobalBenchKind.PHYSICAL,
                    target_type="stm32",
                    status=GlobalBenchStatus.ONLINE,
                    health=HealthStatus.WARNING,
                    capabilities=frozenset({"flash"}),
                    labels={"source": "reconciliation"},
                    firmware_version="9.9",
                ),
            ),
            observed_at=report_time,
        )
        assert report_result.added_ids == {"coverage-agent/bench-3"}
        report_bench = await inventory.get("coverage-agent/bench-3")
        assert report_bench is not None
        assert report_bench.kind is GlobalBenchKind.PHYSICAL
        assert report_bench.labels == {"source": "reconciliation"}
        database.close()

    asyncio.run(scenario())


def test_remote_command_adapter_bundles_idempotently_and_uses_status_cas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "commands-adapter.db")
        agent = _seed_agent(database)
        await SQLiteInventoryAdapter(database).reconcile_agent_snapshot(
            agent,
            (_bench(agent),),
            observed_at=NOW,
        )
        repository = SQLiteRemoteCommandServiceRepository(database)
        operation_id = UUID(int=401)
        command = _command(
            command_id=UUID(int=400),
            idempotency_key="command-bundle",
            operation_id=operation_id,
        )
        operation = _operation(command, operation_id)

        assert await repository.create_bundle(command, operation) == (command, operation)
        assert await repository.create_bundle(command, operation) == (command, operation)
        retry = command.model_copy(update={"id": UUID(int=402), "operation_id": UUID(int=403)})
        assert await repository.create_bundle(retry, None) == (command, operation)
        with pytest.raises(ValueError, match="idempotency"):
            await repository.create_bundle(
                retry.model_copy(update={"payload": {"workflow_name": "other"}}),
                None,
            )

        unbound = _command(
            command_id=UUID(int=404),
            idempotency_key="bad-operation-binding",
            operation_id=UUID(int=405),
        )
        with pytest.raises(ValueError, match="identities differ"):
            await repository.create_bundle(unbound, _operation(unbound, UUID(int=406)))
        assert await repository.get_command(unbound.id) is None

        command_only = _command(command_id=UUID(int=407), idempotency_key="without-operation")
        assert await repository.create_bundle(command_only, None) == (command_only, None)
        assert await repository.get_operation_for_command(command_only.id) is None
        assert await repository.get_command(command.id) == command
        assert await repository.get_by_idempotency_key(AGENT_ID, command.idempotency_key) == command
        assert await repository.get_by_idempotency_key(OTHER_AGENT_ID, "missing") is None
        assert await repository.get_operation_for_command(command.id) == operation

        missing_command = _command(command_id=uuid4(), idempotency_key="missing")
        assert (
            await repository.update_command(
                missing_command,
                expected_statuses={RemoteCommandStatus.CREATED},
            )
            is None
        )
        assert (
            await repository.update_command(
                command,
                expected_statuses={RemoteCommandStatus.QUEUED},
            )
            is None
        )
        dispatched_at = NOW + timedelta(seconds=1)
        dispatched = command.model_copy(
            update={
                "status": RemoteCommandStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        )
        assert (
            await repository.update_command(
                dispatched,
                expected_statuses={RemoteCommandStatus.CREATED},
            )
            == dispatched
        )

        missing_operation = _operation(command_only, uuid4())
        assert (
            await repository.update_operation(
                missing_operation,
                expected_statuses={DistributedOperationStatus.CREATED},
            )
            is None
        )
        assert (
            await repository.update_operation(
                operation,
                expected_statuses={DistributedOperationStatus.RUNNING},
            )
            is None
        )
        dispatched_operation = operation.model_copy(
            update={
                "status": DistributedOperationStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        )
        assert (
            await repository.update_operation(
                dispatched_operation,
                expected_statuses={DistributedOperationStatus.CREATED},
            )
            == dispatched_operation
        )
        assert await repository.list_operations(
            agent_id=AGENT_ID,
            statuses={DistributedOperationStatus.DISPATCHED},
            limit=10,
        ) == [dispatched_operation]
        assert await repository.list_operations(agent_id=None, statuses=(), limit=10) == [
            dispatched_operation
        ]
        assert await repository.list_commands(
            agent_id=AGENT_ID,
            statuses={RemoteCommandStatus.DISPATCHED},
            limit=10,
        ) == [dispatched]
        assert len(await repository.list_commands(statuses=(), limit=10)) == 2

        await repository.acknowledge_latest_attempt(command.id, NOW + timedelta(seconds=2))
        with pytest.raises(ValueError, match="compare-and-set"):
            await repository.record_attempt(
                RemoteCommandAttempt(
                    id=UUID(int=408),
                    command_id=UUID(int=999),
                    attempt_number=1,
                    dispatched_at=dispatched_at,
                )
            )
        attempt = RemoteCommandAttempt(
            id=UUID(int=409),
            command_id=command.id,
            attempt_number=1,
            dispatched_at=dispatched_at,
        )
        assert await repository.record_attempt(attempt) == attempt
        acknowledged_at = NOW + timedelta(seconds=2)
        await repository.acknowledge_latest_attempt(command.id, acknowledged_at)
        await repository.acknowledge_latest_attempt(command.id, acknowledged_at)
        with database.transaction() as connection:
            row = connection.execute(
                "SELECT acknowledged_at FROM remote_command_attempts WHERE id = ?",
                (str(attempt.id),),
            ).fetchone()
        assert row is not None and row["acknowledged_at"] == acknowledged_at.isoformat()
        database.close()

    asyncio.run(scenario())


def test_reconciliation_report_store_fences_content_and_round_trips_results(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "reconciliation-claims.db")
        _seed_agent(database)
        store = SQLiteReconciliationReportStore(database)
        report_id = UUID(int=501)
        digest = "a" * 64
        assert (
            await store.claim_report(
                report_id,
                agent_id=AGENT_ID,
                content_digest=digest,
                received_at=NOW,
            )
        ).status is ReportClaimStatus.NEW
        assert (
            await store.claim_report(
                report_id,
                agent_id=AGENT_ID,
                content_digest=digest,
                received_at=NOW,
            )
        ).status is ReportClaimStatus.IN_PROGRESS
        assert (
            await store.claim_report(
                report_id,
                agent_id=AGENT_ID,
                content_digest="b" * 64,
                received_at=NOW,
            )
        ).status is ReportClaimStatus.CONFLICT
        assert (
            await store.claim_report(
                UUID(int=502),
                agent_id=AGENT_ID,
                content_digest=digest,
                received_at=NOW,
            )
        ).status is ReportClaimStatus.IN_PROGRESS

        await store.abandon_report(UUID(int=999), content_digest=digest)
        await store.abandon_report(report_id, content_digest="c" * 64)
        await store.abandon_report(report_id, content_digest=digest)
        assert (
            await store.claim_report(
                report_id,
                agent_id=AGENT_ID,
                content_digest=digest,
                received_at=NOW,
            )
        ).status is ReportClaimStatus.NEW

        result = ReconciliationResult(
            report_id=report_id,
            report_digest=digest,
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            restarted=True,
            reconciled_command_ids=frozenset({UUID(int=503), UUID(int=504)}),
            reconciled_operation_ids=frozenset({UUID(int=505)}),
            interrupted_operation_ids=frozenset({UUID(int=506)}),
            expired_reservation_ids=frozenset({UUID(int=507)}),
            stale_local_leases=frozenset({(UUID(int=508), 3)}),
            inventory_reconciled=True,
            deduplicated=False,
        )
        with pytest.raises(ValueError, match="lost or changed"):
            await store.complete_report(UUID(int=999), content_digest=digest, result=result)
        with pytest.raises(ValueError, match="lost or changed"):
            await store.complete_report(report_id, content_digest="d" * 64, result=result)
        await store.complete_report(report_id, content_digest=digest, result=result)
        await store.complete_report(report_id, content_digest=digest, result=result)
        changed = replace(result, inventory_reconciled=False)
        with pytest.raises(ValueError, match="cannot change"):
            await store.complete_report(report_id, content_digest=digest, result=changed)

        duplicate = await store.claim_report(
            report_id,
            agent_id=AGENT_ID,
            content_digest=digest,
            received_at=NOW,
        )
        assert duplicate.status is ReportClaimStatus.DUPLICATE
        assert duplicate.prior_result == result
        content_duplicate = await store.claim_report(
            UUID(int=509),
            agent_id=AGENT_ID,
            content_digest=digest,
            received_at=NOW + timedelta(seconds=1),
        )
        assert content_duplicate.status is ReportClaimStatus.DUPLICATE
        assert content_duplicate.prior_result == result

        serialized = _reconciliation_result_json(result)
        assert _reconciliation_result_from_json(serialized) == result
        payload = json.loads(serialized)
        payload.pop("deduplicated")
        assert not _reconciliation_result_from_json(json.dumps(payload)).deduplicated
        with pytest.raises(ValueError, match="missing its result"):
            _reconciliation_result_from_json(None)
        with pytest.raises(ValueError, match="must be an object"):
            _reconciliation_result_from_json("[]")
        with pytest.raises(ValueError, match="malformed"):
            _reconciliation_result_from_json("{}")
        database.close()

    asyncio.run(scenario())


def test_reconciliation_boot_lease_and_persisted_handler_integrate_with_sqlite(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "reconciliation-adapters.db")
        enrolled = _seed_agent(database)
        presence = SQLiteAgentPresenceAdapter(database)
        connected = enrolled.model_copy(
            update={
                "status": AgentStatus.ONLINE,
                "last_connected_at": NOW,
                "last_seen_at": NOW,
            }
        )
        first = _connection(connection_id=UUID(int=601), boot_id=UUID(int=600))
        await presence.activate(connected, ActiveAgentConnection(first, 1.0))
        second_at = NOW + timedelta(seconds=1)
        active = _connection(
            connection_id=UUID(int=602),
            boot_id=BOOT_ID,
            connected_at=second_at,
            heartbeat_at=second_at,
        )
        second_agent = connected.model_copy(
            update={"last_connected_at": second_at, "last_seen_at": second_at}
        )
        await presence.activate(second_agent, ActiveAgentConnection(active, 2.0))
        context = await SQLiteReconciliationBootRepository(database).get_boot_context(AGENT_ID)
        assert context is not None
        assert context.active_boot_id == BOOT_ID
        assert context.interrupted_boot_id == first.boot_id
        assert context.restarted

        inventory = SQLiteInventoryAdapter(database)
        await inventory.reconcile_agent_snapshot(
            second_agent,
            (_bench(second_agent),),
            observed_at=second_at,
        )
        reservation_id = UUID(int=603)
        _seed_reservation(database, reservation_id)
        lease = ReservationLease(
            reservation_id=reservation_id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="coverage",
            valid_from=NOW,
            valid_until=NOW + timedelta(minutes=5),
            lease_version=1,
        )
        leases = SQLiteReservationLeaseRepository(database)
        assert await leases.put(lease, expected_current_version=None) == lease
        lease_adapter = SQLiteReconciliationLeaseAdapter(database)
        assert await lease_adapter.list_leases(AGENT_ID) == [lease]
        assert (
            await lease_adapter.release_if_current(
                reservation_id,
                agent_id=OTHER_AGENT_ID,
                expected_lease_version=1,
                released_at=NOW + timedelta(minutes=1),
            )
            is None
        )
        assert (
            await lease_adapter.release_if_current(
                reservation_id,
                agent_id=AGENT_ID,
                expected_lease_version=2,
                released_at=NOW + timedelta(minutes=1),
            )
            is None
        )
        released = await lease_adapter.release_if_current(
            reservation_id,
            agent_id=AGENT_ID,
            expected_lease_version=1,
            released_at=NOW + timedelta(minutes=1),
        )
        assert released is not None and released.released_at is not None
        assert await lease_adapter.list_leases(AGENT_ID) == []

        report_store = SQLiteReconciliationReportStore(database)
        work = SQLiteRemoteCommandServiceRepository(database)
        report_inventory = SQLiteReconciliationInventoryAdapter(presence, inventory)
        service = ReconciliationService(
            report_store,
            SQLiteReconciliationBootRepository(database),
            work,
            lease_adapter,
            report_inventory,
            clock=lambda: NOW + timedelta(minutes=10),
        )
        handler = SQLitePersistedReconciliationHandler(database, service)
        report = ReconciliationReport(
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW + timedelta(minutes=10),
            active_commands=(),
            recent_commands=(),
            local_reservation_leases=(),
            bench_snapshots=(
                ReconciliationBenchSnapshot(
                    local_bench_id="bench-1",
                    name="reconciled",
                    backend_id="simlab",
                    kind=GlobalBenchKind.SIMULATED,
                    target_type="esp32",
                    status=GlobalBenchStatus.ONLINE,
                    health=HealthStatus.HEALTHY,
                ),
            ),
            buffered_event_count=0,
        )
        report_id = UUID(int=604)
        reconciled = await handler.reconcile(report_id, report)
        assert reconciled.report_id == report_id
        assert reconciled.restarted
        assert reconciled.inventory_reconciled
        stored = await SQLiteReconciliationRepository(database).latest(AGENT_ID)
        assert stored is not None and stored.report == report
        duplicate = await handler.reconcile(report_id, report)
        assert duplicate.deduplicated
        database.close()

    asyncio.run(scenario())


def test_drain_and_protocol_adapters_report_real_workload_and_cancel_queued_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "drain-protocol-adapters.db")
        agent = _seed_agent(database, status=AgentStatus.ONLINE)
        inventory = SQLiteInventoryAdapter(database)
        await inventory.reconcile_agent_snapshot(agent, (_bench(agent),), observed_at=NOW)
        commands = SQLiteRemoteCommandServiceRepository(database)
        operation_id = UUID(int=701)
        queued = _command(
            command_id=UUID(int=700),
            idempotency_key="drain-queued",
            operation_id=operation_id,
            status=RemoteCommandStatus.CREATED,
        ).model_copy(update={"status": RemoteCommandStatus.QUEUED})
        operation = _operation(queued, operation_id)
        await commands.create_bundle(queued, operation)
        reservation_id = UUID(int=702)
        _seed_reservation(database, reservation_id)
        ci_session_id = UUID(int=703)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO ci_sessions "
                "(id, provider, external_run_id, requested_by, bench_id, status, created_at) "
                "VALUES (?, 'local', 'run-1', 'coverage', ?, 'created', ?)",
                (str(ci_session_id), BENCH_ID, NOW.isoformat()),
            )

        drain = SQLiteAgentDrainRepository(database)
        assert await drain.get_drain_snapshot(OTHER_AGENT_ID) is None
        snapshot = await drain.get_drain_snapshot(AGENT_ID)
        assert snapshot is not None
        assert snapshot.workload.active_operations == 1
        assert snapshot.workload.active_reservations == 1
        assert snapshot.workload.queued_ci_sessions == 1
        assert not snapshot.has_active_connection
        assert (
            await drain.compare_and_set_status(
                AGENT_ID,
                expected_status=AgentStatus.OFFLINE,
                target_status=AgentStatus.DRAINING,
            )
            is None
        )
        assert (
            await drain.compare_and_set_status(
                AGENT_ID,
                expected_status=AgentStatus.ONLINE,
                target_status=AgentStatus.DRAINING,
                require_idle=True,
            )
            is None
        )
        assert (
            await drain.compare_and_set_status(
                AGENT_ID,
                expected_status=AgentStatus.ONLINE,
                target_status=AgentStatus.DRAINING,
                require_active_connection=True,
            )
            is None
        )

        presence = SQLiteAgentPresenceAdapter(database)
        connection_record = _connection(connection_id=UUID(int=704))
        connected_agent = agent.model_copy(update={"last_connected_at": NOW, "last_seen_at": NOW})
        await presence.activate(
            connected_agent,
            ActiveAgentConnection(connection_record, 1.0),
        )
        draining = await drain.compare_and_set_status(
            AGENT_ID,
            expected_status=AgentStatus.ONLINE,
            target_status=AgentStatus.DRAINING,
            require_active_connection=True,
        )
        assert draining is not None
        assert draining.agent.status is AgentStatus.DRAINING
        assert draining.has_active_connection
        assert (
            await drain.cancel_queued_work(
                AGENT_ID,
                expected_status=AgentStatus.ONLINE,
            )
            is None
        )
        assert (
            await drain.cancel_queued_work(
                AGENT_ID,
                expected_status=AgentStatus.DRAINING,
            )
            == 2
        )
        assert (
            await drain.cancel_queued_work(
                AGENT_ID,
                expected_status=AgentStatus.DRAINING,
            )
            == 0
        )
        cancelled = await commands.get_command(queued.id)
        assert cancelled is not None
        assert cancelled.status is RemoteCommandStatus.CANCELLED
        assert cancelled.error_code == "AGENT_DRAINING"
        with database.transaction() as connection:
            ci_status = connection.execute(
                "SELECT status FROM ci_sessions WHERE id = ?",
                (str(ci_session_id),),
            ).fetchone()["status"]
        assert ci_status == "cancel_requested"

        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE distributed_operations SET status = 'SUCCEEDED', completed_at = ? "
                "WHERE id = ?",
                ((NOW + timedelta(seconds=10)).isoformat(), str(operation.id)),
            )
            connection.execute(
                "UPDATE reservations SET status = 'released' WHERE id = ?",
                (str(reservation_id),),
            )
        drained = await drain.compare_and_set_status(
            AGENT_ID,
            expected_status=AgentStatus.DRAINING,
            target_status=AgentStatus.DRAINED,
            require_idle=True,
        )
        assert drained is not None and drained.workload.idle

        protocol = SQLiteProtocolJournalAdapter(database)
        message = ProtocolMessageJournalRecord(
            message_id=UUID(int=705),
            agent_id=AGENT_ID,
            connection_id=connection_record.id,
            direction=ProtocolMessageDirection.AGENT_TO_CONTROL_PLANE,
            sequence_number=1,
            message_type="EVENT_BATCH",
            payload_sha256="e" * 64,
            observed_at=NOW,
            outcome=ProtocolMessageOutcome.RECEIVED,
        )
        assert await protocol.record_protocol_message(message)
        assert not await protocol.record_protocol_message(message)
        with pytest.raises(ValueError, match="handled_at"):
            await protocol.finalize_protocol_message(message)
        finalized = message.model_copy(
            update={
                "handled_at": NOW + timedelta(seconds=1),
                "outcome": ProtocolMessageOutcome.HANDLED,
            }
        )
        await protocol.finalize_protocol_message(finalized)
        persisted = await SQLiteProtocolMessageJournalRepository(database).get(
            AGENT_ID,
            message.direction,
            message.message_id,
        )
        assert persisted == finalized
        database.close()

    asyncio.run(scenario())


def test_artifact_transfer_adapter_enforces_upload_and_attempt_compare_and_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "artifact-transfer-adapter.db")
        agent = _seed_agent(database)
        await SQLiteInventoryAdapter(database).reconcile_agent_snapshot(
            agent,
            (_bench(agent),),
            observed_at=NOW,
        )
        command = _command(command_id=UUID(int=800), idempotency_key="artifact-command")
        await SQLiteRemoteCommandServiceRepository(database).create_bundle(command, None)
        repository = SQLiteArtifactTransferServiceRepository(database)
        artifact = RemoteArtifactMetadata(
            id=UUID(int=801),
            agent_id=AGENT_ID,
            local_artifact_id=UUID(int=802),
            command_id=command.id,
            name="results.xml",
            artifact_type="junit",
            content_type="application/xml",
            size_bytes=1024,
            sha256="f" * 64,
            created_at=NOW,
        )
        assert await repository.get_remote_artifact(artifact.id) is None
        assert await repository.put_remote_artifact(artifact) == artifact
        assert await repository.put_remote_artifact(artifact) == artifact
        with pytest.raises(ValueError, match="other metadata"):
            await repository.put_remote_artifact(artifact.model_copy(update={"name": "forged"}))
        uploaded = artifact.model_copy(update={"uploaded_at": NOW + timedelta(seconds=5)})
        uploaded_alias = uploaded.model_copy(update={"id": UUID(int=811)})
        assert await repository.put_remote_artifact(uploaded_alias) == uploaded
        assert await repository.put_remote_artifact(uploaded) == uploaded
        with pytest.raises(ValueError, match="cannot regress"):
            await repository.put_remote_artifact(artifact)
        with pytest.raises(ValueError, match="cannot regress"):
            await repository.put_remote_artifact(
                uploaded.model_copy(update={"uploaded_at": NOW + timedelta(seconds=6)})
            )

        transfer = ArtifactTransferRecord(
            id=UUID(int=803),
            agent_id=AGENT_ID,
            artifact_id=artifact.id,
            direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
            status=ArtifactTransferStatus.PENDING,
            token_hash="1" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
        assert await repository.create_transfer(transfer) == transfer
        assert await repository.get_transfer(transfer.id) == transfer
        assert await repository.get_transfer_by_token_hash(transfer.token_hash) == transfer
        assert await repository.get_transfer_by_token_hash("2" * 64) is None
        missing_transfer = transfer.model_copy(update={"id": UUID(int=804), "token_hash": "3" * 64})
        assert (
            await repository.update_transfer(
                missing_transfer,
                expected_statuses={ArtifactTransferStatus.PENDING},
            )
            is None
        )
        assert (
            await repository.update_transfer(
                transfer,
                expected_statuses={ArtifactTransferStatus.IN_PROGRESS},
            )
            is None
        )
        in_progress = transfer.model_copy(update={"status": ArtifactTransferStatus.IN_PROGRESS})
        assert (
            await repository.update_transfer(
                in_progress,
                expected_statuses={ArtifactTransferStatus.PENDING},
            )
            == in_progress
        )

        with pytest.raises(ValueError, match="compare-and-set"):
            await repository.add_attempt(
                ArtifactTransferAttempt(
                    id=UUID(int=805),
                    transfer_id=UUID(int=999),
                    attempt_number=1,
                    started_at=NOW,
                )
            )
        first_attempt = ArtifactTransferAttempt(
            id=UUID(int=806),
            transfer_id=transfer.id,
            attempt_number=1,
            started_at=NOW,
        )
        assert await repository.add_attempt(first_attempt) == first_attempt
        assert await repository.add_attempt(first_attempt) == first_attempt
        with pytest.raises(ValueError, match="compare-and-set"):
            await repository.add_attempt(
                ArtifactTransferAttempt(
                    id=UUID(int=807),
                    transfer_id=transfer.id,
                    attempt_number=3,
                    started_at=NOW + timedelta(seconds=2),
                )
            )
        persisted_transfer = await repository.get_transfer(transfer.id)
        assert persisted_transfer is not None and persisted_transfer.attempt_count == 1

        fresh_database = _database(tmp_path / "artifact-cas.db")
        fresh_agent = _seed_agent(fresh_database)
        await SQLiteInventoryAdapter(fresh_database).reconcile_agent_snapshot(
            fresh_agent,
            (_bench(fresh_agent),),
            observed_at=NOW,
        )
        fresh_command = _command(command_id=UUID(int=808), idempotency_key="artifact-cas")
        await SQLiteRemoteCommandServiceRepository(fresh_database).create_bundle(
            fresh_command,
            None,
        )
        cas_repository = SQLiteArtifactTransferServiceRepository(fresh_database)
        cas_artifact = artifact.model_copy(
            update={
                "id": UUID(int=809),
                "local_artifact_id": UUID(int=810),
                "command_id": fresh_command.id,
            }
        )
        await cas_repository.put_remote_artifact(cas_artifact)
        monkeypatch.setattr(
            cas_repository._artifacts,
            "update_uploaded",
            AsyncMock(return_value=None),
        )
        with pytest.raises(ValueError, match="compare-and-set"):
            await cas_repository.put_remote_artifact(
                cas_artifact.model_copy(update={"uploaded_at": NOW + timedelta(seconds=1)})
            )
        fresh_database.close()
        database.close()

    asyncio.run(scenario())
