from __future__ import annotations

import hashlib
import importlib
import io
import os
import sqlite3
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from lab_platform.control_plane import backup as backup_module
from lab_platform.control_plane.backup import (
    BACKUP_FORMAT_VERSION,
    BackupFileEntry,
    BackupManifest,
    BackupService,
    DatabaseBackupAdapter,
    LocalArtifactBackupAdapter,
    S3ArtifactBackupAdapter,
)
from lab_platform.core import VERSION
from lab_platform.persistence import SCHEMA_VERSION, SQLiteDatabase

_SHA256 = hashlib.sha256(b"database").hexdigest()
_CREATED_AT = datetime(2026, 8, 24, tzinfo=UTC)


def _manifest(
    *,
    format_version: int = BACKUP_FORMAT_VERSION,
    application_version: str = VERSION,
    database_schema: int = SCHEMA_VERSION,
    database_backend: str = "sqlite",
    artifacts_included: bool = False,
    artifacts: tuple[BackupFileEntry, ...] = (),
) -> BackupManifest:
    return BackupManifest(
        format_version=format_version,
        application_version=application_version,
        database_schema=database_schema,
        created_at=_CREATED_AT,
        database_backend=database_backend,
        database_dump=BackupFileEntry("database.dump", 8, _SHA256),
        artifacts_included=artifacts_included,
        artifacts=artifacts,
        configuration={"profile": "test"},
    )


def _database(path: Path) -> str:
    database = SQLiteDatabase(path)
    database.initialize()
    database.close()
    return f"sqlite:///{path}"


def _service(database_url: str, artifacts: Path) -> BackupService:
    return BackupService(
        DatabaseBackupAdapter(database_url),
        LocalArtifactBackupAdapter(artifacts),
        clock=lambda: _CREATED_AT,
    )


def test_backup_value_objects_reject_inconsistent_or_unsafe_values() -> None:
    artifact = BackupFileEntry("artifacts/object", 1, "0" * 64)

    with pytest.raises(ValueError, match="negative"):
        BackupFileEntry("database.dump", -1, _SHA256)
    with pytest.raises(ValueError, match="SHA-256"):
        BackupFileEntry("database.dump", 1, "A" * 64)
    with pytest.raises(ValueError, match="invalid"):
        BackupFileEntry("bad\\path", 1, "0" * 64)
    with pytest.raises(ValueError, match="format"):
        _manifest(format_version=2)
    with pytest.raises(ValueError, match="schema"):
        _manifest(database_schema=-1)
    with pytest.raises(ValueError, match="backend"):
        _manifest(database_backend="mysql")
    with pytest.raises(ValueError, match="artifacts_included"):
        _manifest(artifacts_included=False, artifacts=(artifact,))
    with pytest.raises(ValueError, match="duplicate"):
        BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            application_version=VERSION,
            database_schema=SCHEMA_VERSION,
            created_at=_CREATED_AT,
            database_backend="sqlite",
            database_dump=BackupFileEntry("artifacts/object", 1, "0" * 64),
            artifacts_included=True,
            artifacts=(artifact,),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            application_version=VERSION,
            database_schema=SCHEMA_VERSION,
            created_at=datetime(2026, 8, 24),
            database_backend="sqlite",
            database_dump=BackupFileEntry("database.dump", 8, _SHA256),
            artifacts_included=False,
        )


def test_manifest_parser_rejects_malformed_and_unexpected_fields() -> None:
    value = _manifest().as_dict()

    with pytest.raises(ValueError, match="JSON object"):
        BackupManifest.from_dict([])
    with pytest.raises(ValueError, match="unknown fields"):
        BackupManifest.from_dict({**value, "secret": "unexpected"})
    with pytest.raises(ValueError, match="artifacts must be an array"):
        BackupManifest.from_dict({**value, "artifacts": {}})
    with pytest.raises(ValueError, match="configuration references"):
        BackupManifest.from_dict({**value, "configuration": {"profile": 7}})
    with pytest.raises(ValueError, match="missing required field: created_at"):
        BackupManifest.from_dict({key: item for key, item in value.items() if key != "created_at"})
    with pytest.raises(ValueError, match="file entry must be an object"):
        BackupManifest.from_dict({**value, "database_dump": "dump"})
    with pytest.raises(ValueError, match="must contain"):
        BackupManifest.from_dict({**value, "database_dump": {"path": "database.dump"}})
    with pytest.raises(ValueError, match="format_version must be an integer"):
        BackupManifest.from_dict({**value, "format_version": True})
    with pytest.raises(ValueError, match="artifacts_included must be a boolean"):
        BackupManifest.from_dict({**value, "artifacts_included": 1})
    with pytest.raises(ValueError, match="application_version must be a non-empty string"):
        BackupManifest.from_dict({**value, "application_version": ""})


