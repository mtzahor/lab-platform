from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import unquote

from lab_platform.control_plane.compatibility import ApplicationVersion
from lab_platform.core import VERSION
from lab_platform.persistence import SCHEMA_VERSION, inspect_database_schema
from psycopg.conninfo import conninfo_to_dict, make_conninfo

BACKUP_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_DATABASE_DUMP_NAME = "database.dump"
_READ_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupFileEntry:
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_archive_path(self.path)
        if self.size_bytes < 0:
            raise ValueError("backup entry size cannot be negative")
        _validate_sha256(self.sha256)

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "size_bytes": self.size_bytes, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    application_version: str
    database_schema: int
    created_at: datetime
    database_backend: str
    database_dump: BackupFileEntry
    artifacts_included: bool
    artifacts: tuple[BackupFileEntry, ...] = ()
    configuration: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.format_version != BACKUP_FORMAT_VERSION:
            raise ValueError(f"unsupported backup format version: {self.format_version}")
        ApplicationVersion.parse(self.application_version)
        if self.database_schema < 0:
            raise ValueError("database schema version cannot be negative")
        _utc(self.created_at)
        if self.database_backend not in {"postgresql", "sqlite"}:
            raise ValueError("backup database backend must be postgresql or sqlite")
        if bool(self.artifacts) != self.artifacts_included:
            raise ValueError("artifact manifest entries do not match artifacts_included")
        paths = [self.database_dump.path, *(entry.path for entry in self.artifacts)]
        if len(set(paths)) != len(paths):
            raise ValueError("backup manifest contains duplicate payload paths")

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "application_version": self.application_version,
            "database_schema": self.database_schema,
            "created_at": _utc(self.created_at).isoformat().replace("+00:00", "Z"),
            "database_backend": self.database_backend,
            "database_dump": self.database_dump.as_dict(),
            "artifacts_included": self.artifacts_included,
            "artifacts": [entry.as_dict() for entry in self.artifacts],
            "configuration": dict(sorted(self.configuration.items())),
        }

    @classmethod
    def from_dict(cls, value: object) -> BackupManifest:
        if not isinstance(value, Mapping):
            raise ValueError("backup manifest must be a JSON object")
        allowed = {
            "format_version",
            "application_version",
            "database_schema",
            "created_at",
            "database_backend",
            "database_dump",
            "artifacts_included",
            "artifacts",
            "configuration",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("backup manifest has unknown fields: " + ", ".join(sorted(unknown)))
        try:
            created_at = datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
            raw_artifacts = value.get("artifacts", [])
            raw_configuration = value.get("configuration", {})
            if not isinstance(raw_artifacts, list):
                raise ValueError("backup artifacts must be an array")
            if not isinstance(raw_configuration, Mapping) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in raw_configuration.items()
            ):
                raise ValueError("backup configuration references must be string values")
            return cls(
                format_version=_strict_int(value["format_version"], "format_version"),
                application_version=_strict_str(
                    value["application_version"], "application_version"
                ),
                database_schema=_strict_int(value["database_schema"], "database_schema"),
                created_at=created_at,
                database_backend=_strict_str(value["database_backend"], "database_backend"),
                database_dump=_file_entry(value["database_dump"]),
                artifacts_included=_strict_bool(value["artifacts_included"], "artifacts_included"),
                artifacts=tuple(_file_entry(item) for item in raw_artifacts),
                configuration={str(key): str(item) for key, item in raw_configuration.items()},
            )
        except KeyError as exc:
            raise ValueError(f"backup manifest is missing required field: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class BackupVerification:
    path: Path
    manifest: BackupManifest
    entries_verified: int
    bytes_verified: int

    @property
    def ok(self) -> bool:
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "path": str(self.path),
            "entries_verified": self.entries_verified,
            "bytes_verified": self.bytes_verified,
            "manifest": self.manifest.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup: Path
    database_restored: bool
    artifacts_restored: int
    manifest: BackupManifest


class BackupDatabaseAdapter(Protocol):
    @property
    def backend(self) -> str: ...

    def schema_version(self) -> int: ...

    def create_dump(self, destination: Path) -> None: ...

    def verify_dump(self, source: Path) -> None: ...

    def target_is_empty(self) -> bool: ...

    def restore_dump(self, source: Path, *, overwrite: bool) -> None: ...


class ArtifactBackupAdapter(Protocol):
    def snapshot(self, destination: Path) -> tuple[BackupFileEntry, ...]: ...

    def target_is_empty(self) -> bool: ...

    def restore(
        self,
        source: Path,
        entries: Iterable[BackupFileEntry],
        *,
        overwrite: bool,
    ) -> int: ...


class S3BackupClient(Protocol):
    """Small synchronous S3 surface used by the backup implementation."""

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def upload_fileobj(
        self,
        file_object: Any,
        bucket: str,
        key: str,
        ExtraArgs: Mapping[str, object] | None = None,
    ) -> object: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]: ...


class BackupEventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, object]) -> None: ...


class DatabaseBackupAdapter:
    """Production PostgreSQL and local SQLite dump/restore implementation."""

    def __init__(
        self,
        url: str,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.url = url
        self._run = run
        self._sqlite_path: Path | None
        if url.startswith("sqlite:///"):
            self._backend = "sqlite"
            raw_path = unquote(url.removeprefix("sqlite:///"))
            self._sqlite_path = Path(raw_path).expanduser().resolve()
        elif url.startswith(("postgresql://", "postgresql+psycopg://")):
            self._backend = "postgresql"
            self._sqlite_path = None
        else:
            raise ValueError("backup database URL must use SQLite or PostgreSQL")

    @property
    def backend(self) -> str:
        return self._backend

    def schema_version(self) -> int:
        return inspect_database_schema(self.url).current_version

    def create_dump(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self._sqlite_path is not None:
            if not self._sqlite_path.is_file():
                raise FileNotFoundError(f"SQLite database does not exist: {self._sqlite_path}")
            source = sqlite3.connect(self._sqlite_path)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                target.execute("PRAGMA journal_mode=DELETE").fetchone()
            finally:
                target.close()
                source.close()
            return
        self._postgres_command(
            ["pg_dump", "--format=custom", "--no-owner", "--file", str(destination)],
            connect=True,
        )

    def verify_dump(self, source: Path) -> None:
        if self._sqlite_path is not None:
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
            if row is None or row[0] != "ok":
                raise ValueError("SQLite backup failed PRAGMA quick_check")
            return
        self._postgres_command(["pg_restore", "--list", str(source)])

    def target_is_empty(self) -> bool:
        if self._sqlite_path is not None:
            return not self._sqlite_path.exists() or self._sqlite_path.stat().st_size == 0
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - required dependency
            raise RuntimeError("PostgreSQL restore checks require psycopg") from exc
        connection = psycopg.connect(self._normalized_postgresql_url())
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchone()
        finally:
            connection.close()
        return row is not None and int(row[0]) == 0

    def restore_dump(self, source: Path, *, overwrite: bool) -> None:
        if self._sqlite_path is not None:
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            incoming = self._sqlite_path.with_name(f".{self._sqlite_path.name}.restore")
            incoming.unlink(missing_ok=True)
            source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            target_connection = sqlite3.connect(incoming)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()
            os.replace(incoming, self._sqlite_path)
            return
        command = ["pg_restore", "--exit-on-error", "--no-owner", "--no-privileges"]
        if overwrite:
            command.extend(["--clean", "--if-exists"])
        command.append(str(source))
        self._postgres_command(command, connect=True)

    def _postgres_command(self, command: list[str], *, connect: bool = False) -> None:
        environment = dict(os.environ)
        environment.pop("PGDATABASE", None)
        environment.setdefault("PGCONNECT_TIMEOUT", "10")
        invoked_command = list(command)
        if connect:
            parameters = conninfo_to_dict(self._normalized_postgresql_url())
            password = parameters.pop("password", None)
            safe_parameters = cast(dict[str, Any], parameters)
            invoked_command[1:1] = ["--dbname", make_conninfo(**safe_parameters)]
            if password is not None:
                environment["PGPASSWORD"] = str(password)
        try:
            self._run(
                invoked_command,
                env=environment,
                check=True,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{command[0]} is required for PostgreSQL backup operations"
            ) from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "command failed").strip()
            raise RuntimeError(f"{command[0]} failed: {message[:1000]}") from exc

    def _normalized_postgresql_url(self) -> str:
        if self.url.startswith("postgresql+psycopg://"):
            return "postgresql://" + self.url.removeprefix("postgresql+psycopg://")
        return self.url


class LocalArtifactBackupAdapter:
    """Checksum-stable local artifact snapshots with staged restore replacement."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def snapshot(self, destination: Path) -> tuple[BackupFileEntry, ...]:
        if not self.root.exists():
            return ()
        entries: list[BackupFileEntry] = []
        for source in sorted(self.root.rglob("*")):
            if ".incoming" in source.relative_to(self.root).parts:
                continue
            if source.is_symlink():
                raise ValueError(f"artifact backup refuses symbolic links: {source}")
            if not source.is_file():
                continue
            relative = source.relative_to(self.root)
            archive_path = PurePosixPath("artifacts", *relative.parts).as_posix()
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            before = source.stat()
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as input_stream, target.open("xb") as output_stream:
                while chunk := input_stream.read(_READ_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
                    output_stream.write(chunk)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError(f"artifact changed while backup was created: {source}")
            entries.append(BackupFileEntry(archive_path, size, digest.hexdigest()))
        return tuple(entries)

    def target_is_empty(self) -> bool:
        return not self.root.exists() or not any(path.is_file() for path in self.root.rglob("*"))

    def restore(
        self,
        source: Path,
        entries: Iterable[BackupFileEntry],
        *,
        overwrite: bool,
    ) -> int:
        entries = tuple(entries)
        if not entries:
            # A database-only backup must never erase an existing artifact store.
            return 0
        if not overwrite and not self.target_is_empty():
            raise FileExistsError("restore artifact target is not empty; use explicit overwrite")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.restore-", dir=self.root.parent))
        previous: Path | None = None
        try:
            for entry in entries:
                relative = PurePosixPath(entry.path).relative_to("artifacts")
                source_path = source.joinpath(*relative.parts)
                destination = stage.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
            if self.root.exists():
                previous = self.root.with_name(f".{self.root.name}.previous-{os.getpid()}")
                if previous.exists():
                    raise FileExistsError(f"restore staging path already exists: {previous}")
                os.replace(self.root, previous)
            os.replace(stage, self.root)
            if previous is not None:
                shutil.rmtree(previous)
            return len(entries)
        except BaseException:
            if previous is not None and previous.exists() and not self.root.exists():
                os.replace(previous, self.root)
            shutil.rmtree(stage, ignore_errors=True)
            raise


class S3ArtifactBackupAdapter:
    """Stream S3-compatible artifacts into and out of a verified backup.

    The adapter deliberately requires a non-empty namespace prefix. That keeps an
    explicit overwrite restore from ever turning into a bucket-wide cleanup.
    ``boto3`` remains optional and is imported only when an operation needs a
    client; tests and alternative S3 SDKs can inject the small protocol above.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: S3BackupClient | None = None,
        client_factory: Callable[[], S3BackupClient] | None = None,
    ) -> None:
        normalized_bucket = bucket.strip()
        if not normalized_bucket:
            raise ValueError("S3 artifact backup bucket must not be empty")
        if client is not None and client_factory is not None:
            raise ValueError("configure either an S3 client or client factory, not both")
        self.bucket = normalized_bucket
        self.prefix = _validate_s3_prefix(prefix)
        self._endpoint_url = endpoint_url
        self._region_name = region_name
        self._client = client
        self._client_factory = client_factory

    def snapshot(self, destination: Path) -> tuple[BackupFileEntry, ...]:
        entries: list[BackupFileEntry] = []
        destination.mkdir(parents=True, exist_ok=True)
        for listed in self._list_objects():
            object_key = _strict_str(listed.get("Key"), "S3 object key")
            if object_key.endswith("/") and listed.get("Size") == 0:
                # Common S3 console folder markers are not artifact content.
                continue
            relative = self._relative_key(object_key)
            archive_path = PurePosixPath("artifacts", *PurePosixPath(relative).parts).as_posix()
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            response = self._require_client().get_object(Bucket=self.bucket, Key=object_key)
            body = response.get("Body")
            if body is None or not hasattr(body, "read"):
                raise RuntimeError(f"S3 object response is not readable: {object_key}")
            digest = hashlib.sha256()
            size = 0
            try:
                with target.open("xb") as output:
                    while chunk := body.read(_READ_SIZE):
                        if not isinstance(chunk, bytes):
                            raise RuntimeError(f"S3 object yielded non-byte content: {object_key}")
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            sha256 = digest.hexdigest()
            self._verify_download(
                object_key,
                listed=listed,
                response=response,
                size=size,
                sha256=sha256,
            )
            entries.append(BackupFileEntry(archive_path, size, sha256))
        return tuple(entries)

    def target_is_empty(self) -> bool:
        return next(iter(self._list_objects(maximum_keys=1)), None) is None

    def restore(
        self,
        source: Path,
        entries: Iterable[BackupFileEntry],
        *,
        overwrite: bool,
    ) -> int:
        entries = tuple(entries)
        if not entries:
            return 0
        prepared: list[tuple[BackupFileEntry, str, Path]] = []
        seen_object_keys: set[str] = set()
        for entry in entries:
            relative = _artifact_entry_relative_key(entry.path)
            object_key = self._object_key(relative)
            if object_key in seen_object_keys:
                raise ValueError(f"backup contains a duplicate S3 artifact key: {object_key}")
            seen_object_keys.add(object_key)
            source_path = source.joinpath(*PurePosixPath(relative).parts)
            actual = _entry_for_file(source_path, entry.path)
            if actual != entry:
                raise ValueError(f"restore artifact checksum mismatch for {entry.path}")
            prepared.append((entry, object_key, source_path))

        existing = tuple(self._list_objects())
        if existing and not overwrite:
            raise FileExistsError("restore artifact target is not empty; use explicit overwrite")
        if overwrite:
            self._delete_objects(existing)

        uploaded: list[str] = []
        try:
            for entry, object_key, source_path in prepared:
                # Track before upload so even a client that fails after creating a
                # partial object is included in best-effort rollback cleanup.
                uploaded.append(object_key)
                with source_path.open("rb") as stream:
                    self._require_client().upload_fileobj(
                        stream,
                        self.bucket,
                        object_key,
                        ExtraArgs={"Metadata": {"sha256": entry.sha256}},
                    )
                self._verify_uploaded(object_key, entry)
        except BaseException:
            # A failed restore must not leave a mixture of restored objects behind.
            self._delete_keys(uploaded)
            raise
        return len(entries)

    def _list_objects(self, *, maximum_keys: int | None = None) -> Iterable[Mapping[str, Any]]:
        continuation_token: str | None = None
        seen_tokens: set[str] = set()
        seen_keys: set[str] = set()
        yielded = 0
        while True:
            request: dict[str, object] = {
                "Bucket": self.bucket,
                "Prefix": f"{self.prefix}/",
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            if maximum_keys is not None:
                request["MaxKeys"] = min(1000, maximum_keys - yielded)
            response = self._require_client().list_objects_v2(**request)
            contents = response.get("Contents", ())
            if not isinstance(contents, list | tuple):
                raise RuntimeError("S3 list_objects_v2 returned invalid Contents")
            for item in contents:
                if not isinstance(item, Mapping):
                    raise RuntimeError("S3 list_objects_v2 returned an invalid object entry")
                # Validate every returned key before exposing it to backup or cleanup logic.
                object_key = _strict_str(item.get("Key"), "S3 object key")
                if object_key in seen_keys:
                    raise RuntimeError(f"S3 pagination returned a duplicate object: {object_key}")
                seen_keys.add(object_key)
                if object_key.endswith("/") and item.get("Size") == 0:
                    namespace = f"{self.prefix}/"
                    if not object_key.startswith(namespace):
                        raise ValueError(
                            "S3 listed an object outside the configured artifact prefix"
                        )
                    marker = object_key.removeprefix(namespace).removesuffix("/")
                    if marker:
                        _validate_s3_relative_key(marker)
                    continue
                self._relative_key(object_key)
                yield item
                yielded += 1
                if maximum_keys is not None and yielded >= maximum_keys:
                    return
            if not response.get("IsTruncated"):
                return
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise RuntimeError("S3 pagination did not return a fresh continuation token")
            seen_tokens.add(next_token)
            continuation_token = next_token

    def _verify_download(
        self,
        object_key: str,
        *,
        listed: Mapping[str, Any],
        response: Mapping[str, Any],
        size: int,
        sha256: str,
    ) -> None:
        listed_size = listed.get("Size")
        if isinstance(listed_size, int) and listed_size != size:
            raise RuntimeError(f"S3 object changed while backup was created: {object_key}")
        response_size = response.get("ContentLength")
        if isinstance(response_size, int) and response_size != size:
            raise RuntimeError(f"S3 object response size mismatch: {object_key}")
        head = self._require_client().head_object(Bucket=self.bucket, Key=object_key)
        if head.get("ContentLength") != size:
            raise RuntimeError(f"S3 object changed while backup was created: {object_key}")
        etags = {
            etag
            for etag in (listed.get("ETag"), response.get("ETag"), head.get("ETag"))
            if isinstance(etag, str)
        }
        if len(etags) > 1:
            raise RuntimeError(f"S3 object changed while backup was created: {object_key}")
        metadata = head.get("Metadata", {})
        stored_sha256 = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if stored_sha256 is not None:
            if not isinstance(stored_sha256, str):
                raise RuntimeError(f"S3 object has invalid checksum metadata: {object_key}")
            _validate_sha256(stored_sha256)
            if stored_sha256 != sha256:
                raise ValueError(f"S3 object checksum metadata mismatch: {object_key}")

    def _verify_uploaded(self, object_key: str, entry: BackupFileEntry) -> None:
        response = self._require_client().head_object(Bucket=self.bucket, Key=object_key)
        if response.get("ContentLength") != entry.size_bytes:
            raise RuntimeError(f"S3 restore upload size mismatch: {object_key}")
        metadata = response.get("Metadata", {})
        sha256 = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if sha256 != entry.sha256:
            raise RuntimeError(f"S3 restore upload checksum metadata mismatch: {object_key}")

    def _delete_objects(self, objects: Iterable[Mapping[str, Any]]) -> None:
        self._delete_keys([_strict_str(item.get("Key"), "S3 object key") for item in objects])

    def _delete_keys(self, keys: Iterable[str]) -> None:
        for object_key in keys:
            self._relative_key(object_key)
            self._require_client().delete_object(Bucket=self.bucket, Key=object_key)

    def _relative_key(self, object_key: str) -> str:
        namespace = f"{self.prefix}/"
        if not object_key.startswith(namespace):
            raise ValueError("S3 listed an object outside the configured artifact prefix")
        return _validate_s3_relative_key(object_key.removeprefix(namespace))

    def _object_key(self, relative: str) -> str:
        return f"{self.prefix}/{_validate_s3_relative_key(relative)}"

    def _require_client(self) -> S3BackupClient:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        try:
            boto3 = importlib.import_module("boto3")
        except ImportError as exc:
            raise RuntimeError(
                "S3 artifact backups require an injected client or the optional boto3 package"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region_name,
        )
        return self._client


class BackupService:
    def __init__(
        self,
        database: BackupDatabaseAdapter,
        artifacts: ArtifactBackupAdapter,
        *,
        configuration: Mapping[str, str] | None = None,
        events: BackupEventSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.configuration = dict(configuration or {})
        self.events = events
        self.clock = clock

    def create(self, destination: Path, *, include_artifacts: bool = True) -> Path:
        created_at = _utc(self.clock())
        output = _backup_output_path(destination, created_at)
        if output.exists():
            raise FileExistsError(f"backup already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        self._emit("BACKUP_STARTED", {"destination": str(output)})
        try:
            with tempfile.TemporaryDirectory(prefix="lab-platform-backup-") as temporary:
                root = Path(temporary)
                database_dump = root / _DATABASE_DUMP_NAME
                self.database.create_dump(database_dump)
                self.database.verify_dump(database_dump)
                database_entry = _entry_for_file(database_dump, _DATABASE_DUMP_NAME)
                artifact_entries = (
                    self.artifacts.snapshot(root / "artifacts") if include_artifacts else ()
                )
                manifest = BackupManifest(
                    format_version=BACKUP_FORMAT_VERSION,
                    application_version=VERSION,
                    database_schema=self.database.schema_version(),
                    created_at=created_at,
                    database_backend=self.database.backend,
                    database_dump=database_entry,
                    artifacts_included=bool(artifact_entries),
                    artifacts=artifact_entries,
                    configuration=self.configuration,
                )
                (root / _MANIFEST_NAME).write_text(
                    json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                _write_archive(root, output)
            verification = self.verify(output, deep=True)
        except BaseException as exc:
            output.unlink(missing_ok=True)
            self._emit("BACKUP_FAILED", {"error": str(exc)[:1000]})
            raise
        self._emit(
            "BACKUP_COMPLETED",
            {
                "path": str(output),
                "entries": verification.entries_verified,
                "bytes": verification.bytes_verified,
            },
        )
        return output

    def verify(self, path: Path, *, deep: bool = True) -> BackupVerification:
        backup = path.expanduser().resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="lab-platform-backup-verify-") as temporary:
            tar_path = _materialize_tar(backup, Path(temporary))
            with tarfile.open(tar_path, "r:") as archive:
                members = _safe_members(archive)
                manifest = _read_manifest(archive, members)
                _validate_application_compatibility(manifest)
                declared = {
                    manifest.database_dump.path: manifest.database_dump,
                    **{entry.path: entry for entry in manifest.artifacts},
                }
                actual_payloads = set(members) - {_MANIFEST_NAME}
                if actual_payloads != set(declared):
                    missing = sorted(set(declared) - actual_payloads)
                    unknown = sorted(actual_payloads - set(declared))
                    raise ValueError(
                        f"backup payload index mismatch; missing={missing}, undeclared={unknown}"
                    )
                total_bytes = 0
                for name, entry in declared.items():
                    member = members[name]
                    if member.size != entry.size_bytes:
                        raise ValueError(f"backup size mismatch for {name}")
                    if deep:
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ValueError(f"backup payload is unreadable: {name}")
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := stream.read(_READ_SIZE):
                            digest.update(chunk)
                            size += len(chunk)
                        if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                            raise ValueError(f"backup checksum mismatch for {name}")
                    total_bytes += member.size
                database_member = archive.extractfile(members[manifest.database_dump.path])
                if database_member is None:
                    raise ValueError("database dump is unreadable")
                database_path = Path(temporary) / "verified-database.dump"
                with database_path.open("xb") as stream:
                    shutil.copyfileobj(database_member, stream)
                self.database.verify_dump(database_path)
        return BackupVerification(backup, manifest, len(declared), total_bytes)

    def restore(
        self,
        path: Path,
        *,
        overwrite: bool = False,
        confirmation: str | None = None,
    ) -> RestoreResult:
        verification = self.verify(path, deep=True)
        if verification.manifest.database_backend != self.database.backend:
            raise ValueError("backup database backend does not match the configured restore target")
        if confirmation != "RESTORE":
            raise PermissionError("restore requires the exact confirmation value RESTORE")
        if not overwrite and not self.database.target_is_empty():
            raise FileExistsError("restore target database is not empty; use explicit overwrite")
        if (
            not overwrite
            and verification.manifest.artifacts
            and not self.artifacts.target_is_empty()
        ):
            raise FileExistsError("restore artifact target is not empty; use explicit overwrite")
        self._emit("RESTORE_STARTED", {"backup": str(verification.path)})
        with tempfile.TemporaryDirectory(prefix="lab-platform-restore-") as temporary:
            root = Path(temporary)
            tar_path = _materialize_tar(verification.path, root)
            with tarfile.open(tar_path, "r:") as archive:
                members = _safe_members(archive)
                for name in (
                    verification.manifest.database_dump.path,
                    *(entry.path for entry in verification.manifest.artifacts),
                ):
                    member = members[name]
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError(f"backup payload is unreadable: {name}")
                    destination = root.joinpath(*PurePosixPath(name).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as output:
                        shutil.copyfileobj(stream, output)
            self.database.restore_dump(
                root / verification.manifest.database_dump.path,
                overwrite=overwrite,
            )
            restored = (
                self.artifacts.restore(
                    root / "artifacts",
                    verification.manifest.artifacts,
                    overwrite=overwrite,
                )
                if verification.manifest.artifacts
                else 0
            )
        self._emit(
            "RESTORE_COMPLETED",
            {"backup": str(verification.path), "artifacts_restored": restored},
        )
        return RestoreResult(verification.path, True, restored, verification.manifest)

    def _emit(self, event_type: str, payload: Mapping[str, object]) -> None:
        if self.events is not None:
            self.events.emit(event_type, payload)


def _write_archive(root: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lab-platform-tar-") as temporary:
        tar_path = Path(temporary) / "backup.tar"
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for source_path in sorted(path for path in root.rglob("*") if path.is_file()):
                archive.add(
                    source_path,
                    arcname=source_path.relative_to(root).as_posix(),
                    recursive=False,
                )
        if output.name.endswith(".tar.zst"):
            zstandard = _zstandard()
            compressor = zstandard.ZstdCompressor(level=10)
            with tar_path.open("rb") as input_stream, output.open("xb") as output_stream:
                compressor.copy_stream(input_stream, output_stream)
        elif output.name.endswith(".tar"):
            with output.open("xb") as output_stream, tar_path.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output_stream)
        else:
            raise ValueError("backup destination must end in .tar.zst or .tar")


def _materialize_tar(backup: Path, temporary: Path) -> Path:
    if backup.name.endswith(".tar"):
        return backup
    if not backup.name.endswith(".tar.zst"):
        raise ValueError("backup path must end in .tar.zst or .tar")
    destination = temporary / "backup.tar"
    zstandard = _zstandard()
    decompressor = zstandard.ZstdDecompressor()
    with backup.open("rb") as source, destination.open("xb") as output:
        decompressor.copy_stream(source, output)
    return destination


def _safe_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = _validate_archive_path(member.name)
        if name in members:
            raise ValueError(f"backup contains a duplicate archive entry: {name}")
        if not member.isfile():
            raise ValueError(f"backup contains a non-regular archive entry: {name}")
        members[name] = member
    return members


def _read_manifest(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
) -> BackupManifest:
    member = members.get(_MANIFEST_NAME)
    if member is None:
        raise ValueError("backup manifest is missing")
    if member.size > 1024 * 1024:
        raise ValueError("backup manifest is unreasonably large")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("backup manifest is unreadable")
    try:
        value = json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup manifest is invalid JSON") from exc
    return BackupManifest.from_dict(value)


def _validate_application_compatibility(manifest: BackupManifest) -> None:
    backup = ApplicationVersion.parse(manifest.application_version)
    current = ApplicationVersion.parse(VERSION)
    if backup.major != current.major or backup > current or backup.minor < current.minor - 1:
        raise ValueError(f"backup application version {backup} is incompatible with {current}")
    if manifest.database_schema > SCHEMA_VERSION:
        raise ValueError(
            f"backup schema {manifest.database_schema} is newer than supported schema "
            f"{SCHEMA_VERSION}"
        )


def _entry_for_file(path: Path, archive_path: str) -> BackupFileEntry:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_READ_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return BackupFileEntry(archive_path, size, digest.hexdigest())


def _file_entry(value: object) -> BackupFileEntry:
    if not isinstance(value, Mapping):
        raise ValueError("backup file entry must be an object")
    if set(value) != {"path", "size_bytes", "sha256"}:
        raise ValueError("backup file entry must contain path, size_bytes, and sha256")
    return BackupFileEntry(
        path=_strict_str(value["path"], "path"),
        size_bytes=_strict_int(value["size_bytes"], "size_bytes"),
        sha256=_strict_str(value["sha256"], "sha256"),
    )


def _validate_archive_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("backup archive path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("backup archive path is unsafe")
    return path.as_posix()


def _validate_s3_prefix(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("S3 artifact backup prefix must be a string")
    normalized = value.strip().strip("/")
    if not normalized:
        raise ValueError("S3 artifact backup requires a non-empty object-key prefix")
    _validate_s3_relative_key(normalized)
    return normalized


def _validate_s3_relative_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("S3 artifact object key is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError("S3 artifact object key is unsafe")
    return value


def _artifact_entry_relative_key(value: str) -> str:
    normalized = _validate_archive_path(value)
    if normalized != value:
        raise ValueError("backup artifact path is not canonical")
    path = PurePosixPath(normalized)
    if len(path.parts) < 2 or path.parts[0] != "artifacts":
        raise ValueError("backup artifact path must be below artifacts/")
    return _validate_s3_relative_key(PurePosixPath(*path.parts[1:]).as_posix())


def _backup_output_path(destination: Path, created_at: datetime) -> Path:
    destination = destination.expanduser()
    if destination.suffix or destination.name.endswith(".tar.zst"):
        return destination.resolve()
    timestamp = created_at.strftime("%Y-%m-%dT%H%M%SZ")
    return (destination / f"lab-platform-backup-{timestamp}.tar.zst").resolve()


def _zstandard() -> Any:
    try:
        return importlib.import_module("zstandard")
    except ImportError as exc:
        raise RuntimeError(".tar.zst backups require the zstandard runtime dependency") from exc


def _strict_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"backup manifest field {name} must be a non-empty string")
    return value


def _strict_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"backup manifest field {name} must be an integer")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"backup manifest field {name} must be a boolean")
    return value


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("backup SHA-256 must contain 64 lowercase hexadecimal digits")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("backup timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ArtifactBackupAdapter",
    "BACKUP_FORMAT_VERSION",
    "BackupDatabaseAdapter",
    "BackupFileEntry",
    "BackupManifest",
    "BackupService",
    "BackupVerification",
    "DatabaseBackupAdapter",
    "LocalArtifactBackupAdapter",
    "RestoreResult",
    "S3ArtifactBackupAdapter",
    "S3BackupClient",
]
