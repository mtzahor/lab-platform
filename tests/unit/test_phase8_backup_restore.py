from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lab_platform.control_plane.backup import (
    BackupService,
    DatabaseBackupAdapter,
    LocalArtifactBackupAdapter,
)
from lab_platform.persistence import SQLiteDatabase


def _database(path: Path) -> str:
    database = SQLiteDatabase(path)
    database.initialize()
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO events (id, timestamp, type, source, payload) VALUES (?, ?, ?, ?, ?)",
                ("backup-event", "2026-08-24T00:00:00+00:00", "TEST", "test", "{}"),
            )
    finally:
        database.close()
    return f"sqlite:///{path}"


def _service(database_url: str, artifacts: Path) -> BackupService:
    return BackupService(
        DatabaseBackupAdapter(database_url),
        LocalArtifactBackupAdapter(artifacts),
        configuration={"profile": "test", "public_url": "https://lab.example.test"},
        clock=lambda: datetime(2026, 8, 24, tzinfo=UTC),
    )


def test_backup_create_verify_and_restore_round_trip(tmp_path: Path) -> None:
    source_artifacts = tmp_path / "source-artifacts"
    (source_artifacts / "objects" / "one").mkdir(parents=True)
    (source_artifacts / "objects" / "one" / "content").write_bytes(b"artifact-content")
    service = _service(_database(tmp_path / "source.db"), source_artifacts)

    backup = service.create(tmp_path / "backup.tar")
    verification = service.verify(backup, deep=True)

    assert verification.ok
    assert verification.manifest.database_schema == 12
    assert verification.manifest.artifacts_included is True
    assert verification.entries_verified == 2

    target_database = tmp_path / "restored.db"
    target_artifacts = tmp_path / "restored-artifacts"
    restored = _service(f"sqlite:///{target_database}", target_artifacts).restore(
        backup,
        confirmation="RESTORE",
    )

    assert restored.database_restored is True
    assert restored.artifacts_restored == 1
    assert (target_artifacts / "objects" / "one" / "content").read_bytes() == b"artifact-content"
    database = SQLiteDatabase(target_database)
    database.initialize()
    try:
        with database.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        database.close()


def test_restore_requires_confirmation_and_empty_targets(tmp_path: Path) -> None:
    service = _service(_database(tmp_path / "source.db"), tmp_path / "source-artifacts")
    backup = service.create(tmp_path / "backup.tar", include_artifacts=False)
    target_path = tmp_path / "target.db"
    target = _service(_database(target_path), tmp_path / "target-artifacts")

    with pytest.raises(PermissionError, match="RESTORE"):
        target.restore(backup)
    with pytest.raises(FileExistsError, match="not empty"):
        target.restore(backup, confirmation="RESTORE")

    result = target.restore(backup, overwrite=True, confirmation="RESTORE")
    assert result.database_restored is True


def test_verify_rejects_checksum_tampering(tmp_path: Path) -> None:
    source_artifacts = tmp_path / "artifacts"
    source_artifacts.mkdir()
    (source_artifacts / "log.txt").write_text("original")
    service = _service(_database(tmp_path / "source.db"), source_artifacts)
    backup = service.create(tmp_path / "backup.tar")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(backup, "r:") as archive:
        archive.extractall(extracted, filter="data")
    (extracted / "artifacts" / "log.txt").write_text("tampered")
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(tampered, "w") as archive:
        for path in sorted(item for item in extracted.rglob("*") if item.is_file()):
            archive.add(path, arcname=path.relative_to(extracted).as_posix())

    with pytest.raises(ValueError, match="size mismatch|checksum mismatch"):
        service.verify(tampered)


def test_verify_rejects_archive_traversal_before_extraction(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"escape"
        member = tarfile.TarInfo("../escape")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    service = _service(f"sqlite:///{tmp_path / 'unused.db'}", tmp_path / "artifacts")
    with pytest.raises(ValueError, match="unsafe"):
        service.verify(archive_path)
    assert not (tmp_path / "escape").exists()


def test_verify_rejects_undeclared_payload(tmp_path: Path) -> None:
    service = _service(_database(tmp_path / "source.db"), tmp_path / "artifacts")
    backup = service.create(tmp_path / "backup.tar", include_artifacts=False)
    extra = tmp_path / "extra.tar"
    with tarfile.open(backup, "r:") as source, tarfile.open(extra, "w") as target:
        for member in source.getmembers():
            stream = source.extractfile(member)
            assert stream is not None
            target.addfile(member, stream)
        payload = json.dumps({"secret": "not-indexed"}).encode()
        member = tarfile.TarInfo("undeclared.json")
        member.size = len(payload)
        target.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="payload index mismatch"):
        service.verify(extra)
