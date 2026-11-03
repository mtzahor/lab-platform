from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
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
from lab_platform.control_plane.cli import _backup_service
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.core import VERSION
from lab_platform.persistence import SCHEMA_VERSION, SQLiteDatabase
from psycopg.conninfo import conninfo_to_dict

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
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    adapter = DatabaseBackupAdapter(
        "postgresql+psycopg://user:s3cr%40t@db/lab?sslmode=require",
        run=run,
    )

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
    connected_calls = [calls[index] for index in (0, 2, 3)]
    for command, environment in connected_calls:
        database_option = command.index("--dbname")
        assert conninfo_to_dict(command[database_option + 1]) == {
            "dbname": "lab",
            "host": "db",
            "sslmode": "require",
            "user": "user",
        }
        assert environment["PGPASSWORD"] == "s3cr@t"
    assert "--dbname" not in calls[1][0]
    assert all("s3cr" not in argument for command, _ in calls for argument in command)
    assert "--clean" not in calls[2][0]
    assert calls[3][0][-3:-1] == ["--clean", "--if-exists"]
    assert all("PGDATABASE" not in environment for _, environment in calls)
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


@pytest.mark.parametrize("directory_name", ["run#1", "run?1"])
def test_sqlite_adapter_verifies_and_restores_paths_with_uri_delimiters(
    tmp_path: Path,
    directory_name: str,
) -> None:
    # An unencoded "file:" URI ends the path at "#" or "?", which used to verify
    # and restore a different, empty database without any error.
    directory = tmp_path / directory_name
    directory.mkdir()
    database_path = directory / "control.db"
    writer = sqlite3.connect(database_path)
    writer.execute("CREATE TABLE marker (value TEXT)")
    writer.execute("INSERT INTO marker VALUES ('preserved')")
    writer.commit()
    writer.close()

    adapter = DatabaseBackupAdapter(f"sqlite:///{database_path}")
    dump = directory / "backup.dump"
    adapter.create_dump(dump)
    adapter.verify_dump(dump)
    database_path.unlink()
    adapter.restore_dump(dump, overwrite=True)

    restored = sqlite3.connect(database_path)
    try:
        assert restored.execute("SELECT value FROM marker").fetchall() == [("preserved",)]
    finally:
        restored.close()

    corrupt = directory / "corrupt.dump"
    corrupt.write_bytes(b"not a sqlite database" * 64)
    with pytest.raises(sqlite3.DatabaseError):
        adapter.verify_dump(corrupt)


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
    (root / ".lab-platform-restore-orphan" / "previous").mkdir(parents=True)
    (root / ".lab-platform-restore-orphan" / "previous" / "old").write_bytes(b"old")
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


def test_local_artifact_restore_refuses_nonempty_target_and_stages_inside_mount(
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

    with pytest.raises(FileExistsError, match="explicit overwrite"):
        adapter.restore(source, [entry], overwrite=False)

    original_mkdtemp = tempfile.mkdtemp
    original_mkdir = Path.mkdir
    original_replace = os.replace
    staging_parents: list[Path] = []

    def mounted_mkdtemp(*, prefix: str, dir: Path) -> str:
        staging_parents.append(dir)
        return original_mkdtemp(prefix=prefix, dir=dir)

    def reject_parent_write(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == root.parent:
            raise PermissionError("simulated read-only container filesystem")
        original_mkdir(path, *args, **kwargs)

    def reject_mount_replacement(source_path: Path, destination_path: Path) -> None:
        if source_path == root or destination_path == root:
            raise OSError("simulated volume mount replacement")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(tempfile, "mkdtemp", mounted_mkdtemp)
    monkeypatch.setattr(Path, "mkdir", reject_parent_write)
    monkeypatch.setattr(os, "replace", reject_mount_replacement)

    assert adapter.restore(source, [entry], overwrite=True) == 1
    assert staging_parents == [root]
    assert not (root / "old").exists()
    assert (root / "content").read_bytes() == b"new"


def test_local_artifact_restore_rolls_back_after_replacement_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a-installed").write_bytes(b"new-a")
    (source / "z-failure").write_bytes(b"new-z")
    entries = [
        BackupFileEntry("artifacts/a-installed", 5, hashlib.sha256(b"new-a").hexdigest()),
        BackupFileEntry("artifacts/z-failure", 5, hashlib.sha256(b"new-z").hexdigest()),
    ]
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "old").write_bytes(b"old")
    adapter = LocalArtifactBackupAdapter(root)
    original_replace = os.replace
    replacement_failed = False

    def fail_stage_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal replacement_failed
        if destination_path == root / "z-failure" and source_path.parent.name == "incoming":
            replacement_failed = True
            raise OSError("simulated replacement failure")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_stage_replace)
    with pytest.raises(OSError, match="simulated"):
        adapter.restore(source, entries, overwrite=True)

    assert replacement_failed is True
    assert (root / "old").read_bytes() == b"old"
    assert not (root / "a-installed").exists()
    assert not (root / "z-failure").exists()
    assert not any(path.name.startswith(".lab-platform-restore-") for path in root.iterdir())


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


