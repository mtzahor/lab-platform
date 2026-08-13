from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from lab_platform.control_plane_core.drain import AgentDrainSnapshot, AgentWorkload
from lab_platform.control_plane_core.inventory import InventoryReconciliation
from lab_platform.control_plane_core.presence import ActiveAgentConnection
from lab_platform.control_plane_core.reconciliation import (
    AgentBootContext,
    ReconciliationResult,
    ReconciliationService,
    ReportClaim,
    ReportClaimStatus,
)
from lab_platform.models import (
    AgentConnectionRecord,
    AgentRecord,
    AgentStatus,
    ArtifactTransferAttempt,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchRecord,
    GlobalBenchStatus,
    ProtocolMessageJournalRecord,
    ReconciliationBenchSnapshot,
    ReconciliationReport,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandAttempt,
    RemoteCommandStatus,
    ReservationLease,
)
from lab_platform.persistence.agents import (
    SQLiteAgentEnrollmentRepository,
    _agent_from_row,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.distributed import (
    ProtocolMessageRecordDisposition,
    SQLiteArtifactTransferRepository,
    SQLiteDistributedOperationRepository,
    SQLiteProtocolMessageJournalRepository,
    SQLiteReconciliationRepository,
    SQLiteRemoteArtifactRepository,
    SQLiteRemoteCommandRepository,
    SQLiteReservationLeaseRepository,
    StoredBenchSnapshot,
    _bench_from_row,
    _command_from_row,
    _command_values,
    _connection_from_row,
    _connection_values,
    _lease_from_row,
    _operation_from_row,
    _operation_values,
    _require_command_route,
    _require_same_command_request,
    _upsert_bench,
)


class SQLiteAgentPresenceAdapter:
    """Atomic Agent/connection adapter for the presence service."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._agents = SQLiteAgentEnrollmentRepository(database)
        self._monotonic: dict[UUID, float] = {}
        self._lock = asyncio.Lock()

    async def add_agent(self, agent: AgentRecord) -> AgentRecord:
        existing = await self._agents.get_agent(agent.id)
        if existing is None:
            raise ValueError("Agents must be created through one-time enrollment")
        if existing.id != agent.id or existing.slug != agent.slug:
            raise ValueError("Agent identity conflicts with the enrolled record")
        return existing

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord | None:
        return await self._agents.get_agent(agent_id, organisation_id=organisation_id)

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[AgentRecord]:
        return await self._agents.list_agents(
            organisation_id=organisation_id,
            limit=10_000,
        )

    async def get_connection(self, connection_id: UUID) -> ActiveAgentConnection | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE id = ?",
                (str(connection_id),),
            ).fetchone()
        if row is None:
            return None
        record = _connection_from_row(row)
        return ActiveAgentConnection(record, self._monotonic.get(record.id, 0.0))

    async def get_active_connection(self, agent_id: UUID) -> ActiveAgentConnection | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE agent_id = ? AND disconnected_at IS NULL",
                (str(agent_id),),
            ).fetchone()
        if row is None:
            return None
        record = _connection_from_row(row)
        return ActiveAgentConnection(record, self._monotonic.get(record.id, 0.0))

    async def list_active_connections(self) -> list[ActiveAgentConnection]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_connections WHERE disconnected_at IS NULL "
                "ORDER BY agent_id, connected_at"
            ).fetchall()
        return [
            ActiveAgentConnection(
                _connection_from_row(row),
                self._monotonic.get(UUID(row["id"]), 0.0),
            )
            for row in rows
        ]

    async def activate(
        self,
        agent: AgentRecord,
        active: ActiveAgentConnection,
    ) -> ActiveAgentConnection | None:
        record = active.connection
        async with self._lock:
            with self._database.transaction(immediate=True) as connection:
                agent_row = connection.execute(
                    "SELECT * FROM agents WHERE id = ?",
                    (str(agent.id),),
                ).fetchone()
                if agent_row is None:
                    raise ValueError("Agent does not exist")
                enrolled = _agent_from_row(agent_row)
                if enrolled.slug != agent.slug:
                    raise ValueError("Agent identity conflicts with enrollment")
                same = connection.execute(
                    "SELECT * FROM agent_connections WHERE id = ?",
                    (str(record.id),),
                ).fetchone()
                if same is not None:
                    existing = _connection_from_row(same)
                    if existing != record:
                        raise ValueError("Connection ID is already bound to different state")
                    return None
                prior_row = connection.execute(
                    "SELECT * FROM agent_connections WHERE agent_id = ? "
                    "AND disconnected_at IS NULL",
                    (str(agent.id),),
                ).fetchone()
                prior: AgentConnectionRecord | None = None
                if prior_row is not None:
                    prior = _connection_from_row(prior_row)
                    disconnected_at = max(record.connected_at, prior.last_heartbeat_at)
                    connection.execute(
                        "UPDATE agent_connections SET disconnected_at = ? "
                        "WHERE id = ? AND disconnected_at IS NULL",
                        (disconnected_at.isoformat(), str(prior.id)),
                    )
                _update_agent(connection, agent)
                connection.execute(
                    "INSERT INTO agent_connections "
                    "(id, agent_id, boot_id, protocol_version, connected_at, "
                    "last_heartbeat_at, disconnected_at, last_sequence_number, "
                    "observed_clock_offset_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _connection_values(record),
                )
            self._monotonic[record.id] = active.last_activity_monotonic
            if prior is None:
                return None
            return ActiveAgentConnection(prior, self._monotonic.pop(prior.id, 0.0))

    async def update_if_current(
        self,
        agent: AgentRecord,
        active: ActiveAgentConnection,
    ) -> bool:
        record = active.connection
        async with self._lock:
            with self._database.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM agent_connections WHERE id = ? AND agent_id = ? "
                    "AND boot_id = ? AND disconnected_at IS NULL",
                    (str(record.id), str(agent.id), str(record.boot_id)),
                ).fetchone()
                if row is None:
                    return False
                current = _connection_from_row(row)
                previous_monotonic = self._monotonic.get(record.id, 0.0)
                if (
                    record.last_sequence_number < current.last_sequence_number
                    or record.last_heartbeat_at < current.last_heartbeat_at
                    or active.last_activity_monotonic < previous_monotonic
                ):
                    return False
                cursor = connection.execute(
                    "UPDATE agent_connections SET last_heartbeat_at = ?, "
                    "last_sequence_number = ?, observed_clock_offset_seconds = ? "
                    "WHERE id = ? AND boot_id = ? AND disconnected_at IS NULL "
                    "AND last_sequence_number = ?",
                    (
                        record.last_heartbeat_at.isoformat(),
                        record.last_sequence_number,
                        record.observed_clock_offset_seconds,
                        str(record.id),
                        str(record.boot_id),
                        current.last_sequence_number,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                _update_agent(connection, agent)
            self._monotonic[record.id] = active.last_activity_monotonic
            return True

    async def disconnect_if_current(
        self,
        agent: AgentRecord,
        active: ActiveAgentConnection,
    ) -> bool:
        record = active.connection
        if record.disconnected_at is None:
            raise ValueError("Disconnected record must include disconnected_at")
        async with self._lock:
            with self._database.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT * FROM agent_connections WHERE id = ? AND agent_id = ? "
                    "AND boot_id = ? AND disconnected_at IS NULL",
                    (str(record.id), str(agent.id), str(record.boot_id)),
                ).fetchone()
                if row is None:
                    return False
                current = _connection_from_row(row)
                if current.last_sequence_number != record.last_sequence_number:
                    return False
                cursor = connection.execute(
                    "UPDATE agent_connections SET disconnected_at = ? WHERE id = ? "
                    "AND boot_id = ? AND disconnected_at IS NULL AND last_sequence_number = ?",
                    (
                        record.disconnected_at.isoformat(),
                        str(record.id),
                        str(record.boot_id),
                        record.last_sequence_number,
                    ),
                )
                if cursor.rowcount != 1:
                    return False
                _update_agent(connection, agent)
            self._monotonic.pop(record.id, None)
            return True


class SQLiteInventoryAdapter:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (bench_id, str(organisation_id)) if organisation_id is not None else (bench_id,)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM global_benches WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _bench_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[GlobalBenchRecord]:
        with self._database.transaction() as connection:
            if organisation_id is None:
                rows = connection.execute("SELECT * FROM global_benches ORDER BY id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM global_benches WHERE organisation_id = ? ORDER BY id",
                    (str(organisation_id),),
                ).fetchall()
        return [_bench_from_row(row) for row in rows]

    async def reconcile_agent_snapshot(
        self,
        agent: AgentRecord,
        benches: tuple[GlobalBenchRecord, ...],
        *,
        observed_at: datetime,
        snapshot_id: UUID | None = None,
        boot_id: UUID | None = None,
        generated_at: datetime | None = None,
    ) -> InventoryReconciliation:
        snapshot: StoredBenchSnapshot | None = None
        snapshot_json: str | None = None
        snapshot_fields = (snapshot_id, boot_id, generated_at)
        if any(value is not None for value in snapshot_fields):
            if not all(value is not None for value in snapshot_fields):
                raise ValueError("Snapshot identity fields must be provided together")
            assert snapshot_id is not None and boot_id is not None and generated_at is not None
            snapshot = StoredBenchSnapshot(
                id=snapshot_id,
                agent_id=agent.id,
                boot_id=boot_id,
                generated_at=generated_at,
                received_at=observed_at,
                benches=benches,
            )
            snapshot_json = json.dumps(
                [item.model_dump(mode="json") for item in snapshot.benches],
                sort_keys=True,
                separators=(",", ":"),
            )
        with self._database.transaction(immediate=True) as connection:
            if snapshot is not None:
                existing_snapshot = connection.execute(
                    "SELECT organisation_id, agent_id, boot_id, generated_at, snapshot_json "
                    "FROM bench_snapshots WHERE id = ?",
                    (str(snapshot.id),),
                ).fetchone()
                if existing_snapshot is not None:
                    if (
                        existing_snapshot["organisation_id"] != str(agent.organisation_id)
                        or existing_snapshot["agent_id"] != str(snapshot.agent_id)
                        or existing_snapshot["boot_id"] != str(snapshot.boot_id)
                        or existing_snapshot["generated_at"] != snapshot.generated_at.isoformat()
                        or existing_snapshot["snapshot_json"] != snapshot_json
                    ):
                        raise ValueError("Snapshot ID is already bound to different inventory")
                    current_rows = connection.execute(
                        "SELECT * FROM global_benches WHERE agent_id = ? "
                        "AND organisation_id = ? ORDER BY id",
                        (str(agent.id), str(agent.organisation_id)),
                    ).fetchall()
                    return InventoryReconciliation(
                        benches=tuple(_bench_from_row(row) for row in current_rows),
                        added_ids=frozenset(),
                        updated_ids=frozenset(),
                        offline_ids=frozenset(),
                    )
                latest_snapshot = connection.execute(
                    "SELECT generated_at FROM bench_snapshots WHERE agent_id = ? "
                    "AND organisation_id = ? "
                    "ORDER BY generated_at DESC, received_at DESC LIMIT 1",
                    (str(agent.id), str(agent.organisation_id)),
                ).fetchone()
                if (
                    latest_snapshot is not None
                    and datetime.fromisoformat(latest_snapshot["generated_at"])
                    > snapshot.generated_at
                ):
                    raise ValueError("A stale inventory snapshot cannot replace newer state")
            before_rows = connection.execute(
                "SELECT * FROM global_benches WHERE agent_id = ? "
                "AND organisation_id = ? ORDER BY id",
                (str(agent.id), str(agent.organisation_id)),
            ).fetchall()
            before = {row["id"]: _bench_from_row(row) for row in before_rows}
            incoming_ids: set[str] = set()
            for bench in benches:
                if (
                    bench.agent_id != agent.id
                    or bench.agent_slug != agent.slug
                    or bench.organisation_id != agent.organisation_id
                ):
                    raise ValueError("Bench snapshot identity does not match Agent")
                _upsert_bench(connection, bench)
                incoming_ids.add(bench.id)
            missing = set(before) - incoming_ids
            if missing:
                placeholders = ",".join("?" for _ in missing)
                connection.execute(
                    "UPDATE global_benches SET status = 'OFFLINE', updated_at = ? "
                    f"WHERE agent_id = ? AND organisation_id = ? AND id IN ({placeholders}) "  # noqa: S608
                    "AND status != 'OFFLINE'",
                    (
                        observed_at.isoformat(),
                        str(agent.id),
                        str(agent.organisation_id),
                        *sorted(missing),
                    ),
                )
            after_rows = connection.execute(
                "SELECT * FROM global_benches WHERE agent_id = ? "
                "AND organisation_id = ? ORDER BY id",
                (str(agent.id), str(agent.organisation_id)),
            ).fetchall()
            after = {row["id"]: _bench_from_row(row) for row in after_rows}
            if snapshot is not None:
                assert snapshot_json is not None
                connection.execute(
                    "INSERT INTO bench_snapshots "
                    "(id, organisation_id, agent_id, boot_id, generated_at, received_at, "
                    "bench_count, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(snapshot.id),
                        str(agent.organisation_id),
                        str(snapshot.agent_id),
                        str(snapshot.boot_id),
                        snapshot.generated_at.isoformat(),
                        snapshot.received_at.isoformat(),
                        len(snapshot.benches),
                        snapshot_json,
                    ),
                )
        added = frozenset(set(after) - set(before))
        updated = frozenset(
            bench_id
            for bench_id in set(after) & set(before)
            if after[bench_id] != before[bench_id] and bench_id not in missing
        )
        offlined = frozenset(
            bench_id
            for bench_id in missing
            if before[bench_id].status is not GlobalBenchStatus.OFFLINE
        )
        return InventoryReconciliation(
            benches=tuple(after.values()),
            added_ids=added,
            updated_ids=updated,
            offline_ids=offlined,
        )

    async def mark_agent_offline(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
        organisation_id: UUID | None = None,
    ) -> tuple[GlobalBenchRecord, ...]:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        update_values: list[object] = [observed_at.isoformat(), str(agent_id)]
        select_values: list[object] = [str(agent_id)]
        if organisation_id is not None:
            update_values.append(str(organisation_id))
            select_values.append(str(organisation_id))
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE global_benches SET status = 'OFFLINE', updated_at = ? "
                f"WHERE agent_id = ? AND status != 'OFFLINE'{scope}",  # noqa: S608
                update_values,
            )
            rows = connection.execute(
                f"SELECT * FROM global_benches WHERE agent_id = ?{scope} ORDER BY id",  # noqa: S608
                select_values,
            ).fetchall()
        return tuple(_bench_from_row(row) for row in rows)


class SQLiteDistributedDirectory:
    def __init__(
        self,
        presence: SQLiteAgentPresenceAdapter,
        inventory: SQLiteInventoryAdapter,
    ) -> None:
        self._presence = presence
        self._inventory = inventory

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return await self._presence.get_agent(agent_id)

    async def get_bench(self, bench_id: str) -> GlobalBenchRecord | None:
        return await self._inventory.get(bench_id)


class SQLiteRemoteCommandServiceRepository:
    """Combined command/operation adapter with atomic initial creation."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._commands = SQLiteRemoteCommandRepository(database)
        self._operations = SQLiteDistributedOperationRepository(database)

    async def create_bundle(
        self,
        command: RemoteCommand,
        operation: DistributedOperation | None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        with self._database.transaction(immediate=True) as connection:
            _require_command_route(connection, command)
            row = connection.execute(
                "SELECT * FROM remote_commands WHERE id = ? OR "
                "(agent_id = ? AND idempotency_key = ?) LIMIT 1",
                (str(command.id), str(command.agent_id), command.idempotency_key),
            ).fetchone()
            if row is not None:
                existing = _command_from_row(row)
                _require_same_command_request(existing, command)
                operation_row = connection.execute(
                    "SELECT * FROM distributed_operations WHERE remote_command_id = ?",
                    (str(existing.id),),
                ).fetchone()
                return (
                    existing,
                    _operation_from_row(operation_row) if operation_row is not None else None,
                )
            if operation is not None and (
                operation.remote_command_id != command.id
                or command.operation_id != operation.id
                or operation.organisation_id != command.organisation_id
                or operation.agent_id != command.agent_id
                or operation.bench_id != command.bench_id
            ):
                raise ValueError("Command and distributed operation identities differ")
            connection.execute(
                "INSERT INTO remote_commands "
                "(id, organisation_id, agent_id, bench_id, command_type, payload_json, status, "
                "created_at, "
                "dispatched_at, acknowledged_at, started_at, completed_at, expires_at, "
                "idempotency_key, attempt_count, operation_id, reservation_id, lease_version, "
                "error_code, error_message, actor_context_json, authorisation_snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _command_values(command),
            )
            if operation is not None:
                connection.execute(
                    "INSERT INTO distributed_operations "
                    "(id, organisation_id, remote_command_id, agent_id, bench_id, reservation_id, "
                    "operation_type, status, progress, message, result_json, created_at, "
                    "dispatched_at, started_at, completed_at, last_agent_update_at, "
                    "reconciliation_deadline, "
                    "error_code, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?)",
                    _operation_values(operation),
                )
        return command, operation

    async def get_command(
        self,
        command_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteCommand | None:
        return await self._commands.get(command_id, organisation_id=organisation_id)

    async def get_by_idempotency_key(
        self,
        agent_id: UUID,
        idempotency_key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteCommand | None:
        return await self._commands.get_by_idempotency_key(
            agent_id,
            idempotency_key,
            organisation_id=organisation_id,
        )

    async def update_command(
        self,
        command: RemoteCommand,
        *,
        expected_statuses: Iterable[RemoteCommandStatus],
    ) -> RemoteCommand | None:
        current = await self._commands.get(command.id)
        expected = frozenset(expected_statuses)
        if current is None or current.status not in expected:
            return None
        return await self._commands.update(command, expected_status=current.status)

    async def get_operation_for_command(
        self,
        command_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(command_id), str(organisation_id))
            if organisation_id is not None
            else (str(command_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM distributed_operations WHERE remote_command_id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _operation_from_row(row) if row is not None else None

    async def update_operation(
        self,
        operation: DistributedOperation,
        *,
        expected_statuses: Iterable[DistributedOperationStatus],
    ) -> DistributedOperation | None:
        current = await self._operations.get(operation.id)
        expected = frozenset(expected_statuses)
        if current is None or current.status not in expected:
            return None
        return await self._operations.update(operation, expected_status=current.status)

    async def list_operations(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None,
        statuses: Iterable[DistributedOperationStatus],
        limit: int,
    ) -> list[DistributedOperation]:
        selected = frozenset(statuses)
        with self._database.transaction() as connection:
            conditions: list[str] = []
            values: list[object] = []
            if organisation_id is not None:
                conditions.append("organisation_id = ?")
                values.append(str(organisation_id))
            if agent_id is not None:
                conditions.append("agent_id = ?")
                values.append(str(agent_id))
            if selected:
                placeholders = ",".join("?" for _ in selected)
                conditions.append(f"status IN ({placeholders})")
                values.extend(sorted(status.value for status in selected))
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            values.append(limit)
            rows = connection.execute(
                f"SELECT * FROM distributed_operations{where} "  # noqa: S608
                "ORDER BY created_at, id LIMIT ?",
                values,
            ).fetchall()
        return [_operation_from_row(row) for row in rows]

    async def record_attempt(self, attempt: RemoteCommandAttempt) -> RemoteCommandAttempt:
        recorded = await self._commands.add_attempt(
            attempt,
            expected_attempt_count=attempt.attempt_number - 1,
        )
        if recorded is None:
            raise ValueError("Remote command attempt compare-and-set failed")
        return recorded

    async def acknowledge_latest_attempt(
        self,
        command_id: UUID,
        acknowledged_at: datetime,
    ) -> None:
        attempts = await self._commands.list_attempts(command_id)
        if not attempts:
            return
        latest = attempts[-1]
        if latest.acknowledged_at is not None or latest.failed_at is not None:
            return
        await self._commands.finish_attempt(
            RemoteCommandAttempt.model_validate(
                {**latest.model_dump(), "acknowledged_at": acknowledged_at}
            )
        )

    async def list_commands(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        statuses: Iterable[RemoteCommandStatus] | None = None,
        limit: int = 500,
    ) -> list[RemoteCommand]:
        selected = frozenset(statuses) if statuses is not None else None
        with self._database.transaction() as connection:
            conditions: list[str] = []
            values: list[object] = []
            if organisation_id is not None:
                conditions.append("organisation_id = ?")
                values.append(str(organisation_id))
            if agent_id is not None:
                conditions.append("agent_id = ?")
                values.append(str(agent_id))
            if selected:
                placeholders = ",".join("?" for _ in selected)
                conditions.append(f"status IN ({placeholders})")
                values.extend(sorted(status.value for status in selected))
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            values.append(limit)
            rows = connection.execute(
                f"SELECT * FROM remote_commands{where} "  # noqa: S608
                "ORDER BY created_at, id LIMIT ?",
                values,
            ).fetchall()
        return [_command_from_row(row) for row in rows]

    async def list_terminal_workflow_commands(
        self,
        *,
        limit: int,
    ) -> list[RemoteCommand]:
        """Return only terminal work whose current reservation still needs cleanup.

        Filtering the durable reservation state in SQL prevents completed history
        from filling a bounded monitor batch and starving newer terminal workflows.
        Either the command or its operation may be the terminal witness because the
        two records are deliberately updated with independent compare-and-set writes.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT command.* FROM remote_commands AS command "
                "JOIN coordinated_reservation_leases AS reservation "
                "ON reservation.reservation_id = command.reservation_id "
                "LEFT JOIN distributed_operations AS operation "
                "ON operation.remote_command_id = command.id "
                "WHERE command.command_type = 'RUN_WORKFLOW' "
                "AND reservation.state NOT IN ('RELEASED', 'EXPIRED', 'REVOKED') "
                "AND json_extract("
                "reservation.record_json, "
                "'$.reservation.metadata.reservation_lifecycle'"
                ") = 'workflow' "
                "AND command.lease_version = reservation.lease_version "
                "AND command.agent_id = reservation.agent_id "
                "AND command.bench_id = reservation.bench_id "
                "AND (command.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED') "
                "OR operation.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')) "
                "ORDER BY command.created_at, command.id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_command_from_row(row) for row in rows]


class SQLiteReconciliationReportStore:
    """Durable report-id fencing and content-level deduplication."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def claim_report(
        self,
        report_id: UUID,
        *,
        agent_id: UUID,
        content_digest: str,
        received_at: datetime,
    ) -> ReportClaim:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_report_claims WHERE report_id = ?",
                (str(report_id),),
            ).fetchone()
            if row is not None:
                if row["agent_id"] != str(agent_id) or row["content_digest"] != content_digest:
                    return ReportClaim(ReportClaimStatus.CONFLICT)
                if row["status"] == "PROCESSING":
                    return ReportClaim(ReportClaimStatus.IN_PROGRESS)
                return ReportClaim(
                    ReportClaimStatus.DUPLICATE,
                    _reconciliation_result_from_json(row["result_json"]),
                )

            duplicate = connection.execute(
                "SELECT * FROM reconciliation_report_claims "
                "WHERE agent_id = ? AND content_digest = ? AND status = 'COMPLETE' "
                "ORDER BY received_at, report_id LIMIT 1",
                (str(agent_id), content_digest),
            ).fetchone()
            if duplicate is not None:
                result = _reconciliation_result_from_json(duplicate["result_json"])
                connection.execute(
                    "INSERT INTO reconciliation_report_claims "
                    "(report_id, agent_id, content_digest, received_at, status, result_json) "
                    "VALUES (?, ?, ?, ?, 'COMPLETE', ?)",
                    (
                        str(report_id),
                        str(agent_id),
                        content_digest,
                        received_at.isoformat(),
                        duplicate["result_json"],
                    ),
                )
                return ReportClaim(ReportClaimStatus.DUPLICATE, result)

            processing = connection.execute(
                "SELECT 1 FROM reconciliation_report_claims "
                "WHERE agent_id = ? AND content_digest = ? AND status = 'PROCESSING' LIMIT 1",
                (str(agent_id), content_digest),
            ).fetchone()
            if processing is not None:
                return ReportClaim(ReportClaimStatus.IN_PROGRESS)

            connection.execute(
                "INSERT INTO reconciliation_report_claims "
                "(report_id, agent_id, content_digest, received_at, status, result_json) "
                "VALUES (?, ?, ?, ?, 'PROCESSING', NULL)",
                (str(report_id), str(agent_id), content_digest, received_at.isoformat()),
            )
        return ReportClaim(ReportClaimStatus.NEW)

    async def complete_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
        result: ReconciliationResult,
    ) -> None:
        result_json = _reconciliation_result_json(result)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_report_claims WHERE report_id = ?",
                (str(report_id),),
            ).fetchone()
            if row is None or row["content_digest"] != content_digest:
                raise ValueError("Reconciliation report claim was lost or changed")
            if row["status"] == "COMPLETE":
                if row["result_json"] != result_json:
                    raise ValueError("Completed reconciliation result cannot change")
                return
            cursor = connection.execute(
                "UPDATE reconciliation_report_claims SET status = 'COMPLETE', result_json = ? "
                "WHERE report_id = ? AND content_digest = ? AND status = 'PROCESSING'",
                (result_json, str(report_id), content_digest),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reconciliation report completion compare-and-set failed")

    async def abandon_report(
        self,
        report_id: UUID,
        *,
        content_digest: str,
    ) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM reconciliation_report_claims "
                "WHERE report_id = ? AND content_digest = ? AND status = 'PROCESSING'",
                (str(report_id), content_digest),
            )


class SQLiteReconciliationBootRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def get_boot_context(self, agent_id: UUID) -> AgentBootContext | None:
        with self._database.transaction() as connection:
            active = connection.execute(
                "SELECT boot_id FROM agent_connections "
                "WHERE agent_id = ? AND disconnected_at IS NULL LIMIT 1",
                (str(agent_id),),
            ).fetchone()
            if active is None:
                return None
            prior = connection.execute(
                "SELECT boot_id FROM agent_connections "
                "WHERE agent_id = ? AND disconnected_at IS NOT NULL "
                "ORDER BY disconnected_at DESC, connected_at DESC LIMIT 1",
                (str(agent_id),),
            ).fetchone()
        return AgentBootContext(
            active_boot_id=UUID(active["boot_id"]),
            interrupted_boot_id=UUID(prior["boot_id"]) if prior is not None else None,
        )


class SQLiteReconciliationLeaseAdapter:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._leases = SQLiteReservationLeaseRepository(database)

    async def list_leases(self, agent_id: UUID) -> list[ReservationLease]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM reservation_leases "
                "WHERE agent_id = ? AND released_at IS NULL "
                "ORDER BY reservation_id, lease_version DESC",
                (str(agent_id),),
            ).fetchall()
        return [_lease_from_row(row) for row in rows]

    async def release_if_current(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        expected_lease_version: int,
        released_at: datetime,
    ) -> ReservationLease | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservation_leases WHERE reservation_id = ? "
                "AND lease_version = ? AND agent_id = ? AND released_at IS NULL",
                (str(reservation_id), expected_lease_version, str(agent_id)),
            ).fetchone()
        if row is None:
            return None
        lease = _lease_from_row(row)
        current = await self._leases.current(lease.bench_id)
        if (
            current is None
            or current.reservation_id != reservation_id
            or current.lease_version != expected_lease_version
            or current.agent_id != agent_id
        ):
            return None
        return await self._leases.release(
            reservation_id,
            expected_lease_version,
            released_at,
        )


