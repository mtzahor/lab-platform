from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core.artifacts import ArtifactService
from lab_platform.models import ArtifactOwnerType, ArtifactRecord, EventRecord
from lab_platform.persistence import SQLiteDatabase, SQLiteGenericArtifactRepository

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class EventSink:
    def __init__(self) -> None:
        self.items: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _record(
    *,
    expires_at: datetime | None,
    path: Path | None = None,
    artifact_id: UUID | None = None,
) -> ArtifactRecord:
    record_id = artifact_id or uuid4()
    return ArtifactRecord(
        id=record_id,
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=uuid4(),
        name="serial.log",
        artifact_type="serial-log",
        content_type="text/plain",
        path=str(path or Path("objects") / str(record_id) / "content"),
        size_bytes=6,
        sha256="a" * 64,
        created_at=NOW - timedelta(days=1),
        expires_at=expires_at,
    )


def test_default_retention_expires_content_metadata_and_emits_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "retention.db")
        repository = SQLiteGenericArtifactRepository(database)
        events = EventSink()
        storage_root = tmp_path / "artifacts"
        service = ArtifactService(
            repository,
            storage_root,
            maximum_size_bytes=100,
            retention_seconds=60,
            events=events,
            clock=lambda: NOW,
        )
        try:
            record = await service.store_bytes(
                b"serial",
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=uuid4(),
                name="serial.log",
                artifact_type="serial-log",
            )
            assert record.expires_at == NOW + timedelta(seconds=60)
            content_path = Path(record.path)
            assert content_path.read_bytes() == b"serial"

            assert await service.expire_due(now=NOW + timedelta(seconds=59), limit=10) == 0
            assert content_path.exists()
            assert await repository.get(record.id) == record

            assert await service.expire_due(now=NOW + timedelta(seconds=60), limit=10) == 1
            assert not content_path.exists()
            assert not content_path.parent.exists()
            assert await repository.get(record.id) is None
            expired_events = [item for item in events.items if item.type == "ARTIFACT_EXPIRED"]
            assert len(expired_events) == 1
            assert expired_events[0].payload["artifact_id"] == str(record.id)

            assert await service.expire_due(now=NOW + timedelta(days=1), limit=10) == 0
            assert len([item for item in events.items if item.type == "ARTIFACT_EXPIRED"]) == 1
        finally:
            database.close()

    asyncio.run(scenario())


def test_repository_lists_expired_records_in_stable_order_and_honors_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "listing.db")
        repository = SQLiteGenericArtifactRepository(database)
        first = _record(expires_at=NOW - timedelta(minutes=3))
        second = _record(expires_at=NOW - timedelta(minutes=2))
        third = _record(expires_at=NOW - timedelta(minutes=1))
        future = _record(expires_at=NOW + timedelta(minutes=1))
        permanent = _record(expires_at=None)
        try:
            for record in (third, permanent, first, future, second):
                await repository.save(record)

            jerusalem_cutoff = NOW.astimezone(timezone(timedelta(hours=3)))
            assert await repository.list_expired(
                expires_at_or_before=jerusalem_cutoff,
                limit=2,
            ) == [first, second]
            assert await repository.list_expired(
                expires_at_or_before=jerusalem_cutoff,
                limit=10,
            ) == [first, second, third]
            assert await repository.delete(first.id)
            assert not await repository.delete(first.id)

            with pytest.raises(ValueError, match="positive"):
                await repository.list_expired(expires_at_or_before=NOW, limit=0)
            with pytest.raises(ValueError, match="timezone-aware"):
                await repository.list_expired(
                    expires_at_or_before=NOW.replace(tzinfo=None),
                    limit=10,
                )
        finally:
            database.close()

    asyncio.run(scenario())


def test_expiry_never_unlinks_paths_outside_exact_managed_object_directory(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "path-safety.db")
        repository = SQLiteGenericArtifactRepository(database)
        events = EventSink()
        storage_root = tmp_path / "artifacts"
        service = ArtifactService(
            repository,
            storage_root,
            maximum_size_bytes=100,
            events=events,
            clock=lambda: NOW,
        )
        outside = tmp_path / "external-content"
        outside.write_bytes(b"external")
        wrong_directory = storage_root / "objects" / str(uuid4())
        wrong_directory.mkdir(parents=True)
        wrong_content = wrong_directory / "content"
        wrong_content.write_bytes(b"other artifact")
        symlink_record_id = uuid4()
        symlink_directory = storage_root / "objects" / str(symlink_record_id)
        symlink_directory.mkdir(parents=True)
        symlink_content = symlink_directory / "content"
        symlink_content.symlink_to(outside)
        external_record = _record(
            expires_at=NOW - timedelta(hours=1),
            path=outside,
        )
        mismatched_record = _record(
            expires_at=NOW - timedelta(hours=1),
            path=wrong_content,
        )
        missing_record_id = uuid4()
        missing_record = _record(
            artifact_id=missing_record_id,
            expires_at=NOW - timedelta(hours=1),
            path=storage_root / "objects" / str(missing_record_id) / "content",
        )
        symlink_record = _record(
            artifact_id=symlink_record_id,
            expires_at=NOW - timedelta(hours=1),
            path=symlink_content,
        )
        try:
            records = (
                external_record,
                mismatched_record,
                missing_record,
                symlink_record,
            )
            for record in records:
                await repository.save(record)

            assert await service.expire_due(now=NOW, limit=10) == 4
            assert outside.read_bytes() == b"external"
            assert wrong_content.read_bytes() == b"other artifact"
            assert not symlink_content.exists()
            assert not symlink_directory.exists()
            for record in records:
                assert await repository.get(record.id) is None
            assert len([item for item in events.items if item.type == "ARTIFACT_EXPIRED"]) == 4
        finally:
            database.close()

    asyncio.run(scenario())


def test_expire_due_validates_cutoff_and_batch_size(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "validation.db")
        repository = SQLiteGenericArtifactRepository(database)
        service = ArtifactService(repository, tmp_path / "artifacts", maximum_size_bytes=10)
        try:
            with pytest.raises(ValueError, match="positive"):
                await service.expire_due(now=NOW, limit=0)
            with pytest.raises(ValueError, match="timezone-aware"):
                await service.expire_due(now=NOW.replace(tzinfo=None), limit=10)
        finally:
            database.close()

    asyncio.run(scenario())