def test_verification_dictionary_contains_manifest_and_counts(tmp_path: Path) -> None:
    service = _service(_database(tmp_path / "source.db"), tmp_path / "artifacts")
    archive = service.create(tmp_path / "backup.tar", include_artifacts=False)

    result = service.verify(archive, deep=False).as_dict()

    assert result["ok"] is True
    assert result["path"] == str(archive)
    assert result["entries_verified"] == 1
    assert result["manifest"] == service.verify(archive).manifest.as_dict()


def test_postgresql_adapter_builds_safe_commands_and_normalizes_driver_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Mapping[str, str]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    adapter = DatabaseBackupAdapter("postgresql+psycopg://user:pass@db/lab", run=run)

    adapter.create_dump(tmp_path / "backup.dump")
    adapter.verify_dump(tmp_path / "backup.dump")
    adapter.restore_dump(tmp_path / "backup.dump", overwrite=False)
    adapter.restore_dump(tmp_path / "backup.dump", overwrite=True)

    assert adapter.backend == "postgresql"
    assert [command[0] for command, _ in calls] == [
        "pg_dump",
        "pg_restore",
        "pg_restore",
        "pg_restore",
    ]
    assert "--clean" not in calls[2][0]
    assert calls[3][0][-3:-1] == ["--clean", "--if-exists"]
    assert calls[0][1]["PGDATABASE"] == "postgresql://user:pass@db/lab"
    assert calls[0][1]["PGCONNECT_TIMEOUT"] == "10"


def test_postgresql_adapter_reports_missing_and_failed_client_tools(tmp_path: Path) -> None:
    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    adapter = DatabaseBackupAdapter("postgresql://db/lab", run=missing)
    with pytest.raises(RuntimeError, match="pg_dump is required"):
        adapter.create_dump(tmp_path / "backup.dump")

    def failed(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="credentials rejected")

    adapter = DatabaseBackupAdapter("postgresql://db/lab", run=failed)
    with pytest.raises(RuntimeError, match="pg_restore failed: credentials rejected"):
        adapter.verify_dump(tmp_path / "backup.dump")

    with pytest.raises(ValueError, match="SQLite or PostgreSQL"):
        DatabaseBackupAdapter("mysql://db/lab")


@pytest.mark.parametrize(("row", "expected"), [((0,), True), ((2,), False), (None, False)])
def test_postgresql_adapter_checks_whether_restore_target_is_empty(
    row: tuple[int] | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def fetchone(self) -> tuple[int] | None:
            return row

    class Connection:
        closed = False

        def execute(self, query: str) -> Cursor:
            assert "information_schema.tables" in query
            return Cursor()

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    class PsycopgModule(ModuleType):
        def connect(self, url: str) -> Connection:
            assert url == "postgresql://db/lab"
            return connection

    psycopg = PsycopgModule("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)

    assert DatabaseBackupAdapter("postgresql://db/lab").target_is_empty() is expected
    assert connection.closed is True


def test_sqlite_adapter_handles_missing_empty_and_failed_integrity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source.db"
    adapter = DatabaseBackupAdapter(f"sqlite:///{database_path}")
    assert adapter.target_is_empty() is True
    with pytest.raises(FileNotFoundError, match="does not exist"):
        adapter.create_dump(tmp_path / "dump.db")

    database_path.touch()
    assert adapter.target_is_empty() is True

    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("corrupt",)

    class Connection:
        def execute(self, statement: str) -> Cursor:
            assert statement == "PRAGMA quick_check"
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(ValueError, match="quick_check"):
        adapter.verify_dump(tmp_path / "dump.db")


def test_local_artifact_snapshot_skips_incoming_and_rejects_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / ".incoming").mkdir(parents=True)
    (root / ".incoming" / "partial").write_bytes(b"partial")
    (root / "content").write_bytes(b"content")
    destination = tmp_path / "snapshot"

    entries = LocalArtifactBackupAdapter(root).snapshot(destination)

    assert [entry.path for entry in entries] == ["artifacts/content"]
    assert not (destination / ".incoming" / "partial").exists()
    (root / "link").symlink_to(root / "content")
    with pytest.raises(ValueError, match="symbolic links"):
        LocalArtifactBackupAdapter(root).snapshot(tmp_path / "second-snapshot")
    assert LocalArtifactBackupAdapter(tmp_path / "missing").snapshot(destination) == ()


def test_local_artifact_snapshot_detects_concurrent_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    source = (root / "content").resolve()
    source.write_bytes(b"content")
    original_open = Path.open

    def changing_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = original_open(path, mode, *args, **kwargs)
        if path == source and mode == "rb":
            metadata = os.stat(source)
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1))
        return stream

    monkeypatch.setattr(Path, "open", changing_open)
    with pytest.raises(RuntimeError, match="changed while backup"):
        LocalArtifactBackupAdapter(root).snapshot(tmp_path / "snapshot")


