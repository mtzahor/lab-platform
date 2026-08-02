from __future__ import annotations

import asyncio
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
    _as_utc,
    _dump_json_mapping,
)

AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")
OTHER_AGENT_ID = UUID("59d9f19a-75ac-4248-9ada-d31065a5f409")
BOOT_ID = UUID("1ab59e13-5517-482f-849c-2fe8145db25c")
NOW = datetime(2026, 7, 29, 10, tzinfo=UTC)
BENCH_ID = "coverage-lab/esp32-01"


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _seed_agent(database: SQLiteDatabase, agent_id: UUID = AGENT_ID) -> None:
    slug = "coverage-lab" if agent_id == AGENT_ID else "other-lab"
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, labels_json, "
            "registered_at, enrollment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(agent_id),
                slug,
                slug,
                "ONLINE",
                "0.6.0-alpha",
                "1.0",
                "{}",
                NOW.isoformat(),
                "ENROLLED",
            ),
        )


def _bench(
    local_id: str = "esp32-01",
    *,
    agent_id: UUID = AGENT_ID,
    agent_slug: str = "coverage-lab",
    observed_at: datetime = NOW,
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    health: HealthStatus = HealthStatus.HEALTHY,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"{agent_slug}/{local_id}",
        agent_id=agent_id,
        agent_slug=agent_slug,
        local_bench_id=local_id,
        name=local_id,
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        target_type="esp32-devkit-v1",
        status=status,
        health=health,
        capabilities=frozenset({"Firmware", "serial"}),
        labels={"board": "esp32", "rack": "a"},
        firmware_version="1.2.3",
        last_seen_at=observed_at,
        created_at=NOW,
        updated_at=observed_at,
    )


def _command(**updates: object) -> RemoteCommand:
    values: dict[str, object] = {
        "id": UUID(int=1001),
        "agent_id": AGENT_ID,
        "bench_id": BENCH_ID,
        "command_type": RemoteCommandType.PROBE,
        "payload": {"deep": True},
        "status": RemoteCommandStatus.CREATED,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "idempotency_key": "coverage-probe",
    }
    values.update(updates)
    return RemoteCommand.model_validate(values)


def _operation(command: RemoteCommand, **updates: object) -> DistributedOperation:
    values: dict[str, object] = {
        "id": UUID(int=1002),
        "remote_command_id": command.id,
        "agent_id": AGENT_ID,
        "bench_id": BENCH_ID,
        "operation_type": "PROBE",
        "created_at": NOW,
    }
    values.update(updates)
    return DistributedOperation.model_validate(values)


def _artifact(command: RemoteCommand, **updates: object) -> RemoteArtifactMetadata:
    values: dict[str, object] = {
        "id": UUID(int=1003),
        "agent_id": AGENT_ID,
        "local_artifact_id": UUID(int=1004),
        "command_id": command.id,
        "name": "results.xml",
        "artifact_type": "junit",
        "content_type": "application/xml",
        "size_bytes": 42,
        "sha256": "a" * 64,
        "created_at": NOW,
    }
    values.update(updates)
    return RemoteArtifactMetadata.model_validate(values)


def _transfer(artifact: RemoteArtifactMetadata, **updates: object) -> ArtifactTransferRecord:
    values: dict[str, object] = {
        "id": UUID(int=1005),
        "agent_id": AGENT_ID,
        "artifact_id": artifact.id,
        "direction": ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
        "status": ArtifactTransferStatus.PENDING,
        "token_hash": "b" * 64,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "expected_sha256": artifact.sha256,
        "expected_size_bytes": artifact.size_bytes,
    }
    values.update(updates)
    return ArtifactTransferRecord.model_validate(values)


def _report(**updates: object) -> ReconciliationReport:
    values: dict[str, object] = {
        "agent_id": AGENT_ID,
        "boot_id": BOOT_ID,
        "generated_at": NOW,
        "active_commands": (),
        "recent_commands": (),
        "local_reservation_leases": (),
        "bench_snapshots": (),
        "buffered_event_count": 0,
    }
    values.update(updates)
    return ReconciliationReport.model_validate(values)


def _seed_inventory(database: SQLiteDatabase) -> None:
    asyncio.run(SQLiteGlobalBenchRepository(database).upsert(_bench()))