def _database_backup_restore(path: Path) -> str:
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


def _service_backup_restore(database_url: str, artifacts: Path) -> BackupService:
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
    service = _service_backup_restore(
        _database_backup_restore(tmp_path / "source.db"), source_artifacts
    )

    backup = service.create(tmp_path / "backup.tar")
    verification = service.verify(backup, deep=True)

    assert verification.ok
    assert verification.manifest.database_schema == 12
    assert verification.manifest.artifacts_included is True
    assert verification.entries_verified == 2

    target_database = tmp_path / "restored.db"
    target_artifacts = tmp_path / "restored-artifacts"
    restored = _service_backup_restore(f"sqlite:///{target_database}", target_artifacts).restore(
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
    service = _service_backup_restore(
        _database_backup_restore(tmp_path / "source.db"), tmp_path / "source-artifacts"
    )
    backup = service.create(tmp_path / "backup.tar", include_artifacts=False)
    target_path = tmp_path / "target.db"
    target = _service_backup_restore(
        _database_backup_restore(target_path), tmp_path / "target-artifacts"
    )

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
    service = _service_backup_restore(
        _database_backup_restore(tmp_path / "source.db"), source_artifacts
    )
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

    service = _service_backup_restore(f"sqlite:///{tmp_path / 'unused.db'}", tmp_path / "artifacts")
    with pytest.raises(ValueError, match="unsafe"):
        service.verify(archive_path)
    assert not (tmp_path / "escape").exists()


def test_verify_rejects_undeclared_payload(tmp_path: Path) -> None:
    service = _service_backup_restore(
        _database_backup_restore(tmp_path / "source.db"), tmp_path / "artifacts"
    )
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


class _MemoryS3Client:
    def __init__(self, *, page_size: int = 1000) -> None:
        self.page_size = page_size
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str], str]] = {}
        self.list_calls = 0
        self.deleted: list[tuple[str, str]] = []

    def put(
        self,
        bucket: str,
        key: str,
        content: bytes,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        etag = f'"{hashlib.sha256(content).hexdigest()[:32]}"'
        self.objects[(bucket, key)] = (content, dict(metadata or {}), etag)

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, Any]:
        self.list_calls += 1
        bucket = str(kwargs["Bucket"])
        prefix = str(kwargs["Prefix"])
        keys = sorted(
            key
            for candidate_bucket, key in self.objects
            if candidate_bucket == bucket and key.startswith(prefix)
        )
        start = int(str(kwargs.get("ContinuationToken", "0")))
        requested = int(str(kwargs.get("MaxKeys", 1000)))
        page = keys[start : start + min(self.page_size, requested)]
        next_index = start + len(page)
        response: dict[str, Any] = {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[(bucket, key)][0]),
                    "ETag": self.objects[(bucket, key)][2],
                }
                for key in page
            ],
            "IsTruncated": next_index < len(keys),
        }
        if next_index < len(keys):
            response["NextContinuationToken"] = str(next_index)
        return response

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        content, metadata, etag = self.objects[self._key(kwargs)]
        return {
            "Body": io.BytesIO(content),
            "ContentLength": len(content),
            "Metadata": metadata,
            "ETag": etag,
        }

    def head_object(self, **kwargs: object) -> Mapping[str, Any]:
        content, metadata, etag = self.objects[self._key(kwargs)]
        return {
            "ContentLength": len(content),
            "Metadata": metadata,
            "ETag": etag,
        }

    def upload_fileobj(
        self,
        file_object: Any,
        bucket: str,
        key: str,
        ExtraArgs: Mapping[str, object] | None = None,
    ) -> None:
        metadata: dict[str, str] = {}
        if ExtraArgs is not None and isinstance(ExtraArgs.get("Metadata"), Mapping):
            raw_metadata = ExtraArgs["Metadata"]
            assert isinstance(raw_metadata, Mapping)
            metadata = {str(name): str(value) for name, value in raw_metadata.items()}
        self.put(bucket, key, file_object.read(), metadata=metadata)

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]:
        key = self._key(kwargs)
        self.deleted.append(key)
        self.objects.pop(key, None)
        return {}

    @staticmethod
    def _key(kwargs: Mapping[str, object]) -> tuple[str, str]:
        return str(kwargs["Bucket"]), str(kwargs["Key"])