def test_local_artifact_restore_refuses_nonempty_target_and_staging_collision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "content").write_bytes(b"new")
    entry = BackupFileEntry("artifacts/content", 3, hashlib.sha256(b"new").hexdigest())
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "old").write_bytes(b"old")
    adapter = LocalArtifactBackupAdapter(root)

    with pytest.raises(FileExistsError, match="explicit overwrite"):
        adapter.restore(source, [entry], overwrite=False)

    previous = root.with_name(f".{root.name}.previous-{os.getpid()}")
    previous.mkdir()
    with pytest.raises(FileExistsError, match="staging path"):
        adapter.restore(source, [entry], overwrite=True)
    assert (root / "old").read_bytes() == b"old"


def test_local_artifact_restore_rolls_back_after_replacement_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "content").write_bytes(b"new")
    entry = BackupFileEntry("artifacts/content", 3, hashlib.sha256(b"new").hexdigest())
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "old").write_bytes(b"old")
    adapter = LocalArtifactBackupAdapter(root)
    original_replace = os.replace
    replacement_failed = False

    def fail_stage_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal replacement_failed
        if destination_path == root and ".restore-" in source_path.name:
            replacement_failed = True
            raise OSError("simulated replacement failure")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_stage_replace)
    with pytest.raises(OSError, match="simulated"):
        adapter.restore(source, [entry], overwrite=True)

    assert replacement_failed is True
    assert (root / "old").read_bytes() == b"old"


class _ScriptedBody:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.closed = False

    def read(self, size: int) -> object:
        del size
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


class _ScriptedS3Client:
    def __init__(self) -> None:
        self.list_responses: list[Mapping[str, Any]] = [{"Contents": [], "IsTruncated": False}]
        self.list_calls = 0
        self.get_response: Mapping[str, Any] = {}
        self.head_response: Mapping[str, Any] = {}
        self.deleted: list[str] = []
        self.upload_error: BaseException | None = None

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, Any]:
        del kwargs
        index = min(self.list_calls, len(self.list_responses) - 1)
        self.list_calls += 1
        return self.list_responses[index]

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        del kwargs
        return self.get_response

    def head_object(self, **kwargs: object) -> Mapping[str, Any]:
        del kwargs
        return self.head_response

    def upload_fileobj(
        self,
        file_object: Any,
        bucket: str,
        key: str,
        ExtraArgs: Mapping[str, object] | None = None,
    ) -> None:
        del file_object, bucket, key, ExtraArgs
        if self.upload_error is not None:
            raise self.upload_error

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]:
        self.deleted.append(str(kwargs["Key"]))
        return {}


def _s3(client: _ScriptedS3Client) -> S3ArtifactBackupAdapter:
    return S3ArtifactBackupAdapter(bucket="backups", prefix="tenant/artifacts", client=client)