def _seed_reservation(database: SQLiteDatabase, reservation_id: UUID) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO reservations "
            "(id, bench_id, owner, created_at, status, requested_at, starts_at, ends_at, "
            "activated_at, source, metadata, release_pending) "
            "VALUES (?, ?, 'coverage', ?, 'active', ?, ?, ?, ?, 'api', '{}', 0)",
            (
                str(reservation_id),
                BENCH_ID,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(hours=1)).isoformat(),
                NOW.isoformat(),
            ),
        )


def test_snapshot_validation_and_connection_rejection_branches(tmp_path: Path) -> None:
    other_bench = _bench(agent_id=OTHER_AGENT_ID)
    with pytest.raises(ValueError, match="another Agent"):
        StoredBenchSnapshot(
            id=uuid4(),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            received_at=NOW,
            benches=(other_bench,),
        )
    with pytest.raises(ValueError, match="duplicate"):
        StoredBenchSnapshot(
            id=uuid4(),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            received_at=NOW,
            benches=(_bench(), _bench()),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _as_utc(datetime(2026, 7, 29))

    async def scenario() -> None:
        database = _database(tmp_path / "connections-coverage.db")
        _seed_agent(database)
        repository = SQLiteAgentConnectionRepository(database)
        assert await repository.get(uuid4()) is None
        assert await repository.get_active(AGENT_ID) is None
        assert await repository.list() == []
        with pytest.raises(ValueError, match="positive"):
            await repository.list(limit=0)

        disconnected = AgentConnectionRecord(
            id=uuid4(),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            protocol_version="1.0",
            connected_at=NOW,
            last_heartbeat_at=NOW,
            disconnected_at=NOW,
        )
        with pytest.raises(ValueError, match="already be disconnected"):
            await repository.open(disconnected)

        first = disconnected.model_copy(update={"id": UUID(int=1101), "disconnected_at": None})
        assert await repository.open(first) == first
        assert await repository.open(first) == first
        conflicting = first.model_copy(update={"boot_id": uuid4()})
        with pytest.raises(ValueError, match="different state"):
            await repository.open(conflicting)
        assert await repository.list(agent_id=AGENT_ID, active_only=True, limit=1) == [first]

        assert (
            await repository.heartbeat(
                uuid4(),
                expected_last_sequence_number=0,
                last_sequence_number=1,
                heartbeat_at=NOW,
                observed_clock_offset_seconds=0,
            )
            is None
        )
        heartbeat = await repository.heartbeat(
            first.id,
            expected_last_sequence_number=0,
            last_sequence_number=2,
            heartbeat_at=NOW + timedelta(seconds=20),
            observed_clock_offset_seconds=0.5,
        )
        assert heartbeat is not None
        assert (
            await repository.heartbeat(
                first.id,
                expected_last_sequence_number=2,
                last_sequence_number=3,
                heartbeat_at=NOW + timedelta(seconds=10),
                observed_clock_offset_seconds=0,
            )
            is None
        )
        assert (
            await repository.heartbeat(
                first.id,
                expected_last_sequence_number=2,
                last_sequence_number=1,
                heartbeat_at=NOW + timedelta(seconds=30),
                observed_clock_offset_seconds=0,
            )
            is None
        )

        stale = AgentConnectionRecord(
            id=UUID(int=1102),
            agent_id=AGENT_ID,
            boot_id=uuid4(),
            protocol_version="1.0",
            connected_at=NOW + timedelta(seconds=10),
            last_heartbeat_at=NOW + timedelta(seconds=10),
        )
        with pytest.raises(ValueError, match="stale connection"):
            await repository.open(stale)
        assert await repository.disconnect(uuid4(), NOW + timedelta(seconds=30)) is None
        assert (
            await repository.disconnect(
                first.id,
                NOW + timedelta(seconds=30),
                expected_last_sequence_number=1,
            )
            is None
        )
        closed = await repository.disconnect(first.id, NOW + timedelta(seconds=30))
        assert closed is not None
        assert await repository.disconnect(first.id, NOW + timedelta(seconds=31)) is None
        database.close()

    asyncio.run(scenario())


def test_inventory_filters_identity_fencing_and_snapshot_edges(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "inventory-coverage.db")
        _seed_agent(database)
        _seed_agent(database, OTHER_AGENT_ID)
        repository = SQLiteGlobalBenchRepository(database)
        assert await repository.get("missing/bench") is None
        assert await repository.get_snapshot(uuid4()) is None
        with pytest.raises(ValueError, match="positive"):
            await repository.list(limit=-1)

        recent = _bench(observed_at=NOW + timedelta(minutes=2))
        degraded = _bench(
            "esp32-02",
            observed_at=NOW + timedelta(minutes=1),
            status=GlobalBenchStatus.DEGRADED,
            health=HealthStatus.WARNING,
        )
        await repository.upsert(recent)
        await repository.upsert(degraded)
        assert await repository.get(BENCH_ID) == recent
        assert await repository.list(
            agent_id=AGENT_ID,
            status=GlobalBenchStatus.ONLINE,
            kind=GlobalBenchKind.PHYSICAL,
            health=HealthStatus.HEALTHY,
        ) == [recent]
        assert await repository.list(capability="firmware", labels={"rack": "a"}, limit=1) == [
            recent
        ]
        assert await repository.list(capability="missing") == []
        assert await repository.list(labels={"rack": "missing"}) == []

        with pytest.raises(ValueError, match="Agent-local"):
            await repository.upsert(_bench(agent_slug="other-global"))
        with pytest.raises(ValueError, match="Global bench ID"):
            await repository.upsert(_bench(agent_id=OTHER_AGENT_ID))
        with pytest.raises(ValueError, match="stale bench"):
            await repository.upsert(_bench(observed_at=NOW + timedelta(minutes=1)))
        refreshed = recent.model_copy(
            update={"name": "refreshed", "updated_at": NOW + timedelta(minutes=3)}
        )
        assert await repository.upsert(refreshed) == refreshed

        snapshot = StoredBenchSnapshot(
            id=UUID(int=1201),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW + timedelta(minutes=4),
            received_at=NOW + timedelta(minutes=4),
            benches=(refreshed.model_copy(update={"updated_at": NOW + timedelta(minutes=4)}),),
        )
        await repository.save_snapshot(snapshot)
        changed = StoredBenchSnapshot(
            id=snapshot.id,
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=snapshot.generated_at,
            received_at=snapshot.received_at,
            benches=(),
        )
        with pytest.raises(ValueError, match="different inventory"):
            await repository.save_snapshot(changed)

        empty = StoredBenchSnapshot(
            id=UUID(int=1202),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW + timedelta(minutes=5),
            received_at=NOW + timedelta(minutes=5),
            benches=(),
        )
        assert all(
            bench.status is GlobalBenchStatus.OFFLINE
            for bench in await repository.save_snapshot(empty)
        )
        database.close()

    asyncio.run(scenario())


def test_command_and_operation_cas_failure_branches(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "commands-coverage.db")
        _seed_agent(database)
        await SQLiteGlobalBenchRepository(database).upsert(_bench())
        commands = SQLiteRemoteCommandRepository(database)
        operations = SQLiteDistributedOperationRepository(database)
        command = await commands.create(_command())

        assert await commands.get(uuid4()) is None
        assert await commands.get_by_idempotency_key(AGENT_ID, "missing") is None
        assert await commands.get_by_idempotency_key(AGENT_ID, command.idempotency_key) == command
        assert await commands.list(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            status=RemoteCommandStatus.CREATED,
        ) == [command]
        with pytest.raises(ValueError, match="positive"):
            await commands.list(limit=0)
        assert (
            await commands.update(
                command.model_copy(update={"id": uuid4()}),
                expected_status=RemoteCommandStatus.CREATED,
            )
            is None
        )
        assert (
            await commands.update(
                command,
                expected_status=RemoteCommandStatus.DISPATCHED,
            )
            is None
        )
        with pytest.raises(ValueError, match="immutable fields"):
            await commands.update(
                command.model_copy(
                    update={
                        "created_at": NOW + timedelta(seconds=1),
                        "expires_at": NOW + timedelta(minutes=11),
                    }
                ),
                expected_status=RemoteCommandStatus.CREATED,
            )
        with pytest.raises(ValueError, match="attempt_count"):
            await commands.update(
                command.model_copy(update={"attempt_count": 1}),
                expected_status=RemoteCommandStatus.CREATED,
            )
        assert (
            await commands.update(command, expected_status=RemoteCommandStatus.CREATED) == command
        )

        missing_attempt = RemoteCommandAttempt(
            id=uuid4(),
            command_id=uuid4(),
            attempt_number=1,
            dispatched_at=NOW,
        )
        assert await commands.add_attempt(missing_attempt, expected_attempt_count=0) is None
        with pytest.raises(ValueError, match="finished command attempt"):
            await commands.finish_attempt(missing_attempt)
        missing_finished = missing_attempt.model_copy(update={"failed_at": NOW})
        assert await commands.finish_attempt(missing_finished) is None

        attempt = RemoteCommandAttempt(
            id=UUID(int=1301),
            command_id=command.id,
            attempt_number=1,
            sequence_number=1,
            dispatched_at=NOW + timedelta(seconds=1),
        )
        await commands.add_attempt(attempt, expected_attempt_count=0)
        with pytest.raises(ValueError, match="bound to other state"):
            await commands.add_attempt(
                attempt.model_copy(update={"sequence_number": 2}),
                expected_attempt_count=0,
            )
        changed_finished = attempt.model_copy(
            update={"sequence_number": 2, "failed_at": NOW + timedelta(seconds=2)}
        )
        with pytest.raises(ValueError, match="immutable fields"):
            await commands.finish_attempt(changed_finished)
        failed = attempt.model_copy(
            update={"failed_at": NOW + timedelta(seconds=2), "error_code": "offline"}
        )
        assert await commands.finish_attempt(failed) == failed
        assert (
            await commands.finish_attempt(failed.model_copy(update={"error_code": "other"})) is None
        )

        operation = await operations.create(_operation(command))
        assert await operations.get(uuid4()) is None
        assert await operations.list(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            status=DistributedOperationStatus.CREATED,
        ) == [operation]
        with pytest.raises(ValueError, match="positive"):
            await operations.list(limit=0)
        with pytest.raises(ValueError, match="different state"):
            await operations.create(operation.model_copy(update={"operation_type": "RESET"}))
        assert (
            await operations.update(
                operation.model_copy(update={"id": uuid4()}),
                expected_status=DistributedOperationStatus.CREATED,
            )
            is None
        )
        assert (
            await operations.update(
                operation,
                expected_status=DistributedOperationStatus.RUNNING,
            )
            is None
        )
        with pytest.raises(ValueError, match="immutable fields"):
            await operations.update(
                operation.model_copy(update={"operation_type": "RESET"}),
                expected_status=DistributedOperationStatus.CREATED,
            )
        assert (
            await operations.update(
                operation,
                expected_status=DistributedOperationStatus.CREATED,
            )
            == operation
        )

        dispatched = operation.model_copy(
            update={
                "status": DistributedOperationStatus.DISPATCHED,
                "dispatched_at": NOW + timedelta(seconds=2),
                "last_agent_update_at": NOW + timedelta(seconds=3),
            }
        )
        assert (
            await operations.update(
                dispatched,
                expected_status=DistributedOperationStatus.CREATED,
            )
            == dispatched
        )
        stale = dispatched.model_copy(update={"last_agent_update_at": NOW + timedelta(seconds=2)})
        assert (
            await operations.update(
                stale,
                expected_status=DistributedOperationStatus.DISPATCHED,
            )
            is None
        )
        database.close()

    asyncio.run(scenario())


def test_lease_reconciliation_protocol_and_timeline_edges(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "journal-coverage.db")
        _seed_agent(database)
        await SQLiteGlobalBenchRepository(database).upsert(_bench())
        reservation_id = UUID(int=1401)
        _seed_reservation(database, reservation_id)
        leases = SQLiteReservationLeaseRepository(database)
        assert await leases.current(BENCH_ID) is None
        assert await leases.release(uuid4(), 1, NOW) is None
        lease = ReservationLease(
            reservation_id=reservation_id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="coverage",
            valid_from=NOW,
            valid_until=NOW + timedelta(minutes=10),
            lease_version=1,
        )
        assert await leases.put(lease, expected_current_version=99) is None
        assert await leases.put(lease) == lease
        with pytest.raises(ValueError, match="different state"):
            await leases.put(lease.model_copy(update={"owner": "someone-else"}))
        with pytest.raises(ValueError, match="Only active"):
            await leases.put(lease.model_copy(update={"released_at": NOW}))
        stale_renewal = lease.model_copy(
            update={
                "lease_version": 2,
                "valid_from": NOW - timedelta(seconds=1),
                "valid_until": NOW + timedelta(minutes=20),
            }
        )
        assert await leases.put(stale_renewal, expected_current_version=1) is None
        released = await leases.release(reservation_id, 1, NOW + timedelta(seconds=1))
        assert released is not None
        assert await leases.release(reservation_id, 1, NOW + timedelta(seconds=2)) == released

        second_reservation = UUID(int=1402)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reservations SET status = 'released', released_at = ? WHERE id = ?",
                ((NOW + timedelta(seconds=1)).isoformat(), str(reservation_id)),
            )
        _seed_reservation(database, second_reservation)
        reused_version = lease.model_copy(update={"reservation_id": second_reservation})
        assert await leases.put(reused_version) is None

        reconciliation = SQLiteReconciliationRepository(database)
        assert await reconciliation.latest(AGENT_ID) is None
        report = _report()
        report_id = UUID(int=1403)
        await reconciliation.save(report_id, report, NOW)
        with pytest.raises(ValueError, match="other state"):
            await reconciliation.save(
                report_id,
                _report(buffered_event_count=1),
                NOW + timedelta(seconds=1),
            )

        protocol = SQLiteProtocolMessageJournalRepository(database)
        message = ProtocolMessageJournalRecord(
            message_id=UUID(int=1404),
            agent_id=AGENT_ID,
            direction=ProtocolMessageDirection.AGENT_TO_CONTROL_PLANE,
            sequence_number=1,
            message_type="PING",
            payload_sha256="c" * 64,
            observed_at=NOW,
            outcome=ProtocolMessageOutcome.RECEIVED,
        )
        assert await protocol.get(AGENT_ID, message.direction, uuid4()) is None
        with pytest.raises(ValueError, match="Only HANDLED or REJECTED"):
            await protocol.mark_handled(
                AGENT_ID,
                message.direction,
                message.message_id,
                handled_at=NOW,
                outcome=ProtocolMessageOutcome.RECEIVED,
            )
        assert (
            await protocol.mark_handled(
                AGENT_ID,
                message.direction,
                message.message_id,
                handled_at=NOW,
                outcome=ProtocolMessageOutcome.HANDLED,
            )
            is None
        )
        await protocol.record(message)
        rejected = await protocol.mark_handled(
            AGENT_ID,
            message.direction,
            message.message_id,
            handled_at=NOW + timedelta(seconds=1),
            outcome=ProtocolMessageOutcome.REJECTED,
        )
        assert rejected is not None
        assert (
            await protocol.mark_handled(
                AGENT_ID,
                message.direction,
                message.message_id,
                handled_at=NOW + timedelta(seconds=2),
                outcome=ProtocolMessageOutcome.REJECTED,
            )
            == rejected
        )
        assert (
            await protocol.mark_handled(
                AGENT_ID,
                message.direction,
                message.message_id,
                handled_at=NOW + timedelta(seconds=2),
                outcome=ProtocolMessageOutcome.HANDLED,
            )
            is None
        )

        timelines = SQLiteAgentTimelineRepository(database)
        entry = AgentTimelineRecord(
            id=UUID(int=1405),
            agent_id=AGENT_ID,
            timestamp=NOW,
            event_type="CONNECTED",
            severity=AgentTimelineSeverity.WARNING,
            message="connected",
            metadata={"attempt": 1},
            deduplication_key="connection:1",
        )
        await timelines.append(entry)
        with pytest.raises(ValueError, match="deduplication key"):
            await timelines.append(entry.model_copy(update={"message": "changed"}))
        with pytest.raises(ValueError, match="deduplication key"):
            await timelines.append(entry.model_copy(update={"id": uuid4(), "message": "changed"}))
        assert await timelines.list(
            AGENT_ID,
            severity=AgentTimelineSeverity.WARNING,
            event_type="CONNECTED",
            since=NOW - timedelta(seconds=1),
        ) == [entry]
        assert await timelines.list(AGENT_ID, since=NOW + timedelta(seconds=1)) == []
        with pytest.raises(ValueError, match="positive"):
            await timelines.list(AGENT_ID, limit=0)
        database.close()

    asyncio.run(scenario())


