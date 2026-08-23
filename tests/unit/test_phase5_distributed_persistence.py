from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.models import (
    AgentConnectionRecord,
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    ProtocolMessageDirection,
    ProtocolMessageJournalRecord,
    ProtocolMessageOutcome,
    ReconciliationBenchSnapshot,
    ReconciliationCommandState,
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
    ProtocolMessageRecordDisposition,
    SQLiteAgentConnectionRepository,
    SQLiteAgentTimelineRepository,
    SQLiteArtifactTransferRepository,
    SQLiteDistributedOperationRepository,
    SQLiteGlobalBenchRepository,
    SQLiteProtocolMessageJournalRepository,
    SQLiteReconciliationRepository,
    SQLiteRemoteArtifactRepository,
    SQLiteRemoteCommandRepository,
    SQLiteReservationLeaseRepository,
    StoredBenchSnapshot,
)
from lab_platform.persistence.migrations import SCHEMA_VERSION

AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")
BOOT_ID = UUID("1ab59e13-5517-482f-849c-2fe8145db25c")
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
BENCH_ID = "home-lab/esp32-01"


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _seed_agent(database: SQLiteDatabase) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, labels_json, "
            "registered_at, enrollment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(AGENT_ID),
                "home-lab",
                "Home Lab",
                "ONLINE",
                "0.6.0-alpha",
                "1.0",
                '{"location":"home"}',
                NOW.isoformat(),
                "ENROLLED",
            ),
        )


def _bench(
    local_id: str = "esp32-01",
    *,
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    observed_at: datetime = NOW,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"home-lab/{local_id}",
        agent_id=AGENT_ID,
        agent_slug="home-lab",
        local_bench_id=local_id,
        name=local_id,
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        target_type="esp32-devkit-v1",
        status=status,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"firmware", "serial"}),
        labels={"board": "esp32"},
        last_seen_at=observed_at,
        created_at=NOW,
        updated_at=observed_at,
    )


def _command(**updates: object) -> RemoteCommand:
    values: dict[str, object] = {
        "id": UUID("10f76299-e654-41a8-a896-71f8fd9bd0b1"),
        "agent_id": AGENT_ID,
        "bench_id": BENCH_ID,
        "command_type": RemoteCommandType.RUN_WORKFLOW,
        "payload": {"workflow_name": "esp32-ci-test"},
        "status": RemoteCommandStatus.CREATED,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "idempotency_key": "ci-session:42:workflow",
    }
    values.update(updates)
    return RemoteCommand.model_validate(values)


async def _seed_bench(database: SQLiteDatabase) -> None:
    await SQLiteGlobalBenchRepository(database).upsert(_bench())


def _seed_reservation(database: SQLiteDatabase, reservation_id: UUID) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO reservations "
            "(id, bench_id, owner, created_at, status, requested_at, starts_at, ends_at, "
            "activated_at, source, metadata, release_pending) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, 'api', '{}', 0)",
            (
                str(reservation_id),
                BENCH_ID,
                "github-actions",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=1)).isoformat(),
                NOW.isoformat(),
            ),
        )


def test_v8_migration_exposes_every_distributed_table_and_uniqueness_index(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "shape.db")
    with database.transaction() as connection:
        assert SCHEMA_VERSION == 11
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "agent_connections",
            "global_benches",
            "bench_snapshots",
            "remote_commands",
            "remote_command_attempts",
            "distributed_operations",
            "distributed_ci_workflows",
            "reservation_leases",
            "reconciliation_reports",
            "protocol_message_journal",
            "remote_artifacts",
            "artifact_transfers",
            "artifact_transfer_attempts",
            "agent_timelines",
        } <= tables
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {
            "agent_connections_one_active",
            "reservation_leases_current_active",
            "agent_timelines_deduplication",
            "remote_commands_dispatch",
            "distributed_ci_workflows_route",
        } <= indexes
        command_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(remote_commands)")
        }
        assert {"idempotency_key", "attempt_count", "lease_version"} <= command_columns
        operation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(distributed_operations)")
        }
        assert "result_json" in operation_columns
    database.close()