def test_s3_adapter_rejects_conflicting_client_configuration() -> None:
    client = _ScriptedS3Client()
    with pytest.raises(ValueError, match="bucket"):
        S3ArtifactBackupAdapter(bucket=" ", prefix="tenant", client=client)
    with pytest.raises(ValueError, match="either an S3 client"):
        S3ArtifactBackupAdapter(
            bucket="backups",
            prefix="tenant",
            client=client,
            client_factory=lambda: client,
        )


def test_s3_snapshot_handles_folder_markers_and_unreadable_streams(tmp_path: Path) -> None:
    client = _ScriptedS3Client()
    client.list_responses = [
        {
            "Contents": [{"Key": "tenant/artifacts/folder/", "Size": 0}],
            "IsTruncated": False,
        }
    ]
    assert _s3(client).snapshot(tmp_path / "markers") == ()

    client.list_calls = 0
    client.list_responses = [
        {
            "Contents": [{"Key": "tenant/artifacts/object", "Size": 1}],
            "IsTruncated": False,
        }
    ]
    client.get_response = {"Body": None}
    with pytest.raises(RuntimeError, match="not readable"):
        _s3(client).snapshot(tmp_path / "missing-body")

    body = _ScriptedBody(["not bytes"])
    client.list_calls = 0
    client.get_response = {"Body": body}
    with pytest.raises(RuntimeError, match="non-byte"):
        _s3(client).snapshot(tmp_path / "non-byte")
    assert body.closed is True


@pytest.mark.parametrize(
    "response",
    [
        {"Contents": "bad", "IsTruncated": False},
        {"Contents": ["bad"], "IsTruncated": False},
        {
            "Contents": [
                {"Key": "tenant/artifacts/object", "Size": 1},
                {"Key": "tenant/artifacts/object", "Size": 1},
            ],
            "IsTruncated": False,
        },
        {
            "Contents": [{"Key": "another-prefix/folder/", "Size": 0}],
            "IsTruncated": False,
        },
    ],
)
def test_s3_listing_rejects_malformed_or_out_of_scope_results(
    response: Mapping[str, Any],
) -> None:
    client = _ScriptedS3Client()
    client.list_responses = [response]
    with pytest.raises((RuntimeError, ValueError), match="invalid|duplicate|outside"):
        tuple(_s3(client)._list_objects())


def test_s3_listing_rejects_stale_pagination_tokens() -> None:
    client = _ScriptedS3Client()
    client.list_responses = [
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
    ]

    with pytest.raises(RuntimeError, match="fresh continuation token"):
        tuple(_s3(client)._list_objects())


@pytest.mark.parametrize(
    ("listed", "response", "head", "message"),
    [
        ({"Size": 2}, {}, {"ContentLength": 1}, "changed"),
        ({"Size": 1}, {"ContentLength": 2}, {"ContentLength": 1}, "response size"),
        ({"Size": 1}, {"ContentLength": 1}, {"ContentLength": 2}, "changed"),
        (
            {"Size": 1, "ETag": '"one"'},
            {"ContentLength": 1, "ETag": '"two"'},
            {"ContentLength": 1, "ETag": '"one"'},
            "changed",
        ),
        (
            {"Size": 1},
            {"ContentLength": 1},
            {"ContentLength": 1, "Metadata": {"sha256": 7}},
            "invalid checksum metadata",
        ),
    ],
)
def test_s3_download_verification_detects_remote_mutation(
    listed: Mapping[str, Any],
    response: Mapping[str, Any],
    head: Mapping[str, Any],
    message: str,
) -> None:
    client = _ScriptedS3Client()
    client.head_response = head

    with pytest.raises(RuntimeError, match=message):
        _s3(client)._verify_download(
            "tenant/artifacts/object",
            listed=listed,
            response=response,
            size=1,
            sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("head", "message"),
    [
        ({"ContentLength": 2, "Metadata": {"sha256": "0" * 64}}, "size mismatch"),
        ({"ContentLength": 1, "Metadata": {}}, "checksum metadata mismatch"),
    ],
)
def test_s3_upload_verification_rejects_size_or_checksum(
    head: Mapping[str, Any],
    message: str,
) -> None:
    client = _ScriptedS3Client()
    client.head_response = head
    with pytest.raises(RuntimeError, match=message):
        _s3(client)._verify_uploaded(
            "tenant/artifacts/object",
            BackupFileEntry("artifacts/object", 1, "0" * 64),
        )


