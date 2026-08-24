from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from lab_platform.core.artifact_storage import ArtifactStorage
from lab_platform.core.retention import (
    RetentionCandidate,
    retention_class_for_artifact_type,
)
from lab_platform.models import LEGACY_ORGANISATION_ID, ArtifactOwnerType, ArtifactRecord
from lab_platform.persistence.database import SQLiteDatabase


class SQLiteGenericArtifactRepository:
    """Metadata repository for immutable, polymorphically owned artifacts."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        retention_storage: ArtifactStorage | None = None,
    ) -> None:
        self._database = database
        self._retention_storage = retention_storage

    async def save(
        self,
        record: ArtifactRecord,
        *,
        idempotency_key: str | None = None,
    ) -> ArtifactRecord:
        with self._database.transaction(immediate=True) as connection:
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE owner_id = ? AND idempotency_key = ? "
                    "AND organisation_id = ? AND retention_state = 'active'",
                    (str(record.owner_id), idempotency_key, str(record.organisation_id)),
                ).fetchone()
                if existing is not None:
                    return _artifact_from_row(existing)
            try:
                connection.execute(
                    "INSERT INTO artifacts "
                    "(id, organisation_id, owner_type, owner_id, name, artifact_type, "
                    "content_type, path, "
                    "size_bytes, sha256, created_at, expires_at, metadata_json, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*_artifact_values(record), idempotency_key),
                )
            except sqlite3.IntegrityError:
                # Concurrent retriers may both miss the first lookup. Resolve the
                # unique idempotency race to the resource that won the transaction.
                if idempotency_key is None:
                    raise
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE owner_id = ? AND idempotency_key = ? "
                    "AND organisation_id = ? AND retention_state = 'active'",
                    (str(record.owner_id), idempotency_key, str(record.organisation_id)),
                ).fetchone()
                if existing is None:
                    raise
                return _artifact_from_row(existing)
        return record

    async def get(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(artifact_id), str(organisation_id))
            if organisation_id is not None
            else (str(artifact_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM artifacts WHERE id = ? AND retention_state = 'active'{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _artifact_from_row(row) if row is not None else None

    async def get_by_idempotency_key(
        self,
        owner_id: UUID,
        key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord | None:
        scope_id = organisation_id or LEGACY_ORGANISATION_ID
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE owner_id = ? AND idempotency_key = ? "
                "AND organisation_id = ? AND retention_state = 'active'",
                (str(owner_id), key, str(scope_id)),
            ).fetchone()
        return _artifact_from_row(row) if row is not None else None

    async def list_for_owner(
        self,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> list[ArtifactRecord]:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (_enum_value(owner_type), str(owner_id), str(organisation_id))
            if organisation_id is not None
            else (_enum_value(owner_type), str(owner_id))
        )
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE owner_type = ? AND owner_id = ?"
                f"{scope} AND retention_state = 'active' "  # noqa: S608
                "ORDER BY created_at, id",
                values,
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    async def list_all(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 500,
    ) -> list[ArtifactRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        scope = (
            "WHERE organisation_id = ? AND retention_state = 'active' "
            if organisation_id is not None
            else "WHERE retention_state = 'active' "
        )
        values: tuple[object, ...] = (
            (str(organisation_id), limit) if organisation_id is not None else (limit,)
        )
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifacts {scope}"  # noqa: S608
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                values,
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    async def list_expired(
        self,
        *,
        expires_at_or_before: datetime,
        organisation_id: UUID | None = None,
        limit: int = 100,
    ) -> list[ArtifactRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if expires_at_or_before.tzinfo is None or expires_at_or_before.utcoffset() is None:
            raise ValueError("artifact expiry time must be timezone-aware")
        cutoff = expires_at_or_before.astimezone(UTC).isoformat()
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: list[object] = [cutoff]
        if organisation_id is not None:
            values.append(str(organisation_id))
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts "
                "WHERE expires_at IS NOT NULL AND expires_at <= ? "
                "AND retention_state = 'active' "
                f"{scope} "  # noqa: S608
                "ORDER BY expires_at, created_at, id LIMIT ?",
                values,
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    async def delete(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> bool:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(artifact_id), str(organisation_id))
            if organisation_id is not None
            else (str(artifact_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"DELETE FROM artifacts WHERE id = ? AND retention_state = 'active'{scope}",  # noqa: S608
                values,
            )
        return cursor.rowcount == 1

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> list[RetentionCandidate]:
        if self._retention_storage is None:
            raise RuntimeError("artifact retention storage is not configured")
        if limit <= 0:
            raise ValueError("retention claim limit must be positive")
        if claim_ttl <= timedelta(0):
            raise ValueError("retention claim TTL must be positive")
        observed_at = _aware_utc(now, field="retention claim time")
        reclaim_before = observed_at - claim_ttl
        candidates: list[RetentionCandidate] = []
        with self._database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE expires_at IS NOT NULL AND expires_at <= ? "
                "AND retention_deleted_at IS NULL AND (retention_state = 'active' OR "
                "(retention_state = 'pending_deletion' AND "
                "(retention_claimed_at IS NULL OR retention_claimed_at <= ?))) "
                "ORDER BY expires_at, created_at, id LIMIT ?",
                (
                    observed_at.isoformat(),
                    reclaim_before.isoformat(),
                    min(limit * 10, 100_000),
                ),
            ).fetchall()
            for row in rows:
                if len(candidates) >= limit:
                    break
                if _artifact_owner_is_active(connection, row):
                    continue
                artifact_id = UUID(str(row["id"]))
                expected_key = f"objects/{artifact_id}/content"
                referenced_key = self._retention_storage.key_from_reference(str(row["path"]))
                storage_key = referenced_key if referenced_key == expected_key else expected_key
                claim_token = uuid4()
                attempt_count = int(row["retention_attempt_count"]) + 1
                cursor = connection.execute(
                    "UPDATE artifacts SET retention_state = 'pending_deletion', "
                    "retention_claim_token = ?, retention_claimed_at = ?, "
                    "retention_attempt_count = ?, retention_last_error = NULL "
                    "WHERE id = ? AND retention_deleted_at IS NULL AND "
                    "(retention_state = 'active' OR (retention_state = 'pending_deletion' "
                    "AND (retention_claimed_at IS NULL OR retention_claimed_at <= ?)))",
                    (
                        str(claim_token),
                        observed_at.isoformat(),
                        attempt_count,
                        str(artifact_id),
                        reclaim_before.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                candidates.append(
                    RetentionCandidate(
                        artifact_id=artifact_id,
                        organisation_id=UUID(str(row["organisation_id"])),
                        storage_key=storage_key,
                        retention_class=retention_class_for_artifact_type(
                            str(row["artifact_type"])
                        ),
                        expires_at=_aware_utc(
                            datetime.fromisoformat(str(row["expires_at"])),
                            field="artifact expiry time",
                        ),
                        claim_token=claim_token,
                        size_bytes=int(row["size_bytes"]),
                        attempt_count=attempt_count,
                    )
                )
        return candidates

    async def mark_tombstoned(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        deleted_at: datetime,
    ) -> bool:
        observed_at = _aware_utc(deleted_at, field="retention deletion time")
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ? AND retention_state = "
                "'pending_deletion' AND retention_claim_token = ?",
                (str(artifact_id), str(claim_token)),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                "UPDATE artifacts SET retention_state = 'tombstoned', "
                "retention_deleted_at = ?, retention_claim_token = NULL, "
                "retention_claimed_at = NULL, retention_last_error = NULL "
                "WHERE id = ? AND retention_state = 'pending_deletion' "
                "AND retention_claim_token = ?",
                (observed_at.isoformat(), str(artifact_id), str(claim_token)),
            )
            if cursor.rowcount != 1:
                return False
            _insert_retention_audit_event(
                connection,
                row,
                occurred_at=observed_at,
                outcome="SUCCEEDED",
                reason=None,
            )
        return True

    async def mark_failed(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> bool:
        observed_at = _aware_utc(failed_at, field="retention failure time")
        bounded_error = error.strip()[:2_000] or "artifact deletion failed"
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ? AND retention_state = "
                "'pending_deletion' AND retention_claim_token = ?",
                (str(artifact_id), str(claim_token)),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                "UPDATE artifacts SET retention_claimed_at = ?, retention_last_error = ? "
                "WHERE id = ? AND retention_state = 'pending_deletion' "
                "AND retention_claim_token = ?",
                (
                    observed_at.isoformat(),
                    bounded_error,
                    str(artifact_id),
                    str(claim_token),
                ),
            )
            if cursor.rowcount != 1:
                return False
            _insert_retention_audit_event(
                connection,
                row,
                occurred_at=observed_at,
                outcome="FAILED",
                reason=bounded_error,
            )
        return True


# The shorter name is convenient for new composition roots while the explicit
# name avoids confusion with Phase 2's firmware-only SQLiteArtifactRepository.
SQLiteArtifactRecordRepository = SQLiteGenericArtifactRepository


def _artifact_owner_is_active(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    owner_type = str(row["owner_type"])
    owner_id = str(row["owner_id"])
    if owner_type == ArtifactOwnerType.CI_SESSION.value:
        active = connection.execute(
            "SELECT 1 FROM ci_sessions WHERE id = ? AND status IN "
            "('created', 'waiting_for_bench', 'reserved', 'running', "
            "'cancel_requested', 'cleanup_pending')",
            (owner_id,),
        ).fetchone()
        return active is not None
    if owner_type == ArtifactOwnerType.WORKFLOW_RUN.value:
        active = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE id = ? AND status IN "
            "('pending', 'running', 'cancel_requested')",
            (owner_id,),
        ).fetchone()
        return active is not None
    if owner_type == ArtifactOwnerType.WORKFLOW_STEP.value:
        active = connection.execute(
            "SELECT 1 FROM workflow_step_results AS step "
            "JOIN workflow_runs AS run ON run.id = step.workflow_run_id "
            "WHERE step.id = ? AND run.status IN "
            "('pending', 'running', 'cancel_requested')",
            (owner_id,),
        ).fetchone()
        return active is not None
    if owner_type == ArtifactOwnerType.OPERATION.value:
        distributed = connection.execute(
            "SELECT 1 FROM distributed_operations WHERE id = ? AND status IN "
            "('CREATED', 'DISPATCHED', 'ACCEPTED', 'RUNNING', 'UNKNOWN', 'RECONCILING')",
            (owner_id,),
        ).fetchone()
        if distributed is not None:
            return True
        local = connection.execute(
            "SELECT 1 FROM operations WHERE id = ? AND status IN "
            "('pending', 'running', 'cancel_requested')",
            (owner_id,),
        ).fetchone()
        return local is not None
    return False


def _insert_retention_audit_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    occurred_at: datetime,
    outcome: str,
    reason: str | None,
) -> None:
    metadata = {
        "artifact_type": str(row["artifact_type"]),
        "owner_type": str(row["owner_type"]),
        "owner_id": str(row["owner_id"]),
        "size_bytes": int(row["size_bytes"]),
        "retention_attempt_count": int(row["retention_attempt_count"]),
    }
    connection.execute(
        "INSERT INTO audit_events "
        "(id, organisation_id, timestamp, actor_type, actor_id, actor_display_name, "
        "action, resource_type, resource_id, outcome, request_id, source_ip, user_agent, "
        "reason, metadata_json) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, NULL, "
        "NULL, NULL, ?, ?)",
        (
            str(uuid4()),
            str(row["organisation_id"]),
            occurred_at.isoformat(),
            "RETENTION_DELETION",
            "ARTIFACT",
            str(row["id"]),
            outcome,
            reason,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        ),
    )


def _artifact_values(record: ArtifactRecord) -> tuple[object, ...]:
    return (
        str(record.id),
        str(record.organisation_id),
        _enum_value(record.owner_type),
        str(record.owner_id),
        record.name,
        record.artifact_type,
        record.content_type,
        str(record.path),
        record.size_bytes,
        record.sha256,
        record.created_at.isoformat(),
        _datetime_value(record.expires_at),
        json.dumps(record.metadata, sort_keys=True, separators=(",", ":")),
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        owner_type=ArtifactOwnerType(row["owner_type"]),
        owner_id=UUID(row["owner_id"]),
        name=row["name"],
        artifact_type=row["artifact_type"],
        content_type=row["content_type"],
        path=row["path"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        metadata=json.loads(row["metadata_json"]),
    )


def _enum_value(value: object) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate)


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