def test_artifact_and_transfer_failure_branches(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "artifact-coverage.db")
        _seed_agent(database)
        await SQLiteGlobalBenchRepository(database).upsert(_bench())
        command = await SQLiteRemoteCommandRepository(database).create(_command())
        artifacts = SQLiteRemoteArtifactRepository(database)
        artifact = await artifacts.create(_artifact(command))
        assert await artifacts.get(uuid4()) is None
        assert await artifacts.list(agent_id=AGENT_ID, command_id=command.id) == [artifact]
        with pytest.raises(ValueError, match="positive"):
            await artifacts.list(limit=0)
        with pytest.raises(ValueError, match="other metadata"):
            await artifacts.create(artifact.model_copy(update={"name": "different.xml"}))
        assert await artifacts.update_uploaded(uuid4(), NOW) is None
        assert (
            await artifacts.update_uploaded(
                artifact.id,
                NOW + timedelta(seconds=1),
                expected_uploaded_at=NOW,
            )
            is None
        )

        transfers = SQLiteArtifactTransferRepository(database)
        transfer = await transfers.create(_transfer(artifact))
        assert await transfers.get(uuid4()) is None
        assert await transfers.list(
            agent_id=AGENT_ID,
            status=ArtifactTransferStatus.PENDING,
        ) == [transfer]
        with pytest.raises(ValueError, match="positive"):
            await transfers.list(limit=-1)
        with pytest.raises(ValueError, match="bound to other state"):
            await transfers.create(transfer.model_copy(update={"error_code": "different"}))
        assert (
            await transfers.update(
                transfer.model_copy(update={"id": uuid4()}),
                expected_status=ArtifactTransferStatus.PENDING,
            )
            is None
        )
        assert (
            await transfers.update(
                transfer,
                expected_status=ArtifactTransferStatus.IN_PROGRESS,
            )
            is None
        )
        with pytest.raises(ValueError, match="immutable fields"):
            await transfers.update(
                transfer.model_copy(update={"token_hash": "d" * 64}),
                expected_status=ArtifactTransferStatus.PENDING,
            )
        with pytest.raises(ValueError, match="attempt_count"):
            await transfers.update(
                transfer.model_copy(update={"attempt_count": 1}),
                expected_status=ArtifactTransferStatus.PENDING,
            )
        invalid_completed = transfer.model_copy(
            update={
                "status": ArtifactTransferStatus.COMPLETED,
                "completed_at": NOW + timedelta(seconds=1),
            }
        )
        with pytest.raises(ValueError, match="Invalid artifact transfer transition"):
            await transfers.update(
                invalid_completed,
                expected_status=ArtifactTransferStatus.PENDING,
            )
        assert (
            await transfers.update(
                transfer,
                expected_status=ArtifactTransferStatus.PENDING,
            )
            == transfer
        )

        missing_attempt = ArtifactTransferAttempt(
            id=uuid4(),
            transfer_id=uuid4(),
            attempt_number=1,
            started_at=NOW,
        )
        assert await transfers.add_attempt(missing_attempt, expected_attempt_count=0) is None
        with pytest.raises(ValueError, match="attempt number"):
            await transfers.add_attempt(
                missing_attempt.model_copy(update={"attempt_number": 2}),
                expected_attempt_count=0,
            )
        with pytest.raises(ValueError, match="requires completed_at"):
            await transfers.finish_attempt(missing_attempt)
        assert (
            await transfers.finish_attempt(missing_attempt.model_copy(update={"completed_at": NOW}))
            is None
        )

        attempt = ArtifactTransferAttempt(
            id=UUID(int=1501),
            transfer_id=transfer.id,
            attempt_number=1,
            started_at=NOW,
        )
        await transfers.add_attempt(attempt, expected_attempt_count=0)
        with pytest.raises(ValueError, match="bound to other state"):
            await transfers.add_attempt(
                attempt.model_copy(update={"started_at": NOW + timedelta(seconds=1)}),
                expected_attempt_count=0,
            )
        changed = attempt.model_copy(
            update={
                "started_at": NOW + timedelta(seconds=1),
                "completed_at": NOW + timedelta(seconds=2),
            }
        )
        with pytest.raises(ValueError, match="immutable fields"):
            await transfers.finish_attempt(changed)
        finished = attempt.model_copy(update={"completed_at": NOW + timedelta(seconds=1)})
        assert await transfers.finish_attempt(finished) == finished
        assert (
            await transfers.finish_attempt(finished.model_copy(update={"bytes_transferred": 1}))
            is None
        )
        database.close()

    asyncio.run(scenario())