class SQLiteReconciliationInventoryAdapter:
    def __init__(
        self,
        agents: SQLiteAgentPresenceAdapter,
        inventory: SQLiteInventoryAdapter,
    ) -> None:
        self._agents = agents
        self._inventory = inventory

    async def reconcile_report_inventory(
        self,
        agent_id: UUID,
        *,
        boot_id: UUID,
        generated_at: datetime,
        snapshots: tuple[ReconciliationBenchSnapshot, ...],
        observed_at: datetime,
    ) -> InventoryReconciliation:
        agent = await self._agents.get_agent(agent_id)
        if agent is None:
            raise ValueError("Reconciliation Agent does not exist")
        benches = tuple(
            GlobalBenchRecord(
                id=f"{agent.slug}/{snapshot.local_bench_id}",
                agent_id=agent.id,
                agent_slug=agent.slug,
                local_bench_id=snapshot.local_bench_id,
                name=snapshot.name,
                backend_id=snapshot.backend_id,
                kind=snapshot.kind,
                target_type=snapshot.target_type,
                status=snapshot.status,
                health=snapshot.health,
                capabilities=snapshot.capabilities,
                labels=snapshot.labels,
                firmware_version=snapshot.firmware_version,
                last_seen_at=observed_at,
                created_at=observed_at,
                updated_at=observed_at,
            )
            for snapshot in snapshots
        )
        return await self._inventory.reconcile_agent_snapshot(
            agent,
            benches,
            observed_at=observed_at,
            snapshot_id=uuid4(),
            boot_id=boot_id,
            generated_at=generated_at,
        )


