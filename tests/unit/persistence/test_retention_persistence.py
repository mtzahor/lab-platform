from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.core.artifact_storage import LocalArtifactStorage
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.retention import RetentionPolicy, RetentionWorker
from lab_platform.models import ArtifactOwnerType, ArtifactRecord
from lab_platform.persistence import SQLiteDatabase, SQLiteGenericArtifactRepository
from lab_platform.persistence.artifacts import _artifact_owner_is_active

NOW = datetime(2026, 1, 3, tzinfo=UTC)


def _record(
    artifact_id: UUID,
    *,
    owner_type: ArtifactOwnerType = ArtifactOwnerType.CI_SESSION,
    owner_id: UUID | None = None,
    path: str | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        owner_type=owner_type,
        owner_id=owner_id or uuid4(),
        name=f"{artifact_id}.log",
        artifact_type="serial_log",
        content_type="text/plain",
        path=path or f"objects/{artifact_id}/content",
        size_bytes=12,
        sha256="a" * 64,
        created_at=NOW - timedelta(days=3),
        expires_at=NOW - timedelta(days=1),
        metadata={"source": "coverage"},
    )


def test_persisted_retention_claim_tombstone_and_audit_are_atomic(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "control-plane.db")
        database.initialize()
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        repository = SQLiteGenericArtifactRepository(
            database,
            retention_storage=storage,
        )
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        service = ArtifactService(
            repository,
            maximum_size_bytes=1024,
            storage=storage,
            retention_policy=RetentionPolicy(default_days=1),
            clock=lambda: created_at,
        )
        record = await service.store_bytes(
            b"retained artifact",
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="serial.log",
            artifact_type="serial_log",
        )

        result = await RetentionWorker(repository, storage).run_once(
            now=datetime(2026, 1, 3, tzinfo=UTC),
        )

        assert result.claimed == 1
        assert result.tombstoned == 1
        assert result.successful
        assert not await storage.exists(f"objects/{record.id}/content")
        assert await repository.get(record.id) is None
        with database.transaction() as connection:
            retained = connection.execute(
                "SELECT retention_state, retention_deleted_at, retention_attempt_count "
                "FROM artifacts WHERE id = ?",
                (str(record.id),),
            ).fetchone()
            audit = connection.execute(
                "SELECT action, resource_type, resource_id, outcome FROM audit_events "
                "WHERE resource_id = ?",
                (str(record.id),),
            ).fetchone()
        assert retained is not None
        assert tuple(retained) == ("tombstoned", "2026-01-03T00:00:00+00:00", 1)
        assert audit is not None
        assert tuple(audit) == (
            "RETENTION_DELETION",
            "ARTIFACT",
            str(record.id),
            "SUCCEEDED",
        )
        database.close()

    asyncio.run(scenario())


def test_persisted_retention_excludes_active_artifact_owners(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "control-plane.db")
        database.initialize()
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        repository = SQLiteGenericArtifactRepository(
            database,
            retention_storage=storage,
        )
        owner_id = uuid4()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO operations "
                "(id, bench_id, type, status, requested_by, created_at) "
                "VALUES (?, 'bench-1', 'probe', 'pending', 'retention-test', ?)",
                (str(owner_id), datetime(2026, 1, 1, tzinfo=UTC).isoformat()),
            )
        service = ArtifactService(
            repository,
            maximum_size_bytes=1024,
            storage=storage,
            retention_policy=RetentionPolicy(default_days=1),
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        record = await service.store_bytes(
            b"still in use",
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=owner_id,
            name="operation.log",
            artifact_type="workflow_log",
        )
        worker = RetentionWorker(repository, storage)

        active = await worker.run_once(now=datetime(2026, 1, 3, tzinfo=UTC))
        assert active.claimed == 0
        assert await repository.get(record.id) == record

        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE operations SET status = 'succeeded', completed_at = ? WHERE id = ?",
                (datetime(2026, 1, 3, tzinfo=UTC).isoformat(), str(owner_id)),
            )
        terminal = await worker.run_once(now=datetime(2026, 1, 3, tzinfo=UTC))
        assert terminal.tombstoned == 1
        assert await repository.get(record.id) is None
        database.close()

    asyncio.run(scenario())