def test_v7_upgrade_adds_operation_results_without_losing_existing_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "upgrade-v7.db"
        database = _database(path)
        _seed_agent(database)
        await _seed_bench(database)
        command = _command()
        operation = DistributedOperation(
            id=UUID(int=150),
            remote_command_id=command.id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            operation_type="RUN_WORKFLOW",
            created_at=NOW,
        )
        await SQLiteRemoteCommandRepository(database).create(command)
        await SQLiteDistributedOperationRepository(database).create(operation)
        database.close()

        with closing(sqlite3.connect(path)) as connection:
            connection.execute("ALTER TABLE distributed_operations DROP COLUMN result_json")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
            )
            connection.commit()

        upgraded = _database(path)
        with upgraded.transaction() as connection:
            assert (
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                == SCHEMA_VERSION
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(distributed_operations)")
            }
            assert "result_json" in columns
            assert (
                connection.execute(
                    "SELECT result_json FROM distributed_operations WHERE id = ?",
                    (str(operation.id),),
                ).fetchone()[0]
                is None
            )
        assert await SQLiteDistributedOperationRepository(upgraded).get(operation.id) == operation
        upgraded.close()

    asyncio.run(scenario())


def test_connection_heartbeat_cas_and_reconnect_fencing_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "connections.db"
        database = _database(path)
        _seed_agent(database)
        repository = SQLiteAgentConnectionRepository(database)
        first = AgentConnectionRecord(
            id=UUID(int=101),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            protocol_version="1.0",
            connected_at=NOW,
            last_heartbeat_at=NOW,
        )
        assert await repository.open(first) == first
        heartbeat = await repository.heartbeat(
            first.id,
            expected_last_sequence_number=0,
            last_sequence_number=1,
            heartbeat_at=NOW + timedelta(seconds=15),
            observed_clock_offset_seconds=0.25,
        )
        assert heartbeat is not None
        assert heartbeat.last_sequence_number == 1
        assert (
            await repository.heartbeat(
                first.id,
                expected_last_sequence_number=0,
                last_sequence_number=2,
                heartbeat_at=NOW + timedelta(seconds=30),
                observed_clock_offset_seconds=0,
            )
            is None
        )

        second = AgentConnectionRecord(
            id=UUID(int=102),
            agent_id=AGENT_ID,
            boot_id=uuid4(),
            protocol_version="1.0",
            connected_at=NOW + timedelta(seconds=30),
            last_heartbeat_at=NOW + timedelta(seconds=30),
        )
        await repository.open(second)
        fenced = await repository.get(first.id)
        assert fenced is not None
        assert fenced.disconnected_at == second.connected_at
        assert await repository.get_active(AGENT_ID) == second
        assert (
            await repository.heartbeat(
                first.id,
                expected_last_sequence_number=1,
                last_sequence_number=2,
                heartbeat_at=NOW + timedelta(seconds=45),
                observed_clock_offset_seconds=0,
            )
            is None
        )
        assert (
            await repository.disconnect(
                second.id,
                NOW + timedelta(seconds=40),
                expected_last_sequence_number=1,
            )
            is None
        )
        disconnected = await repository.disconnect(second.id, NOW + timedelta(seconds=40))
        assert disconnected is not None
        assert disconnected.disconnected_at == NOW + timedelta(seconds=40)
        database.close()

        reopened = _database(path)
        reloaded = await SQLiteAgentConnectionRepository(reopened).get(second.id)
        assert reloaded == disconnected
        reopened.close()

    asyncio.run(scenario())