class SQLitePersistedReconciliationHandler:
    """Archive the received report before applying its reconciliation effects."""

    def __init__(
        self,
        database: SQLiteDatabase,
        service: ReconciliationService,
    ) -> None:
        self._database = database
        self._reports = SQLiteReconciliationRepository(database)
        self._service = service

    async def reconcile(
        self,
        report_id: UUID,
        report: ReconciliationReport,
    ) -> ReconciliationResult:
        await self._reports.save(report_id, report, datetime.now(UTC))
        return await self._service.reconcile(report_id, report)


class SQLiteAgentDrainRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def get_drain_snapshot(self, agent_id: UUID) -> AgentDrainSnapshot | None:
        with self._database.transaction() as connection:
            return _drain_snapshot(connection, agent_id)

    async def compare_and_set_status(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
        target_status: AgentStatus,
        require_idle: bool = False,
        require_active_connection: bool = False,
    ) -> AgentDrainSnapshot | None:
        expected_value = expected_status.value
        target_value = target_status.value
        with self._database.transaction(immediate=True) as connection:
            current = _drain_snapshot(connection, agent_id)
            if (
                current is None
                or current.agent.status.value != expected_value
                or require_idle
                and not current.workload.idle
                or require_active_connection
                and not current.has_active_connection
            ):
                return None
            cursor = connection.execute(
                "UPDATE agents SET status = ? WHERE id = ? AND status = ?",
                (target_value, str(agent_id), expected_value),
            )
            if cursor.rowcount != 1:
                return None
            return _drain_snapshot(connection, agent_id)

    async def cancel_queued_work(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
    ) -> int | None:
        expected_value = expected_status.value
        now = datetime.now(UTC).isoformat()
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM agents WHERE id = ?",
                (str(agent_id),),
            ).fetchone()
            if row is None or row["status"] != expected_value:
                return None
            remote = connection.execute(
                "UPDATE remote_commands SET status = 'CANCELLED', completed_at = ?, "
                "error_code = 'AGENT_DRAINING', error_message = 'Cancelled while draining' "
                "WHERE agent_id = ? AND status = 'QUEUED'",
                (now, str(agent_id)),
            ).rowcount
            ci = connection.execute(
                "UPDATE ci_sessions SET status = 'cancel_requested' "
                "WHERE bench_id IN (SELECT id FROM global_benches WHERE agent_id = ?) "
                "AND status IN ('created', 'waiting_for_bench')",
                (str(agent_id),),
            ).rowcount
            return remote + ci


