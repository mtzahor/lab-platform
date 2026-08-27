from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from lab_platform.models import (
    BENCH_MAINTENANCE_LABEL,
    BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL,
    DISTRIBUTED_OPERATION_TRANSITIONS,
    REMOTE_COMMAND_TRANSITIONS,
    ActorContext,
    AgentConnectionRecord,
    AgentTimelineRecord,
    AgentTimelineSeverity,
    ArtifactTransferAttempt,
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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _PersistenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoredBenchSnapshot(_PersistenceRecord):
    id: UUID
    agent_id: UUID
    boot_id: UUID
    generated_at: datetime
    received_at: datetime
    benches: tuple[GlobalBenchRecord, ...] = Field(max_length=10_000)

    @field_validator("generated_at", "received_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> StoredBenchSnapshot:
        if any(bench.agent_id != self.agent_id for bench in self.benches):
            raise ValueError("Snapshot contains a bench owned by another Agent")
        if len({bench.organisation_id for bench in self.benches}) > 1:
            raise ValueError("Snapshot contains benches owned by multiple organisations")
        identifiers = [(bench.id, bench.local_bench_id) for bench in self.benches]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Snapshot contains duplicate bench identities")
        return self


class StoredReconciliationReport(_PersistenceRecord):
    id: UUID
    received_at: datetime
    report: ReconciliationReport

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ProtocolMessageRecordDisposition(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"


class SQLiteAgentConnectionRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def open(self, record: AgentConnectionRecord) -> AgentConnectionRecord:
        if record.disconnected_at is not None:
            raise ValueError("A newly opened connection cannot already be disconnected")
        with self._database.transaction(immediate=True) as connection:
            same_row = connection.execute(
                "SELECT * FROM agent_connections WHERE id = ?", (str(record.id),)
            ).fetchone()
            if same_row is not None:
                existing = _connection_from_row(same_row)
                if existing != record:
                    raise ValueError("Connection ID is already bound to different state")
                return existing

            active_row = connection.execute(
                "SELECT * FROM agent_connections WHERE agent_id = ? AND disconnected_at IS NULL",
                (str(record.agent_id),),
            ).fetchone()
            if active_row is not None:
                active = _connection_from_row(active_row)
                if record.connected_at < active.last_heartbeat_at:
                    raise ValueError("A stale connection cannot fence a newer active connection")
                connection.execute(
                    "UPDATE agent_connections SET disconnected_at = ? "
                    "WHERE id = ? AND disconnected_at IS NULL",
                    (record.connected_at.isoformat(), str(active.id)),
                )
            connection.execute(
                "INSERT INTO agent_connections "
                "(id, agent_id, boot_id, protocol_version, connected_at, "
                "last_heartbeat_at, disconnected_at, last_sequence_number, "
                "observed_clock_offset_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _connection_values(record),
            )
        return record

    async def get(self, connection_id: UUID) -> AgentConnectionRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE id = ?", (str(connection_id),)
            ).fetchone()
        return _connection_from_row(row) if row is not None else None

    async def get_active(self, agent_id: UUID) -> AgentConnectionRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE agent_id = ? AND disconnected_at IS NULL",
                (str(agent_id),),
            ).fetchone()
        return _connection_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        agent_id: UUID | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[AgentConnectionRecord]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            values.append(str(agent_id))
        if active_only:
            conditions.append("disconnected_at IS NULL")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_connections{where} "  # noqa: S608
                "ORDER BY connected_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_connection_from_row(row) for row in rows]

    async def heartbeat(
        self,
        connection_id: UUID,
        *,
        expected_last_sequence_number: int,
        last_sequence_number: int,
        heartbeat_at: datetime,
        observed_clock_offset_seconds: float,
    ) -> AgentConnectionRecord | None:
        timestamp = _as_utc(heartbeat_at)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE id = ? "
                "AND disconnected_at IS NULL AND last_sequence_number = ?",
                (str(connection_id), expected_last_sequence_number),
            ).fetchone()
            if row is None:
                return None
            current = _connection_from_row(row)
            updated = AgentConnectionRecord.model_validate(
                {
                    **current.model_dump(),
                    "last_heartbeat_at": timestamp,
                    "last_sequence_number": last_sequence_number,
                    "observed_clock_offset_seconds": observed_clock_offset_seconds,
                }
            )
            if (
                timestamp < current.last_heartbeat_at
                or last_sequence_number < current.last_sequence_number
            ):
                return None
            cursor = connection.execute(
                "UPDATE agent_connections SET last_heartbeat_at = ?, "
                "last_sequence_number = ?, observed_clock_offset_seconds = ? "
                "WHERE id = ? AND disconnected_at IS NULL AND last_sequence_number = ?",
                (
                    timestamp.isoformat(),
                    updated.last_sequence_number,
                    updated.observed_clock_offset_seconds,
                    str(connection_id),
                    expected_last_sequence_number,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return updated

    async def disconnect(
        self,
        connection_id: UUID,
        disconnected_at: datetime,
        *,
        expected_last_sequence_number: int | None = None,
    ) -> AgentConnectionRecord | None:
        timestamp = _as_utc(disconnected_at)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM agent_connections WHERE id = ? AND disconnected_at IS NULL",
                (str(connection_id),),
            ).fetchone()
            if row is None:
                return None
            current = _connection_from_row(row)
            if (
                expected_last_sequence_number is not None
                and current.last_sequence_number != expected_last_sequence_number
            ):
                return None
            updated = AgentConnectionRecord.model_validate(
                {**current.model_dump(), "disconnected_at": timestamp}
            )
            cursor = connection.execute(
                "UPDATE agent_connections SET disconnected_at = ? "
                "WHERE id = ? AND disconnected_at IS NULL AND last_sequence_number = ?",
                (timestamp.isoformat(), str(connection_id), current.last_sequence_number),
            )
            if cursor.rowcount != 1:
                return None
        return updated


class SQLiteGlobalBenchRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def upsert(self, bench: GlobalBenchRecord) -> GlobalBenchRecord:
        with self._database.transaction(immediate=True) as connection:
            _upsert_bench(connection, bench)
            row = connection.execute(
                "SELECT * FROM global_benches WHERE id = ?", (bench.id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the upsert
            raise RuntimeError("Global bench upsert did not persist a row")
        return _bench_from_row(row)

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
        agent_id: UUID | None = None,
        status: GlobalBenchStatus | None = None,
        kind: GlobalBenchKind | None = None,
        health: HealthStatus | None = None,
        capability: str | None = None,
        labels: Mapping[str, str] | None = None,
        limit: int = 10_000,
    ) -> builtins.list[GlobalBenchRecord]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        for condition, value in (
            (
                "organisation_id = ?",
                str(organisation_id) if organisation_id is not None else None,
            ),
            ("agent_id = ?", str(agent_id) if agent_id is not None else None),
            ("status = ?", status.value if status is not None else None),
            ("kind = ?", kind.value if kind is not None else None),
            ("health = ?", health.value if health is not None else None),
        ):
            if value is not None:
                conditions.append(condition)
                values.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._database.transaction() as connection:
            if capability is None and not labels:
                values.append(limit)
                rows = connection.execute(
                    f"SELECT * FROM global_benches{where} ORDER BY id LIMIT ?",  # noqa: S608
                    values,
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT * FROM global_benches{where} ORDER BY id",  # noqa: S608
                    values,
                ).fetchall()
        records = [_bench_from_row(row) for row in rows]
        if capability is not None:
            required = capability.casefold()
            records = [
                item
                for item in records
                if required in {available.casefold() for available in item.capabilities}
            ]
        if labels:
            records = [
                item
                for item in records
                if all(item.labels.get(key) == value for key, value in labels.items())
            ]
        return records[:limit]

    async def save_snapshot(
        self,
        snapshot: StoredBenchSnapshot,
    ) -> builtins.list[GlobalBenchRecord]:
        snapshot_json = _dump_json([bench.model_dump(mode="json") for bench in snapshot.benches])
        with self._database.transaction(immediate=True) as connection:
            existing_row = connection.execute(
                "SELECT * FROM bench_snapshots WHERE id = ?", (str(snapshot.id),)
            ).fetchone()
            if existing_row is not None:
                existing = _snapshot_from_row(existing_row)
                if existing != snapshot:
                    raise ValueError("Snapshot ID is already bound to different inventory")
                return _list_benches(
                    connection,
                    agent_id=snapshot.agent_id,
                    organisation_id=_snapshot_organisation_id(connection, snapshot),
                )

            organisation_id = _snapshot_organisation_id(connection, snapshot)

            latest_row = connection.execute(
                "SELECT generated_at FROM bench_snapshots WHERE agent_id = ? "
                "ORDER BY generated_at DESC, received_at DESC LIMIT 1",
                (str(snapshot.agent_id),),
            ).fetchone()
            if (
                latest_row is not None
                and _parse_datetime(latest_row["generated_at"]) > snapshot.generated_at
            ):
                raise ValueError("A stale inventory snapshot cannot replace newer state")

            for bench in snapshot.benches:
                _upsert_bench(connection, bench)
            seen = {bench.id for bench in snapshot.benches}
            parameters: list[object] = [
                GlobalBenchStatus.OFFLINE.value,
                snapshot.received_at.isoformat(),
                str(snapshot.agent_id),
                snapshot.received_at.isoformat(),
            ]
            missing_clause = ""
            if seen:
                placeholders = ", ".join("?" for _ in seen)
                missing_clause = f" AND id NOT IN ({placeholders})"
                parameters.extend(sorted(seen))
            connection.execute(
                "UPDATE global_benches SET status = ?, updated_at = ? "
                "WHERE agent_id = ? AND updated_at <= ? AND status != 'OFFLINE' "
                "AND organisation_id = ?"
                f"{missing_clause}",  # noqa: S608
                (*parameters[:4], str(organisation_id), *parameters[4:]),
            )
            connection.execute(
                "INSERT INTO bench_snapshots "
                "(id, organisation_id, agent_id, boot_id, generated_at, received_at, bench_count, "
                "snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(snapshot.id),
                    str(organisation_id),
                    str(snapshot.agent_id),
                    str(snapshot.boot_id),
                    snapshot.generated_at.isoformat(),
                    snapshot.received_at.isoformat(),
                    len(snapshot.benches),
                    snapshot_json,
                ),
            )
            return _list_benches(
                connection,
                agent_id=snapshot.agent_id,
                organisation_id=organisation_id,
            )

    async def get_snapshot(
        self,
        snapshot_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> StoredBenchSnapshot | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(snapshot_id), str(organisation_id))
            if organisation_id is not None
            else (str(snapshot_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM bench_snapshots WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    async def mark_agent_offline(
        self,
        agent_id: UUID,
        observed_at: datetime,
        *,
        organisation_id: UUID | None = None,
    ) -> builtins.list[GlobalBenchRecord]:
        timestamp = _as_utc(observed_at).isoformat()
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: list[object] = [timestamp, str(agent_id), timestamp]
        if organisation_id is not None:
            values.append(str(organisation_id))
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE global_benches SET status = 'OFFLINE', updated_at = ? "
                f"WHERE agent_id = ? AND updated_at <= ? AND status != 'OFFLINE'{scope}",  # noqa: S608
                values,
            )
            return _list_benches(
                connection,
                agent_id=agent_id,
                organisation_id=organisation_id,
            )


class SQLiteRemoteCommandRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, command: RemoteCommand) -> RemoteCommand:
        with self._database.transaction(immediate=True) as connection:
            _require_command_route(connection, command)
            row = connection.execute(
                "SELECT * FROM remote_commands WHERE id = ? OR "
                "(agent_id = ? AND idempotency_key = ?) "
                "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
                (
                    str(command.id),
                    str(command.agent_id),
                    command.idempotency_key,
                    str(command.id),
                ),
            ).fetchone()
            if row is not None:
                existing = _command_from_row(row)
                _require_same_command_request(existing, command)
                return existing
            connection.execute(
                "INSERT INTO remote_commands "
                "(id, organisation_id, agent_id, bench_id, command_type, payload_json, "
                "status, created_at, "
                "dispatched_at, acknowledged_at, started_at, completed_at, expires_at, "
                "idempotency_key, attempt_count, operation_id, reservation_id, lease_version, "
                "error_code, error_message, actor_context_json, authorisation_snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _command_values(command),
            )
        return command

    async def get(
        self,
        command_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteCommand | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(command_id), str(organisation_id))
            if organisation_id is not None
            else (str(command_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM remote_commands WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _command_from_row(row) if row is not None else None

    async def get_by_idempotency_key(
        self,
        agent_id: UUID,
        idempotency_key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteCommand | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(agent_id), idempotency_key, str(organisation_id))
            if organisation_id is not None
            else (str(agent_id), idempotency_key)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM remote_commands WHERE agent_id = ? AND idempotency_key = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _command_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        status: RemoteCommandStatus | None = None,
        limit: int = 500,
    ) -> list[RemoteCommand]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        for condition, value in (
            (
                "organisation_id = ?",
                str(organisation_id) if organisation_id is not None else None,
            ),
            ("agent_id = ?", str(agent_id) if agent_id is not None else None),
            ("bench_id = ?", bench_id),
            ("status = ?", status.value if status is not None else None),
        ):
            if value is not None:
                conditions.append(condition)
                values.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM remote_commands{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_command_from_row(row) for row in rows]

    async def update(
        self,
        command: RemoteCommand,
        *,
        expected_status: RemoteCommandStatus,
    ) -> RemoteCommand | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM remote_commands WHERE id = ?", (str(command.id),)
            ).fetchone()
            if row is None:
                return None
            current = _command_from_row(row)
            if current.status is not expected_status:
                return None
            _require_same_command_identity(current, command)
            if current.attempt_count != command.attempt_count:
                raise ValueError("Remote command attempt_count can only change with an attempt")
            _require_remote_command_transition(expected_status, command.status)
            cursor = connection.execute(
                "UPDATE remote_commands SET status = ?, dispatched_at = ?, "
                "acknowledged_at = ?, started_at = ?, completed_at = ?, attempt_count = ?, "
                "operation_id = ?, error_code = ?, error_message = ? "
                "WHERE id = ? AND status = ?",
                (
                    command.status.value,
                    _datetime_value(command.dispatched_at),
                    _datetime_value(command.acknowledged_at),
                    _datetime_value(command.started_at),
                    _datetime_value(command.completed_at),
                    command.attempt_count,
                    _uuid_value(command.operation_id),
                    command.error_code,
                    command.error_message,
                    str(command.id),
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return command

    async def add_attempt(
        self,
        attempt: RemoteCommandAttempt,
        *,
        expected_attempt_count: int,
    ) -> RemoteCommandAttempt | None:
        with self._database.transaction(immediate=True) as connection:
            existing_row = connection.execute(
                "SELECT * FROM remote_command_attempts WHERE id = ? OR "
                "(command_id = ? AND attempt_number = ?) LIMIT 1",
                (str(attempt.id), str(attempt.command_id), attempt.attempt_number),
            ).fetchone()
            if existing_row is not None:
                existing = _command_attempt_from_row(existing_row)
                if not _command_attempt_compatible(existing, attempt):
                    raise ValueError("Command attempt identity is already bound to other state")
                return existing
            if attempt.attempt_number != expected_attempt_count + 1:
                raise ValueError("Command attempt number must follow the persisted attempt count")
            updated = connection.execute(
                "UPDATE remote_commands SET attempt_count = attempt_count + 1 "
                "WHERE id = ? AND attempt_count = ?",
                (str(attempt.command_id), expected_attempt_count),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO remote_command_attempts "
                "(id, command_id, attempt_number, connection_id, sequence_number, "
                "dispatched_at, acknowledged_at, failed_at, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _command_attempt_values(attempt),
            )
        return attempt

    async def finish_attempt(self, attempt: RemoteCommandAttempt) -> RemoteCommandAttempt | None:
        if attempt.acknowledged_at is None and attempt.failed_at is None:
            raise ValueError("A finished command attempt needs an acknowledgment or failure")
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM remote_command_attempts WHERE id = ?", (str(attempt.id),)
            ).fetchone()
            if row is None:
                return None
            current = _command_attempt_from_row(row)
            if current.acknowledged_at is not None or current.failed_at is not None:
                return current if current == attempt else None
            if (
                current.command_id != attempt.command_id
                or current.attempt_number != attempt.attempt_number
                or current.connection_id != attempt.connection_id
                or current.sequence_number != attempt.sequence_number
                or current.dispatched_at != attempt.dispatched_at
            ):
                raise ValueError("Command attempt immutable fields changed")
            cursor = connection.execute(
                "UPDATE remote_command_attempts SET acknowledged_at = ?, failed_at = ?, "
                "error_code = ? WHERE id = ? AND acknowledged_at IS NULL AND failed_at IS NULL",
                (
                    _datetime_value(attempt.acknowledged_at),
                    _datetime_value(attempt.failed_at),
                    attempt.error_code,
                    str(attempt.id),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return attempt

    async def list_attempts(self, command_id: UUID) -> builtins.list[RemoteCommandAttempt]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_command_attempts WHERE command_id = ? "
                "ORDER BY attempt_number, id",
                (str(command_id),),
            ).fetchall()
        return [_command_attempt_from_row(row) for row in rows]


class SQLiteDistributedOperationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, operation: DistributedOperation) -> DistributedOperation:
        with self._database.transaction(immediate=True) as connection:
            _require_operation_route(connection, operation)
            row = connection.execute(
                "SELECT * FROM distributed_operations WHERE id = ? OR remote_command_id = ? "
                "LIMIT 1",
                (str(operation.id), str(operation.remote_command_id)),
            ).fetchone()
            if row is not None:
                existing = _operation_from_row(row)
                _require_same_operation_create(existing, operation)
                return existing
            connection.execute(
                "INSERT INTO distributed_operations "
                "(id, organisation_id, remote_command_id, agent_id, bench_id, reservation_id, "
                "operation_type, "
                "status, progress, message, result_json, created_at, dispatched_at, started_at, "
                "completed_at, last_agent_update_at, reconciliation_deadline, error_code, "
                "error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _operation_values(operation),
            )
        return operation

    async def get(
        self,
        operation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(operation_id), str(organisation_id))
            if organisation_id is not None
            else (str(operation_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM distributed_operations WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _operation_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        status: DistributedOperationStatus | None = None,
        limit: int = 500,
    ) -> list[DistributedOperation]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        for condition, value in (
            (
                "organisation_id = ?",
                str(organisation_id) if organisation_id is not None else None,
            ),
            ("agent_id = ?", str(agent_id) if agent_id is not None else None),
            ("bench_id = ?", bench_id),
            ("status = ?", status.value if status is not None else None),
        ):
            if value is not None:
                conditions.append(condition)
                values.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM distributed_operations{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_operation_from_row(row) for row in rows]

    async def update(
        self,
        operation: DistributedOperation,
        *,
        expected_status: DistributedOperationStatus,
    ) -> DistributedOperation | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM distributed_operations WHERE id = ?", (str(operation.id),)
            ).fetchone()
            if row is None:
                return None
            current = _operation_from_row(row)
            if current.status is not expected_status:
                return None
            if (
                current.last_agent_update_at is not None
                and operation.last_agent_update_at is not None
                and operation.last_agent_update_at < current.last_agent_update_at
            ):
                return None
            _require_same_operation_identity(current, operation)
            _require_operation_transition(expected_status, operation.status)
            cursor = connection.execute(
                "UPDATE distributed_operations SET status = ?, progress = ?, message = ?, "
                "result_json = ?, "
                "dispatched_at = ?, started_at = ?, completed_at = ?, "
                "last_agent_update_at = ?, reconciliation_deadline = ?, error_code = ?, "
                "error_message = ? WHERE id = ? AND status = ?",
                (
                    operation.status.value,
                    operation.progress,
                    operation.message,
                    (
                        _dump_json_mapping(
                            operation.result,
                            field="Distributed operation result",
                            maximum_bytes=1_048_576,
                        )
                        if operation.result is not None
                        else None
                    ),
                    _datetime_value(operation.dispatched_at),
                    _datetime_value(operation.started_at),
                    _datetime_value(operation.completed_at),
                    _datetime_value(operation.last_agent_update_at),
                    _datetime_value(operation.reconciliation_deadline),
                    operation.error_code,
                    operation.error_message,
                    str(operation.id),
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return operation


class SQLiteReservationLeaseRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def put(
        self,
        lease: ReservationLease,
        *,
        expected_current_version: int | None = None,
    ) -> ReservationLease | None:
        if lease.released_at is not None:
            raise ValueError("Only active reservation leases can be put as current")
        with self._database.transaction(immediate=True) as connection:
            exact_row = connection.execute(
                "SELECT * FROM reservation_leases WHERE reservation_id = ? AND lease_version = ?",
                (str(lease.reservation_id), lease.lease_version),
            ).fetchone()
            if exact_row is not None:
                existing = _lease_from_row(exact_row)
                if existing != lease:
                    raise ValueError("Lease version is already bound to different state")
                return existing

            current_row = connection.execute(
                "SELECT * FROM reservation_leases WHERE bench_id = ? AND released_at IS NULL",
                (lease.bench_id,),
            ).fetchone()
            current = _lease_from_row(current_row) if current_row is not None else None
            current_version = current.lease_version if current is not None else None
            if expected_current_version != current_version:
                return None
            maximum_row = connection.execute(
                "SELECT MAX(lease_version) AS maximum_version FROM reservation_leases "
                "WHERE bench_id = ?",
                (lease.bench_id,),
            ).fetchone()
            maximum_version = maximum_row["maximum_version"]
            if maximum_version is not None and lease.lease_version <= int(maximum_version):
                return None
            if current is not None:
                if lease.valid_from < current.valid_from:
                    return None
                connection.execute(
                    "UPDATE reservation_leases SET released_at = ? WHERE reservation_id = ? "
                    "AND lease_version = ? AND released_at IS NULL",
                    (
                        lease.valid_from.isoformat(),
                        str(current.reservation_id),
                        current.lease_version,
                    ),
                )
            connection.execute(
                "INSERT INTO reservation_leases "
                "(reservation_id, agent_id, bench_id, owner, valid_from, valid_until, "
                "lease_version, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _lease_values(lease),
            )
        return lease

    async def current(self, bench_id: str) -> ReservationLease | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reservation_leases WHERE bench_id = ? AND released_at IS NULL",
                (bench_id,),
            ).fetchone()
        return _lease_from_row(row) if row is not None else None

    async def release(
        self,
        reservation_id: UUID,
        lease_version: int,
        released_at: datetime,
    ) -> ReservationLease | None:
        timestamp = _as_utc(released_at)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reservation_leases WHERE reservation_id = ? AND lease_version = ?",
                (str(reservation_id), lease_version),
            ).fetchone()
            if row is None:
                return None
            current = _lease_from_row(row)
            if current.released_at is not None:
                return current
            released = ReservationLease.model_validate(
                {**current.model_dump(), "released_at": timestamp}
            )
            cursor = connection.execute(
                "UPDATE reservation_leases SET released_at = ? WHERE reservation_id = ? "
                "AND lease_version = ? AND released_at IS NULL",
                (timestamp.isoformat(), str(reservation_id), lease_version),
            )
            if cursor.rowcount != 1:
                return None
        return released


class SQLiteReconciliationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def save(
        self,
        report_id: UUID,
        report: ReconciliationReport,
        received_at: datetime,
    ) -> StoredReconciliationReport:
        stored = StoredReconciliationReport(
            id=report_id,
            report=report,
            received_at=received_at,
        )
        report_json = _dump_json(report.model_dump(mode="json"))
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_reports WHERE id = ?", (str(report_id),)
            ).fetchone()
            if row is not None:
                existing = _reconciliation_from_row(row)
                if existing.report != report:
                    raise ValueError("Reconciliation report ID is already bound to other state")
                return existing
            connection.execute(
                "INSERT INTO reconciliation_reports "
                "(id, agent_id, boot_id, generated_at, received_at, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(report_id),
                    str(report.agent_id),
                    str(report.boot_id),
                    report.generated_at.isoformat(),
                    stored.received_at.isoformat(),
                    report_json,
                ),
            )
        return stored

    async def latest(self, agent_id: UUID) -> StoredReconciliationReport | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_reports WHERE agent_id = ? "
                "ORDER BY received_at DESC, generated_at DESC, id DESC LIMIT 1",
                (str(agent_id),),
            ).fetchone()
        return _reconciliation_from_row(row) if row is not None else None


class SQLiteProtocolMessageJournalRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def record(
        self, record: ProtocolMessageJournalRecord
    ) -> ProtocolMessageRecordDisposition:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM protocol_message_journal WHERE agent_id = ? "
                "AND direction = ? AND message_id = ?",
                (str(record.agent_id), record.direction.value, str(record.message_id)),
            ).fetchone()
            if row is not None:
                existing = _protocol_message_from_row(row)
                if _protocol_fingerprint(existing) != _protocol_fingerprint(record):
                    raise ValueError("Protocol message ID was reused with different content")
                return ProtocolMessageRecordDisposition.DUPLICATE
            try:
                connection.execute(
                    "INSERT INTO protocol_message_journal "
                    "(message_id, agent_id, connection_id, direction, sequence_number, "
                    "message_type, correlation_id, payload_sha256, observed_at, handled_at, "
                    "outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _protocol_message_values(record),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "Protocol connection sequence is already bound to another message"
                ) from exc
        return ProtocolMessageRecordDisposition.RECORDED

    async def get(
        self,
        agent_id: UUID,
        direction: ProtocolMessageDirection,
        message_id: UUID,
    ) -> ProtocolMessageJournalRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM protocol_message_journal WHERE agent_id = ? "
                "AND direction = ? AND message_id = ?",
                (str(agent_id), direction.value, str(message_id)),
            ).fetchone()
        return _protocol_message_from_row(row) if row is not None else None

    async def mark_handled(
        self,
        agent_id: UUID,
        direction: ProtocolMessageDirection,
        message_id: UUID,
        *,
        handled_at: datetime,
        outcome: ProtocolMessageOutcome,
    ) -> ProtocolMessageJournalRecord | None:
        if outcome not in {ProtocolMessageOutcome.HANDLED, ProtocolMessageOutcome.REJECTED}:
            raise ValueError("Only HANDLED or REJECTED can finalize a protocol message")
        timestamp = _as_utc(handled_at)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM protocol_message_journal WHERE agent_id = ? "
                "AND direction = ? AND message_id = ?",
                (str(agent_id), direction.value, str(message_id)),
            ).fetchone()
            if row is None:
                return None
            current = _protocol_message_from_row(row)
            if current.handled_at is not None:
                return current if current.outcome is outcome else None
            updated = ProtocolMessageJournalRecord.model_validate(
                {
                    **current.model_dump(),
                    "handled_at": timestamp,
                    "outcome": outcome,
                }
            )
            cursor = connection.execute(
                "UPDATE protocol_message_journal SET handled_at = ?, outcome = ? "
                "WHERE agent_id = ? AND direction = ? AND message_id = ? "
                "AND handled_at IS NULL",
                (
                    timestamp.isoformat(),
                    outcome.value,
                    str(agent_id),
                    direction.value,
                    str(message_id),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return updated


class SQLiteRemoteArtifactRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, artifact: RemoteArtifactMetadata) -> RemoteArtifactMetadata:
        with self._database.transaction(immediate=True) as connection:
            command_row = connection.execute(
                "SELECT organisation_id, agent_id, operation_id FROM remote_commands WHERE id = ?",
                (str(artifact.command_id),),
            ).fetchone()
            if command_row is None or command_row["agent_id"] != str(artifact.agent_id):
                raise ValueError("Remote artifact command route does not match its Agent")
            if artifact.operation_id is not None and command_row["operation_id"] != str(
                artifact.operation_id
            ):
                raise ValueError("Remote artifact operation does not match its command")
            effective = artifact.model_copy(
                update={"organisation_id": UUID(command_row["organisation_id"])}
            )
            row = connection.execute(
                "SELECT * FROM remote_artifacts WHERE id = ? OR "
                "(agent_id = ? AND local_artifact_id = ?) LIMIT 1",
                (str(effective.id), str(effective.agent_id), str(effective.local_artifact_id)),
            ).fetchone()
            if row is not None:
                existing = _artifact_from_row(row)
                _require_same_artifact_create(existing, effective)
                return existing
            connection.execute(
                "INSERT INTO remote_artifacts "
                "(id, organisation_id, agent_id, local_artifact_id, command_id, "
                "operation_id, name, "
                "artifact_type, content_type, size_bytes, sha256, created_at, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _artifact_values(effective),
            )
        return effective

    async def get(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteArtifactMetadata | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(artifact_id), str(organisation_id))
            if organisation_id is not None
            else (str(artifact_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM remote_artifacts WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _artifact_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        command_id: UUID | None = None,
        limit: int = 500,
    ) -> list[RemoteArtifactMetadata]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        if organisation_id is not None:
            conditions.append("organisation_id = ?")
            values.append(str(organisation_id))
        if agent_id is not None:
            conditions.append("agent_id = ?")
            values.append(str(agent_id))
        if command_id is not None:
            conditions.append("command_id = ?")
            values.append(str(command_id))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM remote_artifacts{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    async def update_uploaded(
        self,
        artifact_id: UUID,
        uploaded_at: datetime,
        *,
        expected_uploaded_at: datetime | None = None,
        organisation_id: UUID | None = None,
    ) -> RemoteArtifactMetadata | None:
        timestamp = _as_utc(uploaded_at)
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        identity_values: tuple[object, ...] = (
            (str(artifact_id), str(organisation_id))
            if organisation_id is not None
            else (str(artifact_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                f"SELECT * FROM remote_artifacts WHERE id = ?{scope}",  # noqa: S608
                identity_values,
            ).fetchone()
            if row is None:
                return None
            current = _artifact_from_row(row)
            if current.uploaded_at != expected_uploaded_at:
                return None
            updated = RemoteArtifactMetadata.model_validate(
                {**current.model_dump(), "uploaded_at": timestamp}
            )
            cursor = connection.execute(
                "UPDATE remote_artifacts SET uploaded_at = ? WHERE id = ? AND uploaded_at IS ?",
                (
                    timestamp.isoformat(),
                    str(artifact_id),
                    _datetime_value(expected_uploaded_at),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return updated


class SQLiteArtifactTransferRepository:
    _TRANSITIONS: dict[ArtifactTransferStatus, frozenset[ArtifactTransferStatus]] = {
        ArtifactTransferStatus.PENDING: frozenset(
            {
                ArtifactTransferStatus.IN_PROGRESS,
                ArtifactTransferStatus.FAILED,
                ArtifactTransferStatus.EXPIRED,
            }
        ),
        ArtifactTransferStatus.IN_PROGRESS: frozenset(
            {
                ArtifactTransferStatus.COMPLETED,
                ArtifactTransferStatus.FAILED,
                ArtifactTransferStatus.EXPIRED,
            }
        ),
        ArtifactTransferStatus.FAILED: frozenset(
            {ArtifactTransferStatus.IN_PROGRESS, ArtifactTransferStatus.EXPIRED}
        ),
    }

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(self, transfer: ArtifactTransferRecord) -> ArtifactTransferRecord:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifact_transfers WHERE id = ? OR token_hash = ? LIMIT 1",
                (str(transfer.id), transfer.token_hash),
            ).fetchone()
            if row is not None:
                existing = _transfer_from_row(row)
                if existing != transfer:
                    raise ValueError("Artifact transfer identity is already bound to other state")
                return existing
            connection.execute(
                "INSERT INTO artifact_transfers "
                "(id, agent_id, artifact_id, direction, status, token_hash, created_at, "
                "expires_at, completed_at, expected_sha256, expected_size_bytes, "
                "attempt_count, error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _transfer_values(transfer),
            )
        return transfer

    async def get(self, transfer_id: UUID) -> ArtifactTransferRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_transfers WHERE id = ?", (str(transfer_id),)
            ).fetchone()
        return _transfer_from_row(row) if row is not None else None

    async def list(
        self,
        *,
        agent_id: UUID | None = None,
        status: ArtifactTransferStatus | None = None,
        limit: int = 500,
    ) -> list[ArtifactTransferRecord]:
        _require_positive_limit(limit)
        conditions: list[str] = []
        values: list[object] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            values.append(str(agent_id))
        if status is not None:
            conditions.append("status = ?")
            values.append(status.value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifact_transfers{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_transfer_from_row(row) for row in rows]

    async def update(
        self,
        transfer: ArtifactTransferRecord,
        *,
        expected_status: ArtifactTransferStatus,
    ) -> ArtifactTransferRecord | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifact_transfers WHERE id = ?", (str(transfer.id),)
            ).fetchone()
            if row is None:
                return None
            current = _transfer_from_row(row)
            if current.status is not expected_status:
                return None
            _require_same_transfer_identity(current, transfer)
            if current.attempt_count != transfer.attempt_count:
                raise ValueError("Transfer attempt_count can only change with an attempt")
            if (
                transfer.status is not expected_status
                and transfer.status not in self._TRANSITIONS.get(expected_status, frozenset())
            ):
                raise ValueError(
                    f"Invalid artifact transfer transition: {expected_status} -> {transfer.status}"
                )
            cursor = connection.execute(
                "UPDATE artifact_transfers SET status = ?, completed_at = ?, "
                "attempt_count = ?, error_code = ? WHERE id = ? AND status = ?",
                (
                    transfer.status.value,
                    _datetime_value(transfer.completed_at),
                    transfer.attempt_count,
                    transfer.error_code,
                    str(transfer.id),
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return transfer

    async def add_attempt(
        self,
        attempt: ArtifactTransferAttempt,
        *,
        expected_attempt_count: int,
    ) -> ArtifactTransferAttempt | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifact_transfer_attempts WHERE id = ? OR "
                "(transfer_id = ? AND attempt_number = ?) LIMIT 1",
                (str(attempt.id), str(attempt.transfer_id), attempt.attempt_number),
            ).fetchone()
            if row is not None:
                existing = _transfer_attempt_from_row(row)
                if not _transfer_attempt_compatible(existing, attempt):
                    raise ValueError("Transfer attempt identity is already bound to other state")
                return existing
            if attempt.attempt_number != expected_attempt_count + 1:
                raise ValueError("Transfer attempt number must follow the persisted attempt count")
            updated = connection.execute(
                "UPDATE artifact_transfers SET attempt_count = attempt_count + 1 "
                "WHERE id = ? AND attempt_count = ?",
                (str(attempt.transfer_id), expected_attempt_count),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO artifact_transfer_attempts "
                "(id, transfer_id, attempt_number, started_at, completed_at, "
                "bytes_transferred, sha256, error_code, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _transfer_attempt_values(attempt),
            )
        return attempt

    async def list_attempts(self, transfer_id: UUID) -> builtins.list[ArtifactTransferAttempt]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_transfer_attempts WHERE transfer_id = ? "
                "ORDER BY attempt_number, id",
                (str(transfer_id),),
            ).fetchall()
        return [_transfer_attempt_from_row(row) for row in rows]

    async def finish_attempt(
        self, attempt: ArtifactTransferAttempt
    ) -> ArtifactTransferAttempt | None:
        if attempt.completed_at is None:
            raise ValueError("A finished transfer attempt requires completed_at")
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifact_transfer_attempts WHERE id = ?", (str(attempt.id),)
            ).fetchone()
            if row is None:
                return None
            current = _transfer_attempt_from_row(row)
            if current.completed_at is not None:
                return current if current == attempt else None
            if (
                current.transfer_id != attempt.transfer_id
                or current.attempt_number != attempt.attempt_number
                or current.started_at != attempt.started_at
            ):
                raise ValueError("Transfer attempt immutable fields changed")
            cursor = connection.execute(
                "UPDATE artifact_transfer_attempts SET completed_at = ?, bytes_transferred = ?, "
                "sha256 = ?, error_code = ?, error_message = ? "
                "WHERE id = ? AND completed_at IS NULL",
                (
                    attempt.completed_at.isoformat(),
                    attempt.bytes_transferred,
                    attempt.sha256,
                    attempt.error_code,
                    attempt.error_message,
                    str(attempt.id),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return attempt


class SQLiteAgentTimelineRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def append(self, entry: AgentTimelineRecord) -> AgentTimelineRecord:
        metadata_json = _dump_json(entry.metadata)
        with self._database.transaction(immediate=True) as connection:
            if entry.deduplication_key is not None:
                deduplicated_row = connection.execute(
                    "SELECT * FROM agent_timelines WHERE deduplication_key = ?",
                    (entry.deduplication_key,),
                ).fetchone()
                if deduplicated_row is not None:
                    existing = _timeline_from_row(deduplicated_row)
                    if _timeline_fingerprint(existing) != _timeline_fingerprint(entry):
                        raise ValueError("Timeline deduplication key is bound to another event")
                    return existing
            id_row = connection.execute(
                "SELECT * FROM agent_timelines WHERE id = ?", (str(entry.id),)
            ).fetchone()
            if id_row is not None:
                existing = _timeline_from_row(id_row)
                if existing != entry:
                    raise ValueError("Timeline deduplication key is bound to another event")
                return existing
            connection.execute(
                "INSERT INTO agent_timelines "
                "(id, agent_id, timestamp, event_type, severity, message, correlation_id, "
                "metadata_json, deduplication_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(entry.id),
                    str(entry.agent_id),
                    entry.timestamp.isoformat(),
                    entry.event_type,
                    entry.severity.value,
                    entry.message,
                    _uuid_value(entry.correlation_id),
                    metadata_json,
                    entry.deduplication_key,
                ),
            )
        return entry

    async def list(
        self,
        agent_id: UUID,
        *,
        severity: AgentTimelineSeverity | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[AgentTimelineRecord]:
        _require_positive_limit(limit)
        conditions = ["agent_id = ?"]
        values: list[object] = [str(agent_id)]
        if severity is not None:
            conditions.append("severity = ?")
            values.append(severity.value)
        if event_type is not None:
            conditions.append("event_type = ?")
            values.append(event_type)
        if since is not None:
            conditions.append("timestamp >= ?")
            values.append(_as_utc(since).isoformat())
        values.append(limit)
        where = " AND ".join(conditions)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_timelines WHERE {where} "  # noqa: S608
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                values,
            ).fetchall()
        return [_timeline_from_row(row) for row in rows]


def _connection_values(record: AgentConnectionRecord) -> tuple[object, ...]:
    return (
        str(record.id),
        str(record.agent_id),
        str(record.boot_id),
        record.protocol_version,
        record.connected_at.isoformat(),
        record.last_heartbeat_at.isoformat(),
        _datetime_value(record.disconnected_at),
        record.last_sequence_number,
        record.observed_clock_offset_seconds,
    )


def _connection_from_row(row: sqlite3.Row) -> AgentConnectionRecord:
    return AgentConnectionRecord(
        id=UUID(row["id"]),
        agent_id=UUID(row["agent_id"]),
        boot_id=UUID(row["boot_id"]),
        protocol_version=row["protocol_version"],
        connected_at=_parse_datetime(row["connected_at"]),
        last_heartbeat_at=_parse_datetime(row["last_heartbeat_at"]),
        disconnected_at=_parse_optional_datetime(row["disconnected_at"]),
        last_sequence_number=row["last_sequence_number"],
        observed_clock_offset_seconds=row["observed_clock_offset_seconds"],
    )


def _bench_values(bench: GlobalBenchRecord) -> tuple[object, ...]:
    return (
        bench.id,
        str(bench.organisation_id),
        str(bench.agent_id),
        bench.agent_slug,
        bench.local_bench_id,
        bench.name,
        bench.backend_id,
        bench.kind.value,
        bench.target_type,
        bench.status.value,
        bench.health.value,
        _dump_json(sorted(bench.capabilities)),
        _dump_json(bench.labels),
        bench.firmware_version,
        _datetime_value(bench.last_seen_at),
        bench.created_at.isoformat(),
        bench.updated_at.isoformat(),
    )


def _upsert_bench(connection: sqlite3.Connection, bench: GlobalBenchRecord) -> None:
    agent_row = connection.execute(
        "SELECT organisation_id FROM agents WHERE id = ?", (str(bench.agent_id),)
    ).fetchone()
    if agent_row is None:
        raise ValueError("Global bench Agent does not exist")
    bench = bench.model_copy(update={"organisation_id": UUID(agent_row["organisation_id"])})
    id_row = connection.execute("SELECT * FROM global_benches WHERE id = ?", (bench.id,)).fetchone()
    local_row = connection.execute(
        "SELECT * FROM global_benches WHERE agent_id = ? AND local_bench_id = ?",
        (str(bench.agent_id), bench.local_bench_id),
    ).fetchone()
    if local_row is not None and local_row["id"] != bench.id:
        raise ValueError("Agent-local bench identity is already bound to another global ID")
    if id_row is not None:
        existing = _bench_from_row(id_row)
        if (
            existing.agent_id != bench.agent_id
            or existing.organisation_id != bench.organisation_id
            or existing.agent_slug != bench.agent_slug
            or existing.local_bench_id != bench.local_bench_id
        ):
            raise ValueError("Global bench ID is already bound to another identity")
        if bench.updated_at < existing.updated_at:
            raise ValueError("A stale bench update cannot replace newer inventory state")
        incoming_labels = dict(bench.labels)
        explicitly_cleared = incoming_labels.get(BENCH_MAINTENANCE_LABEL) == "false"
        if explicitly_cleared:
            incoming_labels.pop(BENCH_MAINTENANCE_LABEL, None)
            incoming_labels.pop(BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL, None)
            bench = bench.model_copy(update={"labels": incoming_labels})
        elif existing.labels.get(BENCH_MAINTENANCE_LABEL) == "true":
            incoming_labels[BENCH_MAINTENANCE_LABEL] = "true"
            incoming_labels[BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL] = existing.labels.get(
                BENCH_MAINTENANCE_PREVIOUS_STATUS_LABEL,
                existing.status.value,
            )
            bench = bench.model_copy(
                update={
                    "labels": incoming_labels,
                    "status": (
                        GlobalBenchStatus.OFFLINE
                        if bench.status is GlobalBenchStatus.OFFLINE
                        else GlobalBenchStatus.DEGRADED
                    ),
                }
            )
        connection.execute(
            "UPDATE global_benches SET name = ?, backend_id = ?, kind = ?, target_type = ?, "
            "status = ?, health = ?, capabilities_json = ?, labels_json = ?, "
            "firmware_version = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
            (
                bench.name,
                bench.backend_id,
                bench.kind.value,
                bench.target_type,
                bench.status.value,
                bench.health.value,
                _dump_json(sorted(bench.capabilities)),
                _dump_json(bench.labels),
                bench.firmware_version,
                _datetime_value(bench.last_seen_at),
                bench.updated_at.isoformat(),
                bench.id,
            ),
        )
        return
    connection.execute(
        "INSERT INTO global_benches "
        "(id, organisation_id, agent_id, agent_slug, local_bench_id, name, backend_id, "
        "kind, target_type, "
        "status, health, capabilities_json, labels_json, firmware_version, last_seen_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _bench_values(bench),
    )


def _bench_from_row(row: sqlite3.Row) -> GlobalBenchRecord:
    capabilities = _load_json(row["capabilities_json"])
    labels = _load_json(row["labels_json"])
    if not isinstance(capabilities, list) or not isinstance(labels, dict):
        raise ValueError("Persisted global bench JSON has an invalid shape")
    return GlobalBenchRecord(
        id=row["id"],
        organisation_id=UUID(row["organisation_id"]),
        agent_id=UUID(row["agent_id"]),
        agent_slug=row["agent_slug"],
        local_bench_id=row["local_bench_id"],
        name=row["name"],
        backend_id=row["backend_id"],
        kind=GlobalBenchKind(row["kind"]),
        target_type=row["target_type"],
        status=GlobalBenchStatus(row["status"]),
        health=HealthStatus(row["health"]),
        capabilities=frozenset(cast(list[str], capabilities)),
        labels=cast(dict[str, str], labels),
        firmware_version=row["firmware_version"],
        last_seen_at=_parse_optional_datetime(row["last_seen_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _list_benches(
    connection: sqlite3.Connection,
    *,
    agent_id: UUID,
    organisation_id: UUID | None = None,
) -> list[GlobalBenchRecord]:
    scope = " AND organisation_id = ?" if organisation_id is not None else ""
    values: tuple[object, ...] = (
        (str(agent_id), str(organisation_id)) if organisation_id is not None else (str(agent_id),)
    )
    rows = connection.execute(
        f"SELECT * FROM global_benches WHERE agent_id = ?{scope} ORDER BY id",  # noqa: S608
        values,
    ).fetchall()
    return [_bench_from_row(row) for row in rows]


def _snapshot_organisation_id(
    connection: sqlite3.Connection,
    snapshot: StoredBenchSnapshot,
) -> UUID:
    row = connection.execute(
        "SELECT organisation_id FROM agents WHERE id = ?", (str(snapshot.agent_id),)
    ).fetchone()
    if row is None:
        raise ValueError("Inventory snapshot Agent does not exist")
    organisation_id = UUID(row["organisation_id"])
    if any(bench.organisation_id != organisation_id for bench in snapshot.benches):
        raise ValueError("Inventory snapshot organisation does not match its Agent")
    return organisation_id


def _snapshot_from_row(row: sqlite3.Row) -> StoredBenchSnapshot:
    raw = _load_json(row["snapshot_json"])
    if not isinstance(raw, list):
        raise ValueError("Persisted bench snapshot JSON must be an array")
    benches = tuple(GlobalBenchRecord.model_validate(item) for item in raw)
    if row["bench_count"] != len(benches):
        raise ValueError("Persisted bench snapshot count does not match its payload")
    return StoredBenchSnapshot(
        id=UUID(row["id"]),
        agent_id=UUID(row["agent_id"]),
        boot_id=UUID(row["boot_id"]),
        generated_at=_parse_datetime(row["generated_at"]),
        received_at=_parse_datetime(row["received_at"]),
        benches=benches,
    )


def _command_values(command: RemoteCommand) -> tuple[object, ...]:
    return (
        str(command.id),
        str(command.organisation_id),
        str(command.agent_id),
        command.bench_id,
        command.command_type.value,
        _dump_json(command.payload),
        command.status.value,
        command.created_at.isoformat(),
        _datetime_value(command.dispatched_at),
        _datetime_value(command.acknowledged_at),
        _datetime_value(command.started_at),
        _datetime_value(command.completed_at),
        command.expires_at.isoformat(),
        command.idempotency_key,
        command.attempt_count,
        _uuid_value(command.operation_id),
        _uuid_value(command.reservation_id),
        command.lease_version,
        command.error_code,
        command.error_message,
        (
            _dump_json(command.actor_context.model_dump(mode="json"))
            if command.actor_context is not None
            else None
        ),
        _uuid_value(command.authorisation_snapshot_id),
    )


def _require_command_route(connection: sqlite3.Connection, command: RemoteCommand) -> None:
    row = connection.execute(
        "SELECT agents.organisation_id AS agent_organisation_id, "
        "global_benches.organisation_id AS bench_organisation_id, "
        "global_benches.agent_id AS bench_agent_id FROM agents "
        "JOIN global_benches ON global_benches.id = ? WHERE agents.id = ?",
        (command.bench_id, str(command.agent_id)),
    ).fetchone()
    if (
        row is None
        or row["bench_agent_id"] != str(command.agent_id)
        or row["agent_organisation_id"] != str(command.organisation_id)
        or row["bench_organisation_id"] != str(command.organisation_id)
        or command.actor_context is not None
        and command.actor_context.organisation_id != command.organisation_id
    ):
        raise ValueError("Remote command organisation does not match its Agent and bench route")


def _require_operation_route(
    connection: sqlite3.Connection,
    operation: DistributedOperation,
) -> None:
    row = connection.execute(
        "SELECT organisation_id, agent_id, bench_id FROM remote_commands WHERE id = ?",
        (str(operation.remote_command_id),),
    ).fetchone()
    if (
        row is None
        or row["organisation_id"] != str(operation.organisation_id)
        or row["agent_id"] != str(operation.agent_id)
        or row["bench_id"] != operation.bench_id
    ):
        raise ValueError("Distributed operation organisation does not match its command route")


def _command_from_row(row: sqlite3.Row) -> RemoteCommand:
    payload = _load_json(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("Persisted remote command payload must be an object")
    columns = set(row.keys())
    actor_context = None
    if "actor_context_json" in columns and row["actor_context_json"] is not None:
        raw_actor_context = _load_json(row["actor_context_json"])
        if not isinstance(raw_actor_context, dict):
            raise ValueError("Persisted command actor context must be an object")
        actor_context = ActorContext.model_validate(raw_actor_context)
    return RemoteCommand(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        command_type=RemoteCommandType(row["command_type"]),
        payload=cast(dict[str, Any], payload),
        status=RemoteCommandStatus(row["status"]),
        created_at=_parse_datetime(row["created_at"]),
        dispatched_at=_parse_optional_datetime(row["dispatched_at"]),
        acknowledged_at=_parse_optional_datetime(row["acknowledged_at"]),
        started_at=_parse_optional_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        idempotency_key=row["idempotency_key"],
        attempt_count=row["attempt_count"],
        operation_id=_parse_optional_uuid(row["operation_id"]),
        reservation_id=_parse_optional_uuid(row["reservation_id"]),
        lease_version=row["lease_version"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        actor_context=actor_context,
        authorisation_snapshot_id=(
            _parse_optional_uuid(row["authorisation_snapshot_id"])
            if "authorisation_snapshot_id" in columns
            else None
        ),
    )


def _require_same_command_request(existing: RemoteCommand, requested: RemoteCommand) -> None:
    if (
        existing.agent_id,
        existing.organisation_id,
        existing.bench_id,
        existing.command_type,
        existing.payload,
        existing.idempotency_key,
        existing.reservation_id,
        existing.lease_version,
        _stable_actor_identity(existing.actor_context),
    ) != (
        requested.agent_id,
        requested.organisation_id,
        requested.bench_id,
        requested.command_type,
        requested.payload,
        requested.idempotency_key,
        requested.reservation_id,
        requested.lease_version,
        _stable_actor_identity(requested.actor_context),
    ):
        raise ValueError("Remote command idempotency key was reused for another request")


def _stable_actor_identity(actor: ActorContext | None) -> tuple[object, ...] | None:
    if actor is None:
        return None
    return (
        actor.principal_id,
        actor.principal_type,
        actor.organisation_id,
    )


def _require_same_command_identity(existing: RemoteCommand, updated: RemoteCommand) -> None:
    _require_same_command_request(existing, updated)
    if (
        existing.id != updated.id
        or existing.created_at != updated.created_at
        or existing.expires_at != updated.expires_at
    ):
        raise ValueError("Remote command immutable fields changed")


def _require_remote_command_transition(
    previous: RemoteCommandStatus, updated: RemoteCommandStatus
) -> None:
    if updated is previous:
        return
    if updated not in REMOTE_COMMAND_TRANSITIONS.get(previous, frozenset()):
        raise ValueError(f"Invalid remote command transition: {previous} -> {updated}")


def _command_attempt_values(attempt: RemoteCommandAttempt) -> tuple[object, ...]:
    return (
        str(attempt.id),
        str(attempt.command_id),
        attempt.attempt_number,
        _uuid_value(attempt.connection_id),
        attempt.sequence_number,
        attempt.dispatched_at.isoformat(),
        _datetime_value(attempt.acknowledged_at),
        _datetime_value(attempt.failed_at),
        attempt.error_code,
    )


def _command_attempt_from_row(row: sqlite3.Row) -> RemoteCommandAttempt:
    return RemoteCommandAttempt(
        id=UUID(row["id"]),
        command_id=UUID(row["command_id"]),
        attempt_number=row["attempt_number"],
        connection_id=_parse_optional_uuid(row["connection_id"]),
        sequence_number=row["sequence_number"],
        dispatched_at=_parse_datetime(row["dispatched_at"]),
        acknowledged_at=_parse_optional_datetime(row["acknowledged_at"]),
        failed_at=_parse_optional_datetime(row["failed_at"]),
        error_code=row["error_code"],
    )


def _command_attempt_compatible(
    existing: RemoteCommandAttempt, requested: RemoteCommandAttempt
) -> bool:
    immutable_matches = (
        existing.id,
        existing.command_id,
        existing.attempt_number,
        existing.connection_id,
        existing.sequence_number,
        existing.dispatched_at,
    ) == (
        requested.id,
        requested.command_id,
        requested.attempt_number,
        requested.connection_id,
        requested.sequence_number,
        requested.dispatched_at,
    )
    requested_is_initial = requested.acknowledged_at is None and requested.failed_at is None
    return immutable_matches and (requested_is_initial or existing == requested)


def _operation_values(operation: DistributedOperation) -> tuple[object, ...]:
    return (
        str(operation.id),
        str(operation.organisation_id),
        str(operation.remote_command_id),
        str(operation.agent_id),
        operation.bench_id,
        _uuid_value(operation.reservation_id),
        operation.operation_type,
        operation.status.value,
        operation.progress,
        operation.message,
        (
            _dump_json_mapping(
                operation.result,
                field="Distributed operation result",
                maximum_bytes=1_048_576,
            )
            if operation.result is not None
            else None
        ),
        operation.created_at.isoformat(),
        _datetime_value(operation.dispatched_at),
        _datetime_value(operation.started_at),
        _datetime_value(operation.completed_at),
        _datetime_value(operation.last_agent_update_at),
        _datetime_value(operation.reconciliation_deadline),
        operation.error_code,
        operation.error_message,
    )


def _operation_from_row(row: sqlite3.Row) -> DistributedOperation:
    result = _load_json(row["result_json"]) if row["result_json"] is not None else None
    if result is not None and not isinstance(result, dict):
        raise ValueError("Persisted distributed operation result must be an object")
    return DistributedOperation(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        remote_command_id=UUID(row["remote_command_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        reservation_id=_parse_optional_uuid(row["reservation_id"]),
        operation_type=row["operation_type"],
        status=DistributedOperationStatus(row["status"]),
        progress=row["progress"],
        message=row["message"],
        result=cast(dict[str, Any] | None, result),
        created_at=_parse_datetime(row["created_at"]),
        dispatched_at=_parse_optional_datetime(row["dispatched_at"]),
        started_at=_parse_optional_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        last_agent_update_at=_parse_optional_datetime(row["last_agent_update_at"]),
        reconciliation_deadline=_parse_optional_datetime(row["reconciliation_deadline"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _require_same_operation_identity(
    existing: DistributedOperation, updated: DistributedOperation
) -> None:
    if (
        existing.id,
        existing.remote_command_id,
        existing.organisation_id,
        existing.agent_id,
        existing.bench_id,
        existing.reservation_id,
        existing.operation_type,
        existing.created_at,
    ) != (
        updated.id,
        updated.remote_command_id,
        updated.organisation_id,
        updated.agent_id,
        updated.bench_id,
        updated.reservation_id,
        updated.operation_type,
        updated.created_at,
    ):
        raise ValueError("Distributed operation immutable fields changed")


def _require_same_operation_create(
    existing: DistributedOperation, requested: DistributedOperation
) -> None:
    if (
        existing.remote_command_id,
        existing.organisation_id,
        existing.agent_id,
        existing.bench_id,
        existing.reservation_id,
        existing.operation_type,
        existing.created_at,
    ) != (
        requested.remote_command_id,
        requested.organisation_id,
        requested.agent_id,
        requested.bench_id,
        requested.reservation_id,
        requested.operation_type,
        requested.created_at,
    ):
        raise ValueError("Remote operation is already bound to different state")


def _require_operation_transition(
    previous: DistributedOperationStatus, updated: DistributedOperationStatus
) -> None:
    if updated is previous:
        return
    if updated not in DISTRIBUTED_OPERATION_TRANSITIONS.get(previous, frozenset()):
        raise ValueError(f"Invalid distributed operation transition: {previous} -> {updated}")


def _lease_values(lease: ReservationLease) -> tuple[object, ...]:
    return (
        str(lease.reservation_id),
        str(lease.agent_id),
        lease.bench_id,
        lease.owner,
        lease.valid_from.isoformat(),
        lease.valid_until.isoformat(),
        lease.lease_version,
        _datetime_value(lease.released_at),
    )


def _lease_from_row(row: sqlite3.Row) -> ReservationLease:
    return ReservationLease(
        reservation_id=UUID(row["reservation_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        valid_from=_parse_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        lease_version=row["lease_version"],
        released_at=_parse_optional_datetime(row["released_at"]),
    )


def _reconciliation_from_row(row: sqlite3.Row) -> StoredReconciliationReport:
    raw = _load_json(row["report_json"])
    if not isinstance(raw, dict):
        raise ValueError("Persisted reconciliation report must be an object")
    report = ReconciliationReport.model_validate(raw)
    if report.agent_id != UUID(row["agent_id"]) or report.boot_id != UUID(row["boot_id"]):
        raise ValueError("Persisted reconciliation report identity columns disagree")
    if report.generated_at != _parse_datetime(row["generated_at"]):
        raise ValueError("Persisted reconciliation report timestamp columns disagree")
    return StoredReconciliationReport(
        id=UUID(row["id"]),
        received_at=_parse_datetime(row["received_at"]),
        report=report,
    )


def _protocol_message_values(record: ProtocolMessageJournalRecord) -> tuple[object, ...]:
    return (
        str(record.message_id),
        str(record.agent_id),
        _uuid_value(record.connection_id),
        record.direction.value,
        record.sequence_number,
        record.message_type,
        _uuid_value(record.correlation_id),
        record.payload_sha256,
        record.observed_at.isoformat(),
        _datetime_value(record.handled_at),
        record.outcome.value,
    )


def _protocol_message_from_row(row: sqlite3.Row) -> ProtocolMessageJournalRecord:
    return ProtocolMessageJournalRecord(
        message_id=UUID(row["message_id"]),
        agent_id=UUID(row["agent_id"]),
        connection_id=_parse_optional_uuid(row["connection_id"]),
        direction=ProtocolMessageDirection(row["direction"]),
        sequence_number=row["sequence_number"],
        message_type=row["message_type"],
        correlation_id=_parse_optional_uuid(row["correlation_id"]),
        payload_sha256=row["payload_sha256"],
        observed_at=_parse_datetime(row["observed_at"]),
        handled_at=_parse_optional_datetime(row["handled_at"]),
        outcome=ProtocolMessageOutcome(row["outcome"]),
    )


def _protocol_fingerprint(record: ProtocolMessageJournalRecord) -> tuple[object, ...]:
    # Message IDs identify application messages across transport reconnects. Connection IDs and
    # sequence numbers are deliberately excluded so an unacknowledged message can be replayed on
    # a new ordered stream without looking like ID reuse; the per-connection unique index still
    # fences two distinct messages from claiming the same transport sequence.
    return (
        record.agent_id,
        record.direction,
        record.message_type,
        record.correlation_id,
        record.payload_sha256,
    )


def _artifact_values(artifact: RemoteArtifactMetadata) -> tuple[object, ...]:
    return (
        str(artifact.id),
        str(artifact.organisation_id),
        str(artifact.agent_id),
        str(artifact.local_artifact_id),
        str(artifact.command_id),
        _uuid_value(artifact.operation_id),
        artifact.name,
        artifact.artifact_type,
        artifact.content_type,
        artifact.size_bytes,
        artifact.sha256,
        artifact.created_at.isoformat(),
        _datetime_value(artifact.uploaded_at),
    )


def _artifact_from_row(row: sqlite3.Row) -> RemoteArtifactMetadata:
    return RemoteArtifactMetadata(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        agent_id=UUID(row["agent_id"]),
        local_artifact_id=UUID(row["local_artifact_id"]),
        command_id=UUID(row["command_id"]),
        operation_id=_parse_optional_uuid(row["operation_id"]),
        name=row["name"],
        artifact_type=row["artifact_type"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=_parse_datetime(row["created_at"]),
        uploaded_at=_parse_optional_datetime(row["uploaded_at"]),
    )


def _require_same_artifact_create(
    existing: RemoteArtifactMetadata, requested: RemoteArtifactMetadata
) -> None:
    if (
        existing.organisation_id,
        existing.agent_id,
        existing.local_artifact_id,
        existing.command_id,
        existing.operation_id,
        existing.name,
        existing.artifact_type,
        existing.content_type,
        existing.size_bytes,
        existing.sha256,
        existing.created_at,
    ) != (
        requested.organisation_id,
        requested.agent_id,
        requested.local_artifact_id,
        requested.command_id,
        requested.operation_id,
        requested.name,
        requested.artifact_type,
        requested.content_type,
        requested.size_bytes,
        requested.sha256,
        requested.created_at,
    ):
        raise ValueError("Remote artifact identity is already bound to other metadata")


def _transfer_values(transfer: ArtifactTransferRecord) -> tuple[object, ...]:
    return (
        str(transfer.id),
        str(transfer.agent_id),
        str(transfer.artifact_id),
        transfer.direction.value,
        transfer.status.value,
        transfer.token_hash,
        transfer.created_at.isoformat(),
        transfer.expires_at.isoformat(),
        _datetime_value(transfer.completed_at),
        transfer.expected_sha256,
        transfer.expected_size_bytes,
        transfer.attempt_count,
        transfer.error_code,
    )


def _transfer_from_row(row: sqlite3.Row) -> ArtifactTransferRecord:
    from lab_platform.models import ArtifactTransferDirection

    return ArtifactTransferRecord(
        id=UUID(row["id"]),
        agent_id=UUID(row["agent_id"]),
        artifact_id=UUID(row["artifact_id"]),
        direction=ArtifactTransferDirection(row["direction"]),
        status=ArtifactTransferStatus(row["status"]),
        token_hash=row["token_hash"],
        created_at=_parse_datetime(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        expected_sha256=row["expected_sha256"],
        expected_size_bytes=row["expected_size_bytes"],
        attempt_count=row["attempt_count"],
        error_code=row["error_code"],
    )


def _require_same_transfer_identity(
    existing: ArtifactTransferRecord, updated: ArtifactTransferRecord
) -> None:
    if (
        existing.id,
        existing.agent_id,
        existing.artifact_id,
        existing.direction,
        existing.token_hash,
        existing.created_at,
        existing.expires_at,
        existing.expected_sha256,
        existing.expected_size_bytes,
    ) != (
        updated.id,
        updated.agent_id,
        updated.artifact_id,
        updated.direction,
        updated.token_hash,
        updated.created_at,
        updated.expires_at,
        updated.expected_sha256,
        updated.expected_size_bytes,
    ):
        raise ValueError("Artifact transfer immutable fields changed")


def _transfer_attempt_values(attempt: ArtifactTransferAttempt) -> tuple[object, ...]:
    return (
        str(attempt.id),
        str(attempt.transfer_id),
        attempt.attempt_number,
        attempt.started_at.isoformat(),
        _datetime_value(attempt.completed_at),
        attempt.bytes_transferred,
        attempt.sha256,
        attempt.error_code,
        attempt.error_message,
    )


def _transfer_attempt_from_row(row: sqlite3.Row) -> ArtifactTransferAttempt:
    return ArtifactTransferAttempt(
        id=UUID(row["id"]),
        transfer_id=UUID(row["transfer_id"]),
        attempt_number=row["attempt_number"],
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        bytes_transferred=row["bytes_transferred"],
        sha256=row["sha256"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _transfer_attempt_compatible(
    existing: ArtifactTransferAttempt, requested: ArtifactTransferAttempt
) -> bool:
    immutable_matches = (
        existing.id,
        existing.transfer_id,
        existing.attempt_number,
        existing.started_at,
    ) == (
        requested.id,
        requested.transfer_id,
        requested.attempt_number,
        requested.started_at,
    )
    return immutable_matches and (requested.completed_at is None or existing == requested)


def _timeline_from_row(row: sqlite3.Row) -> AgentTimelineRecord:
    metadata = _load_json(row["metadata_json"])
    if not isinstance(metadata, dict):
        raise ValueError("Persisted Agent timeline metadata must be an object")
    return AgentTimelineRecord(
        id=UUID(row["id"]),
        agent_id=UUID(row["agent_id"]),
        timestamp=_parse_datetime(row["timestamp"]),
        event_type=row["event_type"],
        severity=AgentTimelineSeverity(row["severity"]),
        message=row["message"],
        correlation_id=_parse_optional_uuid(row["correlation_id"]),
        metadata=cast(dict[str, Any], metadata),
        deduplication_key=row["deduplication_key"],
    )


def _timeline_fingerprint(entry: AgentTimelineRecord) -> tuple[object, ...]:
    return (
        entry.agent_id,
        entry.timestamp,
        entry.event_type,
        entry.severity,
        entry.message,
        entry.correlation_id,
        entry.metadata,
        entry.deduplication_key,
    )


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _dump_json_mapping(value: object, *, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    try:
        encoded = _dump_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} serialized bytes")
    return encoded


def _load_json(value: str) -> object:
    return json.loads(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Distributed persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_value(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _uuid_value(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _parse_optional_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value is not None else None


def _require_positive_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("limit must be positive")