def test_inventory_snapshot_is_atomic_idempotent_and_marks_missing_benches_offline(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "inventory.db")
        _seed_agent(database)
        repository = SQLiteGlobalBenchRepository(database)
        first = StoredBenchSnapshot(
            id=UUID(int=201),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            received_at=NOW,
            benches=(_bench(), _bench("esp32-02")),
        )
        assert len(await repository.save_snapshot(first)) == 2
        assert await repository.save_snapshot(first) == await repository.list(agent_id=AGENT_ID)
        assert await repository.get_snapshot(first.id) == first

        observed = NOW + timedelta(minutes=1)
        remaining = _bench("esp32-01", observed_at=observed)
        second = StoredBenchSnapshot(
            id=UUID(int=202),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=observed,
            received_at=observed,
            benches=(remaining,),
        )
        reconciled = await repository.save_snapshot(second)
        by_id = {bench.id: bench for bench in reconciled}
        assert by_id[BENCH_ID].status is GlobalBenchStatus.ONLINE
        assert by_id["home-lab/esp32-02"].status is GlobalBenchStatus.OFFLINE
        assert await repository.list(capability="firmware", labels={"board": "esp32"})

        stale = StoredBenchSnapshot(
            id=UUID(int=203),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            received_at=observed,
            benches=(_bench(),),
        )
        with pytest.raises(ValueError, match="stale"):
            await repository.save_snapshot(stale)
        offline = await repository.mark_agent_offline(AGENT_ID, observed + timedelta(seconds=10))
        assert all(item.status is GlobalBenchStatus.OFFLINE for item in offline)
        database.close()

    asyncio.run(scenario())


