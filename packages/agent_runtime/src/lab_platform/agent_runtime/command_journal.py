from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from lab_platform.agent_protocol import CommandRequestPayload
from lab_platform.control_plane_core.errors import (
    AgentRestartedDuringOperationError,
    RemoteCommandDuplicateError,
)
from lab_platform.models import (
    TERMINAL_REMOTE_COMMAND_STATUSES,
    CommandJournalEntry,
    RemoteCommandStatus,
    RemoteCommandType,
)
from lab_platform.persistence import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class StoredCommandJournalEntry:
    """A public journal entry plus its immutable execution-content fingerprint."""

    entry: CommandJournalEntry
    command_fingerprint: str


@dataclass(frozen=True, slots=True)
class CommandJournalCreateResult:
    record: StoredCommandJournalEntry
    created: bool

    @property
    def replayed(self) -> bool:
        return not self.created


class AgentCommandJournalRepository(Protocol):
    """Durable Agent-local boundary used before acknowledging remote commands."""

    async def recover_interrupted(
        self,
        *,
        boot_id: UUID,
        recovered_at: datetime,
    ) -> list[StoredCommandJournalEntry]: ...

    async def find_replay(
        self,
        request: CommandRequestPayload,
    ) -> StoredCommandJournalEntry | None: ...

    async def create_or_replay(
        self,
        request: CommandRequestPayload,
        *,
        received_at: datetime,
    ) -> CommandJournalCreateResult: ...

    async def get(self, command_id: UUID) -> StoredCommandJournalEntry | None: ...

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> StoredCommandJournalEntry | None: ...

    async def mark_running(
        self,
        command_id: UUID,
        *,
        started_at: datetime,
    ) -> StoredCommandJournalEntry: ...

    async def complete(
        self,
        command_id: UUID,
        *,
        status: RemoteCommandStatus,
        completed_at: datetime,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> StoredCommandJournalEntry: ...

    async def list(
        self,
        *,
        status: RemoteCommandStatus | None = None,
        limit: int = 10_000,
    ) -> list[StoredCommandJournalEntry]: ...


class SQLiteAgentCommandJournal:
    """Schema-v7 SQLite command journal with atomic collision detection.

    Both ``command_id`` and ``idempotency_key`` are replay keys. A retry is accepted only when
    its immutable execution content has the same SHA-256 fingerprint. The write transaction is
    immediate, so two repository instances cannot both decide to execute the same command.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def recover_interrupted(
        self,
        *,
        boot_id: UUID,
        recovered_at: datetime,
    ) -> list[StoredCommandJournalEntry]:
        """Fence active journal entries when a new Agent process takes ownership.

        Execution tasks are process-local and cannot survive an Agent restart. The boot marker and
        all active-to-failed transitions share one immediate transaction, so concurrent journal
        instances cannot preserve or create ambiguous ownership. Reopening the journal from the
        same boot is intentionally a no-op; transport reconnects therefore retain active work.
        """

        recovered = _as_utc(recovered_at)
        marker_key = "agent_command_journal:active_boot_id"
        current_boot = str(boot_id)
        interrupted: list[StoredCommandJournalEntry] = []
        with self._database.transaction(immediate=True) as connection:
            marker = connection.execute(
                "SELECT value FROM agent_runtime_metadata WHERE key = ?",
                (marker_key,),
            ).fetchone()
            if marker is not None and marker["value"] == current_boot:
                return []

            rows = connection.execute(
                "SELECT * FROM agent_command_journal WHERE status IN (?, ?) "
                "ORDER BY received_at, command_id",
                (
                    RemoteCommandStatus.ACCEPTED.value,
                    RemoteCommandStatus.RUNNING.value,
                ),
            ).fetchall()
            for row in rows:
                record = _record_from_row(row)
                completed_at = max(
                    recovered,
                    record.entry.started_at or record.entry.received_at,
                )
                entry = CommandJournalEntry.model_validate(
                    {
                        **record.entry.model_dump(),
                        "status": RemoteCommandStatus.FAILED,
                        "completed_at": completed_at,
                        "result": None,
                        "error_code": AgentRestartedDuringOperationError.code,
                        "error_message": (
                            "Agent process restarted before command execution completed."
                        ),
                    }
                )
                connection.execute(
                    "UPDATE agent_command_journal SET status = ?, completed_at = ?, "
                    "result_json = NULL, error_code = ?, error_message = ? "
                    "WHERE command_id = ? AND status IN (?, ?)",
                    (
                        entry.status.value,
                        entry.completed_at.isoformat() if entry.completed_at else None,
                        entry.error_code,
                        entry.error_message,
                        str(entry.command_id),
                        RemoteCommandStatus.ACCEPTED.value,
                        RemoteCommandStatus.RUNNING.value,
                    ),
                )
                interrupted.append(
                    StoredCommandJournalEntry(
                        entry=entry,
                        command_fingerprint=record.command_fingerprint,
                    )
                )

            connection.execute(
                "INSERT INTO agent_runtime_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (marker_key, current_boot),
            )
        return interrupted

    async def find_replay(
        self,
        request: CommandRequestPayload,
    ) -> StoredCommandJournalEntry | None:
        fingerprint = command_fingerprint(request)
        with self._database.transaction() as connection:
            return _find_replay(connection, request, fingerprint)

    async def create_or_replay(
        self,
        request: CommandRequestPayload,
        *,
        received_at: datetime,
    ) -> CommandJournalCreateResult:
        received = _as_utc(received_at)
        fingerprint = command_fingerprint(request)
        command = request.command
        with self._database.transaction(immediate=True) as connection:
            existing = _find_replay(connection, request, fingerprint)
            if existing is not None:
                return CommandJournalCreateResult(record=existing, created=False)

            entry = CommandJournalEntry(
                command_id=command.id,
                idempotency_key=command.idempotency_key,
                command_type=command.command_type,
                bench_id=command.bench_id,
                status=RemoteCommandStatus.ACCEPTED,
                received_at=received,
            )
            try:
                connection.execute(
                    "INSERT INTO agent_command_journal "
                    "(command_id, idempotency_key, command_fingerprint, command_type, "
                    "bench_id, status, received_at, started_at, completed_at, result_json, "
                    "error_code, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, "
                    "NULL, NULL, NULL)",
                    (
                        str(entry.command_id),
                        entry.idempotency_key,
                        fingerprint,
                        entry.command_type.value,
                        entry.bench_id,
                        entry.status.value,
                        entry.received_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - transaction fences this
                raise RemoteCommandDuplicateError(
                    "Command identity is already bound to different content.",
                    command_id=str(command.id),
                    idempotency_key=command.idempotency_key,
                ) from exc
        return CommandJournalCreateResult(
            record=StoredCommandJournalEntry(entry=entry, command_fingerprint=fingerprint),
            created=True,
        )

    async def get(self, command_id: UUID) -> StoredCommandJournalEntry | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_command_journal WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> StoredCommandJournalEntry | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_command_journal WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def mark_running(
        self,
        command_id: UUID,
        *,
        started_at: datetime,
    ) -> StoredCommandJournalEntry:
        started = _as_utc(started_at)
        with self._database.transaction(immediate=True) as connection:
            row = _require_row(connection, command_id)
            current = _record_from_row(row)
            if current.entry.status is not RemoteCommandStatus.ACCEPTED:
                return current
            updated_entry = CommandJournalEntry.model_validate(
                {
                    **current.entry.model_dump(),
                    "status": RemoteCommandStatus.RUNNING,
                    "started_at": started,
                }
            )
            connection.execute(
                "UPDATE agent_command_journal SET status = ?, started_at = ? "
                "WHERE command_id = ? AND status = ?",
                (
                    updated_entry.status.value,
                    updated_entry.started_at.isoformat() if updated_entry.started_at else None,
                    str(command_id),
                    RemoteCommandStatus.ACCEPTED.value,
                ),
            )
        return StoredCommandJournalEntry(
            entry=updated_entry,
            command_fingerprint=current.command_fingerprint,
        )

    async def complete(
        self,
        command_id: UUID,
        *,
        status: RemoteCommandStatus,
        completed_at: datetime,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> StoredCommandJournalEntry:
        if status not in TERMINAL_REMOTE_COMMAND_STATUSES:
            raise ValueError("Command journal completion requires a terminal status")
        completed = _as_utc(completed_at)
        with self._database.transaction(immediate=True) as connection:
            row = _require_row(connection, command_id)
            current = _record_from_row(row)
            if current.entry.status in TERMINAL_REMOTE_COMMAND_STATUSES:
                return current
            if current.entry.status not in {
                RemoteCommandStatus.ACCEPTED,
                RemoteCommandStatus.RUNNING,
            }:
                raise ValueError(
                    f"Cannot complete command from {current.entry.status.value} status"
                )
            updated_entry = CommandJournalEntry.model_validate(
                {
                    **current.entry.model_dump(),
                    "status": status,
                    "completed_at": completed,
                    "result": result,
                    "error_code": error_code,
                    "error_message": error_message,
                }
            )
            connection.execute(
                "UPDATE agent_command_journal SET status = ?, completed_at = ?, "
                "result_json = ?, error_code = ?, error_message = ? WHERE command_id = ?",
                (
                    updated_entry.status.value,
                    updated_entry.completed_at.isoformat()
                    if updated_entry.completed_at is not None
                    else None,
                    _dump_json(updated_entry.result) if updated_entry.result is not None else None,
                    updated_entry.error_code,
                    updated_entry.error_message,
                    str(command_id),
                ),
            )
        return StoredCommandJournalEntry(
            entry=updated_entry,
            command_fingerprint=current.command_fingerprint,
        )

    async def list(
        self,
        *,
        status: RemoteCommandStatus | None = None,
        limit: int = 10_000,
    ) -> list[StoredCommandJournalEntry]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("Command journal limit must be a positive integer")
        values: tuple[object, ...]
        if status is None:
            query = (
                "SELECT * FROM agent_command_journal ORDER BY received_at DESC, command_id LIMIT ?"
            )
            values = (limit,)
        else:
            query = (
                "SELECT * FROM agent_command_journal WHERE status = ? "
                "ORDER BY received_at DESC, command_id LIMIT ?"
            )
            values = (status.value, limit)
        with self._database.transaction() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_record_from_row(row) for row in rows]


# Short alias for callers that already live in the Agent runtime package.
SQLiteCommandJournal = SQLiteAgentCommandJournal
CommandJournalRepository = AgentCommandJournalRepository


def command_fingerprint(request: CommandRequestPayload) -> str:
    """Hash immutable execution inputs, excluding mutable delivery-attempt state."""

    if not isinstance(request, CommandRequestPayload):
        raise TypeError("request must be a CommandRequestPayload")
    command = request.command
    document = {
        "agent_id": str(command.agent_id),
        "bench_id": command.bench_id,
        "command_type": command.command_type.value,
        "payload": _durable_command_payload(command.command_type, command.payload),
        "created_at": command.created_at.isoformat(),
        "expires_at": command.expires_at.isoformat(),
        "operation_id": str(command.operation_id) if command.operation_id is not None else None,
        "reservation_id": (
            str(command.reservation_id) if command.reservation_id is not None else None
        ),
        "lease_version": command.lease_version,
        "reservation_lease": (
            request.reservation_lease.model_dump(mode="json")
            if request.reservation_lease is not None
            else None
        ),
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _durable_command_payload(
    command_type: RemoteCommandType,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Remove only per-delivery artifact capability fields from replay identity."""

    durable = dict(payload)
    if command_type is RemoteCommandType.RUN_WORKFLOW:
        descriptors = durable.get("artifact_transfers")
        if isinstance(descriptors, list):
            durable["artifact_transfers"] = [
                _durable_artifact_descriptor(item) for item in descriptors
            ]
    elif command_type is RemoteCommandType.FLASH:
        descriptor = durable.get("artifact")
        if isinstance(descriptor, Mapping):
            durable["artifact"] = _durable_artifact_descriptor(descriptor)
    return durable


def _durable_artifact_descriptor(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    return {
        str(key): item
        for key, item in value.items()
        if key not in {"transfer_id", "download_url", "transfer_token", "expires_at"}
    }


def _find_replay(
    connection: sqlite3.Connection,
    request: CommandRequestPayload,
    fingerprint: str,
) -> StoredCommandJournalEntry | None:
    command = request.command
    rows = connection.execute(
        "SELECT * FROM agent_command_journal WHERE command_id = ? OR idempotency_key = ?",
        (str(command.id), command.idempotency_key),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise RemoteCommandDuplicateError(
            "Command ID and idempotency key refer to different journal entries.",
            command_id=str(command.id),
            idempotency_key=command.idempotency_key,
        )
    existing = _record_from_row(rows[0])
    same_command_id = existing.entry.command_id == command.id
    same_key = existing.entry.idempotency_key == command.idempotency_key
    if existing.command_fingerprint != fingerprint or (same_command_id and not same_key):
        raise RemoteCommandDuplicateError(
            "Command identity is already bound to different content.",
            command_id=str(command.id),
            idempotency_key=command.idempotency_key,
        )
    # A new command ID with the same idempotency key and identical execution inputs replays the
    # original entry. This is the safe outcome: it cannot execute the physical action twice.
    return existing


def _require_row(connection: sqlite3.Connection, command_id: UUID) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM agent_command_journal WHERE command_id = ?",
        (str(command_id),),
    ).fetchone()
    if row is None:
        raise KeyError(f"Command journal entry does not exist: {command_id}")
    return cast(sqlite3.Row, row)


def _record_from_row(row: sqlite3.Row) -> StoredCommandJournalEntry:
    result_json = row["result_json"]
    result = json.loads(result_json) if result_json is not None else None
    entry = CommandJournalEntry(
        command_id=UUID(row["command_id"]),
        idempotency_key=row["idempotency_key"],
        command_type=row["command_type"],
        bench_id=row["bench_id"],
        status=row["status"],
        received_at=_parse_datetime(row["received_at"]),
        started_at=_parse_optional_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        result=result,
        error_code=row["error_code"],
        error_message=row["error_message"],
    )
    return StoredCommandJournalEntry(
        entry=entry,
        command_fingerprint=row["command_fingerprint"],
    )


def _dump_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Command journal timestamps must be timezone-aware")
    return value.astimezone(UTC)