class SQLiteProtocolJournalAdapter:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._repository = SQLiteProtocolMessageJournalRepository(database)

    async def record_protocol_message(self, record: ProtocolMessageJournalRecord) -> bool:
        disposition = await self._repository.record(record)
        return disposition is ProtocolMessageRecordDisposition.RECORDED

    async def finalize_protocol_message(self, record: ProtocolMessageJournalRecord) -> None:
        if record.handled_at is None:
            raise ValueError("Final protocol message requires handled_at")
        await self._repository.mark_handled(
            record.agent_id,
            record.direction,
            record.message_id,
            handled_at=record.handled_at,
            outcome=record.outcome,
        )


class SQLiteArtifactTransferServiceRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._transfers = SQLiteArtifactTransferRepository(database)
        self._artifacts = SQLiteRemoteArtifactRepository(database)

    async def create_transfer(self, transfer: ArtifactTransferRecord) -> ArtifactTransferRecord:
        return await self._transfers.create(transfer)

    async def get_transfer(self, transfer_id: UUID) -> ArtifactTransferRecord | None:
        return await self._transfers.get(transfer_id)

    async def get_transfer_by_token_hash(
        self,
        token_hash: str,
    ) -> ArtifactTransferRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM artifact_transfers WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return await self._transfers.get(UUID(row["id"])) if row is not None else None

    async def update_transfer(
        self,
        transfer: ArtifactTransferRecord,
        *,
        expected_statuses: set[ArtifactTransferStatus],
    ) -> ArtifactTransferRecord | None:
        current = await self._transfers.get(transfer.id)
        if current is None or current.status not in expected_statuses:
            return None
        return await self._transfers.update(transfer, expected_status=current.status)

    async def add_attempt(self, attempt: ArtifactTransferAttempt) -> ArtifactTransferAttempt:
        recorded = await self._transfers.add_attempt(
            attempt,
            expected_attempt_count=attempt.attempt_number - 1,
        )
        if recorded is None:
            raise ValueError("Artifact transfer attempt compare-and-set failed")
        return recorded

    async def put_remote_artifact(
        self,
        artifact: RemoteArtifactMetadata,
    ) -> RemoteArtifactMetadata:
        # Creation is also the immutable metadata/idempotency fence.  Calling it for
        # an existing artifact ensures an ID or Agent-local ID cannot be replayed
        # with different content merely because its upload timestamp is unchanged.
        existing = await self._artifacts.create(artifact)
        if existing.uploaded_at == artifact.uploaded_at:
            return existing
        if existing.uploaded_at is not None or artifact.uploaded_at is None:
            raise ValueError("Remote artifact upload state cannot regress")
        updated = await self._artifacts.update_uploaded(
            existing.id,
            artifact.uploaded_at,
            expected_uploaded_at=None,
        )
        if updated is None:
            raise ValueError("Remote artifact upload compare-and-set failed")
        return updated

    async def get_remote_artifact(self, artifact_id: UUID) -> RemoteArtifactMetadata | None:
        return await self._artifacts.get(artifact_id)