def test_remote_command_attempt_and_operation_updates_use_idempotency_and_status_cas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "commands.db"
        database = _database(path)
        _seed_agent(database)
        await _seed_bench(database)
        commands = SQLiteRemoteCommandRepository(database)
        operations = SQLiteDistributedOperationRepository(database)
        command = _command()
        assert await commands.create(command) == command

        retry = _command(id=uuid4(), created_at=NOW + timedelta(seconds=1))
        assert await commands.create(retry) == command
        with pytest.raises(ValueError, match="idempotency"):
            await commands.create(_command(id=uuid4(), payload={"workflow_name": "different"}))

        attempt = RemoteCommandAttempt(
            id=UUID(int=301),
            command_id=command.id,
            attempt_number=1,
            dispatched_at=NOW + timedelta(seconds=1),
        )
        assert await commands.add_attempt(attempt, expected_attempt_count=0) == attempt
        assert await commands.add_attempt(attempt, expected_attempt_count=0) == attempt
        with pytest.raises(ValueError, match="attempt number"):
            await commands.add_attempt(
                RemoteCommandAttempt(
                    id=UUID(int=302),
                    command_id=command.id,
                    attempt_number=2,
                    dispatched_at=NOW + timedelta(seconds=2),
                ),
                expected_attempt_count=0,
            )
        persisted = await commands.get(command.id)
        assert persisted is not None
        assert persisted.attempt_count == 1

        dispatched_at = NOW + timedelta(seconds=1)
        dispatched = RemoteCommand.model_validate(
            {
                **persisted.model_dump(),
                "status": RemoteCommandStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        )
        assert (
            await commands.update(dispatched, expected_status=RemoteCommandStatus.CREATED)
            == dispatched
        )
        assert (
            await commands.update(dispatched, expected_status=RemoteCommandStatus.CREATED) is None
        )
        with pytest.raises(ValueError, match="Invalid remote command transition"):
            await commands.update(
                RemoteCommand.model_validate(
                    {
                        **dispatched.model_dump(),
                        "status": RemoteCommandStatus.SUCCEEDED,
                        "completed_at": NOW + timedelta(seconds=3),
                    }
                ),
                expected_status=RemoteCommandStatus.DISPATCHED,
            )

        acknowledged_attempt = RemoteCommandAttempt.model_validate(
            {**attempt.model_dump(), "acknowledged_at": NOW + timedelta(seconds=2)}
        )
        assert await commands.finish_attempt(acknowledged_attempt) == acknowledged_attempt
        assert await commands.add_attempt(attempt, expected_attempt_count=0) == acknowledged_attempt
        assert await commands.list_attempts(command.id) == [acknowledged_attempt]

        operation = DistributedOperation(
            id=UUID(int=303),
            remote_command_id=command.id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            operation_type="RUN_WORKFLOW",
            result={"phase": "created"},
            created_at=NOW,
        )
        assert await operations.create(operation) == operation
        operation_retry = operation.model_copy(update={"id": uuid4()})
        assert await operations.create(operation_retry) == operation
        operation_dispatched = DistributedOperation.model_validate(
            {
                **operation.model_dump(),
                "status": DistributedOperationStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
                "result": {"phase": "dispatched"},
            }
        )
        assert (
            await operations.update(
                operation_dispatched,
                expected_status=DistributedOperationStatus.CREATED,
            )
            == operation_dispatched
        )
        assert await operations.list(status=DistributedOperationStatus.DISPATCHED) == [
            operation_dispatched
        ]
        with pytest.raises(ValueError, match="Invalid distributed operation transition"):
            await operations.update(
                DistributedOperation.model_validate(
                    {
                        **operation_dispatched.model_dump(),
                        "status": DistributedOperationStatus.SUCCEEDED,
                        "completed_at": NOW + timedelta(seconds=4),
                    }
                ),
                expected_status=DistributedOperationStatus.DISPATCHED,
            )

        accepted = DistributedOperation.model_validate(
            {
                **operation_dispatched.model_dump(),
                "status": DistributedOperationStatus.ACCEPTED,
                "last_agent_update_at": NOW + timedelta(seconds=2),
                "result": {"phase": "accepted"},
            }
        )
        assert (
            await operations.update(
                accepted,
                expected_status=DistributedOperationStatus.DISPATCHED,
            )
            == accepted
        )
        running = DistributedOperation.model_validate(
            {
                **accepted.model_dump(),
                "status": DistributedOperationStatus.RUNNING,
                "progress": 50,
                "started_at": NOW + timedelta(seconds=3),
                "last_agent_update_at": NOW + timedelta(seconds=3),
                "result": {"tests_completed": 5},
            }
        )
        assert (
            await operations.update(
                running,
                expected_status=DistributedOperationStatus.ACCEPTED,
            )
            == running
        )
        succeeded = DistributedOperation.model_validate(
            {
                **running.model_dump(),
                "status": DistributedOperationStatus.SUCCEEDED,
                "progress": 100,
                "completed_at": NOW + timedelta(seconds=4),
                "last_agent_update_at": NOW + timedelta(seconds=4),
                "result": {"tests": "passed", "count": 10},
            }
        )
        assert (
            await operations.update(
                succeeded,
                expected_status=DistributedOperationStatus.RUNNING,
            )
            == succeeded
        )
        database.close()

        restarted = _database(path)
        assert await SQLiteDistributedOperationRepository(restarted).get(operation.id) == succeeded
        restarted.close()

    asyncio.run(scenario())


def test_reservation_leases_are_monotonic_and_have_one_current_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "leases.db")
        _seed_agent(database)
        await _seed_bench(database)
        reservation_id = UUID(int=401)
        _seed_reservation(database, reservation_id)
        repository = SQLiteReservationLeaseRepository(database)
        first = ReservationLease(
            reservation_id=reservation_id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="github-actions",
            valid_from=NOW,
            valid_until=NOW + timedelta(minutes=10),
            lease_version=1,
        )
        assert await repository.put(first, expected_current_version=None) == first
        assert await repository.current(BENCH_ID) == first

        renewed = ReservationLease(
            reservation_id=reservation_id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="github-actions",
            valid_from=NOW + timedelta(minutes=5),
            valid_until=NOW + timedelta(minutes=15),
            lease_version=2,
        )
        assert await repository.put(renewed, expected_current_version=1) == renewed
        assert await repository.current(BENCH_ID) == renewed
        newer = renewed.model_copy(
            update={
                "lease_version": 3,
                "valid_from": NOW + timedelta(minutes=6),
                "valid_until": NOW + timedelta(minutes=16),
            }
        )
        assert await repository.put(newer, expected_current_version=1) is None
        released = await repository.release(
            reservation_id,
            2,
            NOW + timedelta(minutes=7),
        )
        assert released is not None
        assert released.released_at == NOW + timedelta(minutes=7)
        assert await repository.current(BENCH_ID) is None
        database.close()

    asyncio.run(scenario())


