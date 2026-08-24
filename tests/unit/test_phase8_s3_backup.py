from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lab_platform.control_plane.backup import (
    BackupFileEntry,
    BackupService,
    DatabaseBackupAdapter,
    LocalArtifactBackupAdapter,
    S3ArtifactBackupAdapter,
)
from lab_platform.control_plane.cli import _backup_service
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.persistence import SQLiteDatabase


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