def _entry(relative: str, content: bytes) -> BackupFileEntry:
    return BackupFileEntry(
        path=f"artifacts/{relative}",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_s3_snapshot_paginates_streams_and_verifies_checksums(tmp_path: Path) -> None:
    client = _MemoryS3Client(page_size=1)
    for relative, content in (
        ("objects/one/content", b"first"),
        ("objects/two/content", b"second"),
    ):
        client.put(
            "backups",
            f"tenant/artifacts/{relative}",
            content,
            metadata={"sha256": hashlib.sha256(content).hexdigest()},
        )
    client.put("backups", "another-prefix/secret", b"outside")
    adapter = S3ArtifactBackupAdapter(
        bucket="backups",
        prefix="tenant/artifacts",
        client=client,
    )

    entries = adapter.snapshot(tmp_path / "snapshot")

    assert [entry.path for entry in entries] == [
        "artifacts/objects/one/content",
        "artifacts/objects/two/content",
    ]
    assert (tmp_path / "snapshot" / "objects" / "one" / "content").read_bytes() == b"first"
    assert (tmp_path / "snapshot" / "objects" / "two" / "content").read_bytes() == b"second"
    assert client.list_calls == 2


def test_s3_backup_service_create_verify_restore_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    database = SQLiteDatabase(database_path)
    database.initialize()
    database.close()
    source_client = _MemoryS3Client(page_size=1)
    content = b"archived-s3-artifact"
    source_client.put(
        "source",
        "tenant/artifacts/objects/one/content",
        content,
        metadata={"sha256": hashlib.sha256(content).hexdigest()},
    )
    source = BackupService(
        DatabaseBackupAdapter(f"sqlite:///{database_path}"),
        S3ArtifactBackupAdapter(
            bucket="source",
            prefix="tenant/artifacts",
            client=source_client,
        ),
        clock=lambda: datetime(2026, 8, 24, tzinfo=UTC),
    )

    archive = source.create(tmp_path / "s3-backup.tar")
    verification = source.verify(archive)
    assert [entry.path for entry in verification.manifest.artifacts] == [
        "artifacts/objects/one/content"
    ]

    target_client = _MemoryS3Client()
    result = BackupService(
        DatabaseBackupAdapter(f"sqlite:///{tmp_path / 'target.db'}"),
        S3ArtifactBackupAdapter(
            bucket="target",
            prefix="restored/artifacts",
            client=target_client,
        ),
    ).restore(archive, confirmation="RESTORE")

    assert result.artifacts_restored == 1
    restored = target_client.objects[("target", "restored/artifacts/objects/one/content")]
    assert restored[:2] == (
        content,
        {"sha256": hashlib.sha256(content).hexdigest()},
    )


def test_s3_snapshot_rejects_unsafe_keys_and_bad_checksum_metadata(tmp_path: Path) -> None:
    client = _MemoryS3Client()
    content = b"content"
    client.put("backups", "namespace/../escape", content)
    adapter = S3ArtifactBackupAdapter(bucket="backups", prefix="namespace", client=client)
    with pytest.raises(ValueError, match="unsafe"):
        adapter.snapshot(tmp_path / "unsafe")

    client.objects.clear()
    client.put(
        "backups",
        "namespace/object",
        content,
        metadata={"sha256": "0" * 64},
    )
    with pytest.raises(ValueError, match="checksum metadata mismatch"):
        adapter.snapshot(tmp_path / "bad-checksum")


def test_s3_restore_requires_overwrite_and_cleans_only_configured_prefix(
    tmp_path: Path,
) -> None:
    client = _MemoryS3Client(page_size=1)
    client.put("backups", "tenant/artifacts/stale/one", b"stale-one")
    client.put("backups", "tenant/artifacts/stale/two", b"stale-two")
    client.put("backups", "tenant/other/must-survive", b"outside")
    adapter = S3ArtifactBackupAdapter(
        bucket="backups",
        prefix="tenant/artifacts",
        client=client,
    )
    content = b"restored-content"
    source = tmp_path / "artifacts"
    source_path = source / "objects" / "restored" / "content"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)
    entry = _entry("objects/restored/content", content)

    with pytest.raises(FileExistsError, match="explicit overwrite"):
        adapter.restore(source, [entry], overwrite=False)

    assert adapter.restore(source, [entry], overwrite=True) == 1
    assert ("backups", "tenant/artifacts/stale/one") in client.deleted
    assert ("backups", "tenant/artifacts/stale/two") in client.deleted
    assert client.objects[("backups", "tenant/other/must-survive")][0] == b"outside"
    restored = client.objects[("backups", "tenant/artifacts/objects/restored/content")]
    assert restored[:2] == (content, {"sha256": entry.sha256})