def test_s3_restore_validates_duplicates_and_cleans_failed_upload(tmp_path: Path) -> None:
    client = _ScriptedS3Client()
    adapter = _s3(client)
    assert adapter.restore(tmp_path, (), overwrite=False) == 0
    source = tmp_path / "source"
    source.mkdir()
    (source / "object").write_bytes(b"x")
    entry = BackupFileEntry("artifacts/object", 1, hashlib.sha256(b"x").hexdigest())

    with pytest.raises(ValueError, match="duplicate"):
        adapter.restore(source, [entry, entry], overwrite=False)

    client.upload_error = OSError("upload failed after partial write")
    with pytest.raises(OSError, match="upload failed"):
        adapter.restore(source, [entry], overwrite=False)
    assert client.deleted == ["tenant/artifacts/object"]


def test_s3_lazy_sdk_import_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ScriptedS3Client()
    calls: list[tuple[str, Mapping[str, object]]] = []

    class Boto3Module(ModuleType):
        def client(self, name: str, **kwargs: object) -> _ScriptedS3Client:
            calls.append((name, kwargs))
            return client

    boto3 = Boto3Module("boto3")
    monkeypatch.setattr(importlib, "import_module", lambda name: boto3)
    adapter = S3ArtifactBackupAdapter(
        bucket="backups",
        prefix="tenant",
        endpoint_url="https://s3.example.test",
        region_name="eu-west-1",
    )
    assert adapter.target_is_empty() is True
    assert calls == [
        (
            "s3",
            {"endpoint_url": "https://s3.example.test", "region_name": "eu-west-1"},
        )
    ]

    def unavailable(name: str) -> ModuleType:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    with pytest.raises(RuntimeError, match="boto3"):
        S3ArtifactBackupAdapter(bucket="backups", prefix="tenant").target_is_empty()


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.events.append((event_type, payload))


class _FailingDatabaseAdapter:
    backend = "sqlite"

    def schema_version(self) -> int:
        return SCHEMA_VERSION

    def create_dump(self, destination: Path) -> None:
        destination.write_bytes(b"partial")
        raise RuntimeError("dump failed")

    def verify_dump(self, source: Path) -> None:
        del source

    def target_is_empty(self) -> bool:
        return True

    def restore_dump(self, source: Path, *, overwrite: bool) -> None:
        del source, overwrite


def test_backup_failure_removes_partial_output_and_emits_failure(tmp_path: Path) -> None:
    events = _EventRecorder()
    service = BackupService(
        _FailingDatabaseAdapter(),
        LocalArtifactBackupAdapter(tmp_path / "artifacts"),
        events=events,
        clock=lambda: _CREATED_AT,
    )
    output = tmp_path / "backup.tar"

    with pytest.raises(RuntimeError, match="dump failed"):
        service.create(output)

    assert not output.exists()
    assert [event for event, _ in events.events] == ["BACKUP_STARTED", "BACKUP_FAILED"]


def test_backup_refuses_existing_destination_and_supports_zstandard_directory_output(
    tmp_path: Path,
) -> None:
    service = _service(_database(tmp_path / "source.db"), tmp_path / "artifacts")
    existing = tmp_path / "existing.tar"
    existing.touch()
    with pytest.raises(FileExistsError, match="already exists"):
        service.create(existing)

    archive = service.create(tmp_path / "snapshots", include_artifacts=False)
    assert archive.name == "lab-platform-backup-2026-08-24T000000Z.tar.zst"
    assert service.verify(archive).ok is True


