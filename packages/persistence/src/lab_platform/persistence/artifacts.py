from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import UUID

from lab_platform.models import LEGACY_ORGANISATION_ID, ArtifactOwnerType, ArtifactRecord
from lab_platform.persistence.database import SQLiteDatabase


class SQLiteGenericArtifactRepository:
    """Metadata repository for immutable, polymorphically owned artifacts."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

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
                    "AND organisation_id = ?",
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
                    "AND organisation_id = ?",
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
                f"SELECT * FROM artifacts WHERE id = ?{scope}",  # noqa: S608
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
                "AND organisation_id = ?",
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
                f"{scope} "  # noqa: S608
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
        scope = "WHERE organisation_id = ? " if organisation_id is not None else ""
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
                f"DELETE FROM artifacts WHERE id = ?{scope}",  # noqa: S608
                values,
            )
        return cursor.rowcount == 1


# The shorter name is convenient for new composition roots while the explicit
# name avoids confusion with Phase 2's firmware-only SQLiteArtifactRepository.
SQLiteArtifactRecordRepository = SQLiteGenericArtifactRepository


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