def test_s3_restore_validates_all_sources_before_overwrite_cleanup(tmp_path: Path) -> None:
    client = _MemoryS3Client()
    client.put("backups", "namespace/existing", b"keep-on-validation-error")
    adapter = S3ArtifactBackupAdapter(bucket="backups", prefix="namespace", client=client)
    source = tmp_path / "artifacts"
    source.mkdir()
    (source / "replacement").write_bytes(b"different")
    entry = _entry("replacement", b"expected")

    with pytest.raises(ValueError, match="checksum mismatch"):
        adapter.restore(source, [entry], overwrite=True)

    assert client.objects[("backups", "namespace/existing")][0] == b"keep-on-validation-error"
    assert client.deleted == []


def test_s3_client_factory_is_lazy_and_reused() -> None:
    client = _MemoryS3Client()
    calls = 0

    def factory() -> _MemoryS3Client:
        nonlocal calls
        calls += 1
        return client

    adapter = S3ArtifactBackupAdapter(
        bucket="backups",
        prefix="namespace",
        client_factory=factory,
    )
    assert calls == 0
    assert adapter.target_is_empty() is True
    assert adapter.target_is_empty() is True
    assert calls == 1


def test_database_only_restore_adapter_does_not_erase_local_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    existing = root / "existing"
    existing.write_bytes(b"must-survive")

    assert LocalArtifactBackupAdapter(root).restore(tmp_path, (), overwrite=True) == 0
    assert existing.read_bytes() == b"must-survive"


def test_control_plane_backup_service_selects_s3_backend_without_eager_sdk_import() -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "artifacts": {
                "storage_backend": "s3",
                "s3": {
                    "bucket": "phase8-backups",
                    "prefix": "tenant/artifacts",
                },
            },
            "development": {"allow_tls_termination_proxy": True},
        }
    )

    service = _backup_service(config)

    assert isinstance(service.artifacts, S3ArtifactBackupAdapter)
    assert service.configuration["artifact_backend"] == "s3"
    assert service.configuration["artifact_bucket"] == "phase8-backups"
    assert service.configuration["artifact_prefix"] == "tenant/artifacts"


@pytest.mark.parametrize("prefix", ("", "/", "../escape", "nested//empty", "bad\\key"))
def test_s3_backup_requires_safe_nonempty_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix|unsafe"):
        S3ArtifactBackupAdapter(bucket="backups", prefix=prefix, client=_MemoryS3Client())