def test_corrupted_rows_and_json_serialization_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be an object"):
        _dump_json_mapping([], field="payload", maximum_bytes=10)
    with pytest.raises(ValueError, match="JSON-compatible"):
        _dump_json_mapping({"bad": {1}}, field="payload", maximum_bytes=100)
    with pytest.raises(ValueError, match="exceeds"):
        _dump_json_mapping({"large": "x" * 20}, field="payload", maximum_bytes=5)

    async def scenario() -> None:
        database = _database(tmp_path / "corruption-coverage.db")
        _seed_agent(database)
        _seed_agent(database, OTHER_AGENT_ID)
        benches = SQLiteGlobalBenchRepository(database)
        await benches.upsert(_bench())
        with database.transaction(immediate=True) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE global_benches SET capabilities_json = '{}' WHERE id = ?",
                (BENCH_ID,),
            )
        with pytest.raises(ValueError, match="invalid shape"):
            await benches.get(BENCH_ID)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE global_benches SET capabilities_json = '[]', labels_json = '[]' "
                "WHERE id = ?",
                (BENCH_ID,),
            )
        with pytest.raises(ValueError, match="invalid shape"):
            await benches.get(BENCH_ID)

        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE global_benches SET labels_json = '{}' WHERE id = ?",
                (BENCH_ID,),
            )
        snapshot = StoredBenchSnapshot(
            id=UUID(int=1601),
            agent_id=AGENT_ID,
            boot_id=BOOT_ID,
            generated_at=NOW,
            received_at=NOW,
            benches=(_bench(),),
        )
        await benches.save_snapshot(snapshot)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE bench_snapshots SET snapshot_json = '{}' WHERE id = ?",
                (str(snapshot.id),),
            )
        with pytest.raises(ValueError, match="must be an array"):
            await benches.get_snapshot(snapshot.id)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE bench_snapshots SET snapshot_json = '[]', bench_count = 1 WHERE id = ?",
                (str(snapshot.id),),
            )
        with pytest.raises(ValueError, match="count"):
            await benches.get_snapshot(snapshot.id)

        report_id = UUID(int=1602)
        reports = SQLiteReconciliationRepository(database)
        report = _report()
        await reports.save(report_id, report, NOW)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reconciliation_reports SET report_json = '[]' WHERE id = ?",
                (str(report_id),),
            )
        with pytest.raises(ValueError, match="must be an object"):
            await reports.latest(AGENT_ID)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reconciliation_reports SET report_json = ?, agent_id = ? WHERE id = ?",
                (report.model_dump_json(), str(OTHER_AGENT_ID), str(report_id)),
            )
        with pytest.raises(ValueError, match="identity columns"):
            await reports.latest(OTHER_AGENT_ID)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reconciliation_reports SET agent_id = ?, generated_at = ? WHERE id = ?",
                (str(AGENT_ID), (NOW + timedelta(seconds=1)).isoformat(), str(report_id)),
            )
        with pytest.raises(ValueError, match="timestamp columns"):
            await reports.latest(AGENT_ID)

        command = await SQLiteRemoteCommandRepository(database).create(_command())
        operation = await SQLiteDistributedOperationRepository(database).create(_operation(command))
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE distributed_operations SET result_json = '[]' WHERE id = ?",
                (str(operation.id),),
            )
        with pytest.raises(ValueError, match="result must be an object"):
            await SQLiteDistributedOperationRepository(database).get(operation.id)

        timeline = AgentTimelineRecord(
            id=UUID(int=1603),
            agent_id=AGENT_ID,
            timestamp=NOW,
            event_type="CORRUPTED",
            message="corruption test",
        )
        timeline_repository = SQLiteAgentTimelineRepository(database)
        await timeline_repository.append(timeline)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agent_timelines SET metadata_json = '[]' WHERE id = ?",
                (str(timeline.id),),
            )
        with pytest.raises(ValueError, match="metadata must be an object"):
            await timeline_repository.list(AGENT_ID)
        database.close()

    asyncio.run(scenario())