def test_reconciliation_protocol_deduplication_and_agent_timeline_are_durable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "journals.db"
        database = _database(path)
        _seed_agent(database)
        connections = SQLiteAgentConnectionRepository(database)
        connection_record = AgentConnectionRecord(
            id=UUID(int=501),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            protocol_version="1.0",
            connected_at=NOW,
            last_heartbeat_at=NOW,
        )
        await connections.open(connection_record)

        report = ReconciliationReport(
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            active_commands=(
                ReconciliationCommandState(
                    command_id=UUID(int=502),
                    status=RemoteCommandStatus.RUNNING,
                    updated_at=NOW,
                ),
            ),
            recent_commands=(),
            local_reservation_leases=(),
            bench_snapshots=(
                ReconciliationBenchSnapshot(
                    local_bench_id="esp32-01",
                    name="ESP32",
                    backend_id="hardware",
                    kind=GlobalBenchKind.PHYSICAL,
                    status=GlobalBenchStatus.ONLINE,
                    health=HealthStatus.HEALTHY,
                ),
            ),
            buffered_event_count=0,
        )
        reconciliation = SQLiteReconciliationRepository(database)
        stored = await reconciliation.save(UUID(int=503), report, NOW + timedelta(seconds=1))
        assert await reconciliation.latest(AGENT_ID) == stored
        assert await reconciliation.save(stored.id, report, stored.received_at) == stored

        protocol = SQLiteProtocolMessageJournalRepository(database)
        message = ProtocolMessageJournalRecord(
            message_id=UUID(int=504),
            agent_id=AGENT_ID,
            connection_id=connection_record.id,
            direction=ProtocolMessageDirection.AGENT_TO_CONTROL_PLANE,
            sequence_number=1,
            message_type="AGENT_HEARTBEAT",
            correlation_id=None,
            payload_sha256="a" * 64,
            observed_at=NOW,
            outcome=ProtocolMessageOutcome.RECEIVED,
        )
        assert await protocol.record(message) is ProtocolMessageRecordDisposition.RECORDED
        assert await protocol.record(message) is ProtocolMessageRecordDisposition.DUPLICATE
        replayed_on_new_connection = ProtocolMessageJournalRecord.model_validate(
            {
                **message.model_dump(),
                "connection_id": UUID(int=509),
                "sequence_number": 2,
            }
        )
        assert (
            await protocol.record(replayed_on_new_connection)
            is ProtocolMessageRecordDisposition.DUPLICATE
        )
        with pytest.raises(ValueError, match="reused"):
            await protocol.record(
                ProtocolMessageJournalRecord.model_validate(
                    {**message.model_dump(), "payload_sha256": "b" * 64}
                )
            )
        with pytest.raises(ValueError, match="sequence"):
            await protocol.record(
                ProtocolMessageJournalRecord.model_validate(
                    {**message.model_dump(), "message_id": UUID(int=505)}
                )
            )
        handled = await protocol.mark_handled(
            AGENT_ID,
            message.direction,
            message.message_id,
            handled_at=NOW + timedelta(seconds=2),
            outcome=ProtocolMessageOutcome.HANDLED,
        )
        assert handled is not None
        assert handled.outcome is ProtocolMessageOutcome.HANDLED

        timelines = SQLiteAgentTimelineRepository(database)
        entry = AgentTimelineRecord(
            id=UUID(int=506),
            agent_id=AGENT_ID,
            timestamp=NOW,
            event_type="AGENT_RECONNECTED",
            severity=AgentTimelineSeverity.INFO,
            message="Agent reconnected.",
            correlation_id=message.message_id,
            metadata={"boot_id": str(BOOT_ID)},
            deduplication_key="agent-reconnected:boot-1",
        )
        assert await timelines.append(entry) == entry
        assert await timelines.append(entry) == entry
        assert await timelines.append(entry.model_copy(update={"id": uuid4()})) == entry
        assert await timelines.list(AGENT_ID, severity=AgentTimelineSeverity.INFO) == [entry]
        database.close()

        reopened = _database(path)
        assert await SQLiteReconciliationRepository(reopened).latest(AGENT_ID) == stored
        assert await SQLiteAgentTimelineRepository(reopened).list(AGENT_ID) == [entry]
        reopened.close()

    asyncio.run(scenario())