def _reconciliation_result_json(result: ReconciliationResult) -> str:
    payload = {
        "report_id": str(result.report_id),
        "report_digest": result.report_digest,
        "agent_id": str(result.agent_id),
        "boot_id": str(result.boot_id),
        "restarted": result.restarted,
        "reconciled_command_ids": sorted(map(str, result.reconciled_command_ids)),
        "reconciled_operation_ids": sorted(map(str, result.reconciled_operation_ids)),
        "interrupted_operation_ids": sorted(map(str, result.interrupted_operation_ids)),
        "expired_reservation_ids": sorted(map(str, result.expired_reservation_ids)),
        "stale_local_leases": sorted(
            [
                [str(reservation_id), version]
                for reservation_id, version in result.stale_local_leases
            ]
        ),
        "inventory_reconciled": result.inventory_reconciled,
        "deduplicated": result.deduplicated,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _reconciliation_result_from_json(raw: object) -> ReconciliationResult:
    if not isinstance(raw, str):
        raise ValueError("Completed reconciliation claim is missing its result")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Persisted reconciliation result must be an object")
    try:
        return ReconciliationResult(
            report_id=UUID(payload["report_id"]),
            report_digest=str(payload["report_digest"]),
            agent_id=UUID(payload["agent_id"]),
            boot_id=UUID(payload["boot_id"]),
            restarted=bool(payload["restarted"]),
            reconciled_command_ids=frozenset(
                UUID(value) for value in payload["reconciled_command_ids"]
            ),
            reconciled_operation_ids=frozenset(
                UUID(value) for value in payload["reconciled_operation_ids"]
            ),
            interrupted_operation_ids=frozenset(
                UUID(value) for value in payload["interrupted_operation_ids"]
            ),
            expired_reservation_ids=frozenset(
                UUID(value) for value in payload["expired_reservation_ids"]
            ),
            stale_local_leases=frozenset(
                (UUID(value[0]), int(value[1])) for value in payload["stale_local_leases"]
            ),
            inventory_reconciled=bool(payload["inventory_reconciled"]),
            deduplicated=bool(payload.get("deduplicated", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Persisted reconciliation result is malformed") from exc


def _drain_snapshot(
    connection: sqlite3.Connection,
    agent_id: UUID,
) -> AgentDrainSnapshot | None:
    agent_row = connection.execute(
        "SELECT * FROM agents WHERE id = ?",
        (str(agent_id),),
    ).fetchone()
    if agent_row is None:
        return None
    operation_count = connection.execute(
        "SELECT COUNT(*) AS count FROM distributed_operations "
        "WHERE agent_id = ? AND status IN "
        "('CREATED', 'DISPATCHED', 'ACCEPTED', 'RUNNING', 'UNKNOWN', 'RECONCILING')",
        (str(agent_id),),
    ).fetchone()["count"]
    reservation_count = connection.execute(
        "SELECT COUNT(*) AS count FROM reservations "
        "WHERE bench_id IN (SELECT id FROM global_benches WHERE agent_id = ?) "
        "AND status IN ('active', 'expired_pending_operation')",
        (str(agent_id),),
    ).fetchone()["count"]
    ci_count = connection.execute(
        "SELECT COUNT(*) AS count FROM ci_sessions "
        "WHERE bench_id IN (SELECT id FROM global_benches WHERE agent_id = ?) "
        "AND status IN ('created', 'waiting_for_bench')",
        (str(agent_id),),
    ).fetchone()["count"]
    active_connection = connection.execute(
        "SELECT 1 FROM agent_connections WHERE agent_id = ? AND disconnected_at IS NULL LIMIT 1",
        (str(agent_id),),
    ).fetchone()
    return AgentDrainSnapshot(
        agent=_agent_from_row(agent_row),
        workload=AgentWorkload(
            active_operations=int(operation_count),
            active_reservations=int(reservation_count),
            queued_ci_sessions=int(ci_count),
        ),
        has_active_connection=active_connection is not None,
    )


def _update_agent(connection: sqlite3.Connection, agent: AgentRecord) -> None:
    cursor = connection.execute(
        "UPDATE agents SET status = ?, version = ?, protocol_version = ?, location = ?, "
        "labels_json = ?, last_connected_at = ?, last_seen_at = ?, disconnected_at = ?, "
        "certificate_fingerprint = ?, enrollment_status = ?, revoked_at = ? "
        "WHERE id = ? AND slug = ?",
        (
            agent.status.value,
            agent.version,
            agent.protocol_version,
            agent.location,
            json.dumps(agent.labels, sort_keys=True, separators=(",", ":")),
            _datetime_value(agent.last_connected_at),
            _datetime_value(agent.last_seen_at),
            _datetime_value(agent.disconnected_at),
            agent.certificate_fingerprint,
            agent.enrollment_status.value,
            _datetime_value(agent.revoked_at),
            str(agent.id),
            agent.slug,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("Agent presence update lost its identity fence")


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