def test_retention_repository_validates_claims_and_reclaims_failed_attempts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "retry.db")
        database.initialize()
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        unconfigured = SQLiteGenericArtifactRepository(database)
        repository = SQLiteGenericArtifactRepository(database, retention_storage=storage)
        try:
            with pytest.raises(RuntimeError, match="not configured"):
                await unconfigured.claim_due(
                    now=NOW,
                    limit=1,
                    claim_ttl=timedelta(minutes=5),
                )
            with pytest.raises(ValueError, match="limit"):
                await repository.claim_due(
                    now=NOW,
                    limit=0,
                    claim_ttl=timedelta(minutes=5),
                )
            with pytest.raises(ValueError, match="TTL"):
                await repository.claim_due(now=NOW, limit=1, claim_ttl=timedelta(0))
            with pytest.raises(ValueError, match="timezone-aware"):
                await repository.claim_due(
                    now=NOW.replace(tzinfo=None),
                    limit=1,
                    claim_ttl=timedelta(minutes=5),
                )
            with pytest.raises(ValueError, match="timezone-aware"):
                await repository.mark_tombstoned(
                    uuid4(),
                    claim_token=uuid4(),
                    deleted_at=NOW.replace(tzinfo=None),
                )
            with pytest.raises(ValueError, match="timezone-aware"):
                await repository.mark_failed(
                    uuid4(),
                    claim_token=uuid4(),
                    failed_at=NOW.replace(tzinfo=None),
                    error="failed",
                )

            first = _record(
                UUID(int=1),
                path=str(tmp_path / "untrusted-reference"),
            ).model_copy(update={"expires_at": NOW - timedelta(days=2)})
            second = _record(UUID(int=2))
            await repository.save(first)
            await repository.save(second)
            claimed = await repository.claim_due(
                now=NOW,
                limit=1,
                claim_ttl=timedelta(minutes=5),
            )
            assert len(claimed) == 1
            candidate = claimed[0]
            assert candidate.storage_key == f"objects/{candidate.artifact_id}/content"
            assert candidate.attempt_count == 1
            assert candidate.artifact_id == first.id
            assert await repository.delete(second.id)

            wrong_token = uuid4()
            assert not await repository.mark_tombstoned(
                candidate.artifact_id,
                claim_token=wrong_token,
                deleted_at=NOW,
            )
            assert not await repository.mark_failed(
                candidate.artifact_id,
                claim_token=wrong_token,
                failed_at=NOW,
                error="ignored",
            )
            assert await repository.mark_failed(
                candidate.artifact_id,
                claim_token=candidate.claim_token,
                failed_at=NOW,
                error="   ",
            )
            assert (
                await repository.claim_due(
                    now=NOW + timedelta(minutes=4),
                    limit=1,
                    claim_ttl=timedelta(minutes=5),
                )
                == []
            )

            retried = await repository.claim_due(
                now=NOW + timedelta(minutes=5),
                limit=1,
                claim_ttl=timedelta(minutes=5),
            )
            assert len(retried) == 1
            assert retried[0].artifact_id == candidate.artifact_id
            assert retried[0].attempt_count == 2
            long_error = "x" * 2_500
            assert await repository.mark_failed(
                retried[0].artifact_id,
                claim_token=retried[0].claim_token,
                failed_at=NOW + timedelta(minutes=5),
                error=long_error,
            )

            with database.transaction() as connection:
                row = connection.execute(
                    "SELECT retention_state, retention_attempt_count, retention_last_error "
                    "FROM artifacts WHERE id = ?",
                    (str(candidate.artifact_id),),
                ).fetchone()
                audits = connection.execute(
                    "SELECT outcome, reason, metadata_json FROM audit_events "
                    "WHERE resource_id = ? ORDER BY timestamp",
                    (str(candidate.artifact_id),),
                ).fetchall()
            assert row is not None
            assert tuple(row) == ("pending_deletion", 2, "x" * 2_000)
            assert [audit["outcome"] for audit in audits] == ["FAILED", "FAILED"]
            assert audits[0]["reason"] == "artifact deletion failed"
            assert json.loads(audits[1]["metadata_json"])["retention_attempt_count"] == 2
        finally:
            database.close()

    asyncio.run(scenario())


def test_artifact_repository_validation_scoping_and_duplicate_integrity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "repository.db")
        database.initialize()
        repository = SQLiteGenericArtifactRepository(database)
        record = _record(uuid4())
        try:
            await repository.save(record)
            with pytest.raises(sqlite3.IntegrityError):
                await repository.save(record)
            with pytest.raises(ValueError, match="positive"):
                await repository.list_all(limit=0)

            assert await repository.list_all(limit=1) == [record]
            assert await repository.list_all(
                organisation_id=record.organisation_id,
                limit=1,
            ) == [record]
            assert await repository.list_expired(
                expires_at_or_before=NOW,
                organisation_id=record.organisation_id,
            ) == [record]
            assert not await repository.delete(record.id, organisation_id=uuid4())
            assert await repository.delete(
                record.id,
                organisation_id=record.organisation_id,
            )
        finally:
            database.close()

    asyncio.run(scenario())