def test_restore_rejects_backend_mismatch_and_nonempty_artifact_target(tmp_path: Path) -> None:
    source_artifacts = tmp_path / "source-artifacts"
    source_artifacts.mkdir()
    (source_artifacts / "object").write_bytes(b"content")
    source = _service(_database(tmp_path / "source.db"), source_artifacts)
    archive = source.create(tmp_path / "backup.tar")

    class PostgreSQLTarget(_FailingDatabaseAdapter):
        backend = "postgresql"

        def create_dump(self, destination: Path) -> None:
            del destination

    mismatch = BackupService(PostgreSQLTarget(), LocalArtifactBackupAdapter(tmp_path / "target"))
    with pytest.raises(ValueError, match="backend does not match"):
        mismatch.restore(archive, confirmation="RESTORE")

    target_artifacts = tmp_path / "target-artifacts"
    target_artifacts.mkdir()
    (target_artifacts / "existing").write_bytes(b"keep")
    target = _service(f"sqlite:///{tmp_path / 'target.db'}", target_artifacts)
    with pytest.raises(FileExistsError, match="artifact target"):
        target.restore(archive, confirmation="RESTORE")


def _write_tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w") as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content))


def test_archive_helpers_reject_wrong_suffix_duplicate_and_nonregular_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    with pytest.raises(ValueError, match="must end"):
        backup_module._write_archive(root, tmp_path / "backup.zip")
    with pytest.raises(ValueError, match="must end"):
        backup_module._materialize_tar(tmp_path / "backup.zip", tmp_path)

    duplicate = tmp_path / "duplicate.tar"
    first = tarfile.TarInfo("same")
    first.size = 1
    second = tarfile.TarInfo("same")
    second.size = 1
    _write_tar(duplicate, [(first, b"a"), (second, b"b")])
    with (
        tarfile.open(duplicate, "r:") as archive,
        pytest.raises(ValueError, match="duplicate archive entry"),
    ):
        backup_module._safe_members(archive)

    directory = tmp_path / "directory.tar"
    member = tarfile.TarInfo("folder")
    member.type = tarfile.DIRTYPE
    _write_tar(directory, [(member, b"")])
    with (
        tarfile.open(directory, "r:") as archive,
        pytest.raises(ValueError, match="non-regular"),
    ):
        backup_module._safe_members(archive)


def test_manifest_reader_rejects_missing_large_and_invalid_json(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w"):
        pass
    with (
        tarfile.open(empty, "r:") as archive,
        pytest.raises(ValueError, match="manifest is missing"),
    ):
        backup_module._read_manifest(archive, {})

    large = tarfile.TarInfo("manifest.json")
    large.size = 1024 * 1024 + 1
    with (
        tarfile.open(empty, "r:") as archive,
        pytest.raises(ValueError, match="unreasonably large"),
    ):
        backup_module._read_manifest(archive, {"manifest.json": large})

    invalid = tmp_path / "invalid.tar"
    member = tarfile.TarInfo("manifest.json")
    member.size = 1
    _write_tar(invalid, [(member, b"{")])
    with tarfile.open(invalid, "r:") as archive:
        members = backup_module._safe_members(archive)
        with pytest.raises(ValueError, match="invalid JSON"):
            backup_module._read_manifest(archive, members)


def test_backup_compatibility_rejects_future_old_and_new_schema() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        backup_module._validate_application_compatibility(_manifest(application_version="1.0.0"))
    with pytest.raises(ValueError, match="incompatible"):
        backup_module._validate_application_compatibility(_manifest(application_version="0.7.0"))
    with pytest.raises(ValueError, match="newer than supported"):
        backup_module._validate_application_compatibility(
            _manifest(database_schema=SCHEMA_VERSION + 1)
        )


@pytest.mark.parametrize("value", ["", "bad\\path", "bad\x00path"])
def test_archive_path_validation_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        backup_module._validate_archive_path(value)


@pytest.mark.parametrize(
    "value",
    ["", "trailing/", "double//slash", "bad\\key", "bad\x00key", "bad\nkey", "/absolute"],
)
def test_s3_relative_key_validation_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        backup_module._validate_s3_relative_key(value)


def test_artifact_path_validation_requires_canonical_artifact_subpath() -> None:
    with pytest.raises(ValueError, match="canonical"):
        backup_module._artifact_entry_relative_key("artifacts//object")
    with pytest.raises(ValueError, match="below artifacts"):
        backup_module._artifact_entry_relative_key("database.dump")
    with pytest.raises(ValueError, match="prefix"):
        backup_module._validate_s3_prefix(cast(str, 7))


def test_zstandard_helper_reports_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(name: str) -> ModuleType:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    with pytest.raises(RuntimeError, match="zstandard"):
        backup_module._zstandard()