def test_remote_artifact_transfer_and_attempt_lifecycle_survives_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "artifacts.db"
        database = _database(path)
        _seed_agent(database)
        await _seed_bench(database)
        command_repository = SQLiteRemoteCommandRepository(database)
        command = await command_repository.create(_command())
        artifacts = SQLiteRemoteArtifactRepository(database)
        artifact = RemoteArtifactMetadata(
            id=UUID(int=601),
            agent_id=AGENT_ID,
            local_artifact_id=UUID(int=602),
            command_id=command.id,
            name="hardware-results.xml",
            artifact_type="junit",
            content_type="application/xml",
            size_bytes=4096,
            sha256="c" * 64,
            created_at=NOW,
        )
        assert await artifacts.create(artifact) == artifact
        assert await artifacts.create(artifact) == artifact
        assert await artifacts.create(artifact.model_copy(update={"id": uuid4()})) == artifact
        uploaded = await artifacts.update_uploaded(
            artifact.id,
            NOW + timedelta(seconds=2),
            expected_uploaded_at=None,
        )
        assert uploaded is not None
        assert uploaded.uploaded_at == NOW + timedelta(seconds=2)
        assert (
            await artifacts.update_uploaded(
                artifact.id,
                NOW + timedelta(seconds=3),
                expected_uploaded_at=None,
            )
            is None
        )

        transfers = SQLiteArtifactTransferRepository(database)
        transfer = ArtifactTransferRecord(
            id=UUID(int=603),
            agent_id=AGENT_ID,
            artifact_id=artifact.id,
            direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
            status=ArtifactTransferStatus.PENDING,
            token_hash="d" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
        assert await transfers.create(transfer) == transfer
        started_attempt = ArtifactTransferAttempt(
            id=UUID(int=604),
            transfer_id=transfer.id,
            attempt_number=1,
            started_at=NOW + timedelta(seconds=1),
        )
        assert await transfers.add_attempt(started_attempt, expected_attempt_count=0) == (
            started_attempt
        )
        attempt = ArtifactTransferAttempt.model_validate(
            {
                **started_attempt.model_dump(),
                "completed_at": NOW + timedelta(seconds=2),
                "bytes_transferred": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
        )
        assert await transfers.finish_attempt(attempt) == attempt
        assert await transfers.finish_attempt(attempt) == attempt
        assert await transfers.add_attempt(started_attempt, expected_attempt_count=0) == attempt
        persisted = await transfers.get(transfer.id)
        assert persisted is not None
        assert persisted.attempt_count == 1

        in_progress = ArtifactTransferRecord.model_validate(
            {
                **persisted.model_dump(),
                "status": ArtifactTransferStatus.IN_PROGRESS,
            }
        )
        assert (
            await transfers.update(in_progress, expected_status=ArtifactTransferStatus.PENDING)
            == in_progress
        )
        completed = ArtifactTransferRecord.model_validate(
            {
                **in_progress.model_dump(),
                "status": ArtifactTransferStatus.COMPLETED,
                "completed_at": NOW + timedelta(seconds=3),
            }
        )
        assert (
            await transfers.update(completed, expected_status=ArtifactTransferStatus.IN_PROGRESS)
            == completed
        )
        assert await transfers.list_attempts(transfer.id) == [attempt]
        database.close()

        reopened = _database(path)
        assert await SQLiteRemoteArtifactRepository(reopened).get(artifact.id) == uploaded
        assert await SQLiteArtifactTransferRepository(reopened).get(transfer.id) == completed
        reopened.close()

    asyncio.run(scenario())