class _FetchOne:
    def __init__(self, row: MappingRow | None) -> None:
        self._row = row

    def fetchone(self) -> MappingRow | None:
        return self._row


MappingRow = dict[str, Any]


class _RaceConnection:
    def __init__(self, winner: MappingRow | None) -> None:
        self.winner = winner
        self.select_count = 0

    def execute(self, query: str, _values: object) -> _FetchOne:
        if query.startswith("SELECT"):
            self.select_count += 1
            return _FetchOne(None if self.select_count == 1 else self.winner)
        raise sqlite3.IntegrityError("simulated concurrent insert")


class _Transaction(AbstractContextManager[_RaceConnection]):
    def __init__(self, connection: _RaceConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _RaceConnection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _RaceDatabase:
    def __init__(self, winner: MappingRow | None) -> None:
        self.connection = _RaceConnection(winner)

    def transaction(self, *, immediate: bool = False) -> _Transaction:
        assert immediate is True
        return _Transaction(self.connection)


def _row_for(record: ArtifactRecord) -> MappingRow:
    return {
        "id": str(record.id),
        "organisation_id": str(record.organisation_id),
        "owner_type": record.owner_type.value,
        "owner_id": str(record.owner_id),
        "name": record.name,
        "artifact_type": record.artifact_type,
        "content_type": record.content_type,
        "path": str(record.path),
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "metadata_json": json.dumps(record.metadata),
    }


def test_artifact_repository_resolves_idempotency_insert_races() -> None:
    async def scenario() -> None:
        record = _record(uuid4())
        winner_database = _RaceDatabase(_row_for(record))
        winner_repository = SQLiteGenericArtifactRepository(cast(SQLiteDatabase, winner_database))
        assert await winner_repository.save(record, idempotency_key="retry") == record
        assert winner_database.connection.select_count == 2

        missing_repository = SQLiteGenericArtifactRepository(
            cast(SQLiteDatabase, _RaceDatabase(None))
        )
        with pytest.raises(sqlite3.IntegrityError, match="concurrent"):
            await missing_repository.save(record, idempotency_key="retry")

        duplicate_repository = SQLiteGenericArtifactRepository(
            cast(SQLiteDatabase, _RaceDatabase(None))
        )
        with pytest.raises(sqlite3.IntegrityError, match="concurrent"):
            await duplicate_repository.save(record)

    asyncio.run(scenario())


def test_active_owner_detection_covers_all_retention_owner_types() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE ci_sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE workflow_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE workflow_step_results (
            id TEXT PRIMARY KEY,
            workflow_run_id TEXT NOT NULL
        );
        CREATE TABLE distributed_operations (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE operations (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        """
    )

    def owner_row(owner_type: ArtifactOwnerType | str, owner_id: str) -> sqlite3.Row:
        owner_type_value = getattr(owner_type, "value", owner_type)
        row = connection.execute(
            "SELECT ? AS owner_type, ? AS owner_id",
            (owner_type_value, owner_id),
        ).fetchone()
        assert row is not None
        return cast(sqlite3.Row, row)

    try:
        connection.execute("INSERT INTO ci_sessions VALUES ('ci-active', 'running')")
        assert _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.CI_SESSION, "ci-active"),
        )
        assert not _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.CI_SESSION, "ci-missing"),
        )

        connection.execute("INSERT INTO workflow_runs VALUES ('run-active', 'pending')")
        connection.execute("INSERT INTO workflow_runs VALUES ('run-done', 'succeeded')")
        connection.execute("INSERT INTO workflow_step_results VALUES ('step-active', 'run-active')")
        assert _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.WORKFLOW_RUN, "run-active"),
        )
        assert not _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.WORKFLOW_RUN, "run-done"),
        )
        assert _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.WORKFLOW_STEP, "step-active"),
        )
        assert not _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.WORKFLOW_STEP, "step-missing"),
        )

        connection.execute(
            "INSERT INTO distributed_operations VALUES ('distributed-active', 'RECONCILING')"
        )
        connection.execute("INSERT INTO operations VALUES ('local-active', 'running')")
        assert _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.OPERATION, "distributed-active"),
        )
        assert _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.OPERATION, "local-active"),
        )
        assert not _artifact_owner_is_active(
            connection,
            owner_row(ArtifactOwnerType.OPERATION, "operation-missing"),
        )

        assert not _artifact_owner_is_active(
            connection,
            owner_row("diagnostic_bundle", "other"),
        )
    finally:
        connection.close()
