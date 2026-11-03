from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lab_platform.core.artifact_storage import (
    LocalArtifactStorage,
    S3CompatibleArtifactStorage,
    StoredArtifact,
    normalize_storage_key,
)
from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidArtifactError,
)


async def _chunks(*values: bytes) -> AsyncIterable[bytes]:
    for value in values:
        yield value


async def _read_all(chunks: AsyncIterable[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def test_local_artifact_storage_is_atomic_verified_and_confined(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "artifacts"
        storage = LocalArtifactStorage(root)
        content = b"phase-8-artifact"
        digest = hashlib.sha256(content).hexdigest()

        stored = await storage.put(
            "objects/one/content",
            _chunks(content[:7], b"", content[7:]),
            maximum_size_bytes=1024,
            expected_size_bytes=len(content),
            expected_sha256=digest,
        )
        assert stored.key == "objects/one/content"
        assert stored.size_bytes == len(content)
        assert stored.sha256 == digest
        assert await storage.exists(stored.key) is True
        assert await storage.stat(stored.key) == stored
        assert await _read_all(await storage.get(stored.key)) == content
        assert storage.key_from_reference(storage.reference(stored.key)) == stored.key
        assert storage.key_from_reference(str(tmp_path / "outside")) is None

        # Retried writes of the same immutable object are idempotent and leave no
        # abandoned staging file behind.
        assert (
            await storage.put(
                stored.key,
                _chunks(content),
                maximum_size_bytes=1024,
                expected_sha256=digest,
            )
            == stored
        )
        assert list((root / ".incoming").iterdir()) == []

        await storage.delete(stored.key)
        assert await storage.exists(stored.key) is False
        with pytest.raises(ArtifactNotFoundError):
            await storage.stat(stored.key)

    asyncio.run(scenario())


def test_local_artifact_storage_removes_failed_staging_and_blocks_symlink_escape(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "artifacts"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        storage = LocalArtifactStorage(root)

        with pytest.raises(ArtifactNotFoundError):
            await storage.put(
                "escape/content",
                _chunks(b"secret"),
                maximum_size_bytes=100,
            )
        assert not (outside / "content").exists()

        with pytest.raises(ArtifactChecksumMismatchError):
            await storage.put(
                "objects/checksum/content",
                _chunks(b"wrong"),
                maximum_size_bytes=100,
                expected_sha256="0" * 64,
            )
        assert list((root / ".incoming").iterdir()) == []

        with pytest.raises(ArtifactTooLargeError):
            await storage.put(
                "objects/large/content",
                _chunks(b"123", b"456"),
                maximum_size_bytes=5,
            )
        assert list((root / ".incoming").iterdir()) == []

    asyncio.run(scenario())


def test_local_artifact_storage_validates_chunks_contract_and_overwrites_conflicts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "artifacts"
        storage = LocalArtifactStorage(root)

        first = await storage.put(
            "objects/conflict/content",
            _chunks(b"first"),
            maximum_size_bytes=100,
        )
        second = await storage.put(
            first.key,
            _chunks(b"replacement"),
            maximum_size_bytes=100,
        )
        assert second != first
        assert await _read_all(await storage.get(first.key)) == b"replacement"

        async def bad_chunks() -> AsyncIterable[bytes]:
            yield cast(bytes, "not-bytes")

        with pytest.raises(InvalidArtifactError, match="chunks must be bytes"):
            await storage.put(
                "objects/bad/content",
                bad_chunks(),
                maximum_size_bytes=100,
            )
        with pytest.raises(ArtifactTooLargeError, match="declared size"):
            await storage.put(
                "objects/declared/content",
                _chunks(b"123", b"4"),
                maximum_size_bytes=100,
                expected_size_bytes=3,
            )
        with pytest.raises(InvalidArtifactError, match="length does not match"):
            await storage.put(
                "objects/short/content",
                _chunks(b"12"),
                maximum_size_bytes=100,
                expected_size_bytes=3,
            )
        assert list((root / ".incoming").iterdir()) == []

    asyncio.run(scenario())


def test_local_artifact_storage_reference_and_delete_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = tmp_path / "artifacts"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        storage = LocalArtifactStorage(root)

        with pytest.raises(InvalidArtifactError, match="escaped"):
            await storage.delete("escape/content")

        (root / "objects" / "directory").mkdir(parents=True)
        with pytest.raises(ArtifactNotFoundError):
            storage.local_path("objects/directory")

        (root / "objects" / "relative").mkdir(parents=True)
        assert storage.key_from_reference("objects/relative/content") == "objects/relative/content"
        assert storage.key_from_reference(str(root / "objects" / "relative" / "..")) is None

        original_resolve = Path.resolve

        def failing_resolve(path: Path, strict: bool = False) -> Path:
            if path == root / "unreadable":
                raise OSError("unreadable parent")
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", failing_resolve)
        assert storage.key_from_reference(str(root / "unreadable" / "content")) is None

    asyncio.run(scenario())


class _S3NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("missing object")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def upload_fileobj(
        self,
        file_object: Any,
        bucket: str,
        key: str,
        ExtraArgs: Mapping[str, object] | None = None,
    ) -> None:
        metadata: dict[str, str] = {}
        if ExtraArgs is not None:
            supplied = ExtraArgs.get("Metadata")
            if isinstance(supplied, Mapping):
                metadata = {str(name): str(value) for name, value in supplied.items()}
        self.objects[(bucket, key)] = (file_object.read(), metadata)

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        item = self._item(kwargs)
        return {"Body": io.BytesIO(item[0])}

    def head_object(self, **kwargs: object) -> Mapping[str, Any]:
        content, metadata = self._item(kwargs)
        return {"ContentLength": len(content), "Metadata": metadata}

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]:
        self.objects.pop(self._key(kwargs), None)
        return {}

    def _item(self, kwargs: Mapping[str, object]) -> tuple[bytes, dict[str, str]]:
        try:
            return self.objects[self._key(kwargs)]
        except KeyError as exc:
            raise _S3NotFound from exc

    @staticmethod
    def _key(kwargs: Mapping[str, object]) -> tuple[str, str]:
        return str(kwargs["Bucket"]), str(kwargs["Key"])


class _S3ResponseError(Exception):
    def __init__(self, response: object) -> None:
        super().__init__("object store failed")
        self.response = response


class _ScriptedBody:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.closed = False

    def read(self, _amount: int = -1) -> Any:
        return self.values.pop(0) if self.values else b""

    def close(self) -> None:
        self.closed = True


class _MalformedS3Client(_MemoryS3Client):
    def __init__(self, *, body: object = None, get_error: Exception | None = None) -> None:
        super().__init__()
        self.body = body
        self.get_error = get_error

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        if self.get_error is not None:
            raise self.get_error
        return {} if self.body is None else {"Body": self.body}


class _FailingHeadS3Client(_MemoryS3Client):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def head_object(self, **kwargs: object) -> Mapping[str, Any]:
        raise self.error


def test_s3_compatible_storage_round_trips_without_exposing_credentials() -> None:
    async def scenario() -> None:
        client = _MemoryS3Client()
        storage = S3CompatibleArtifactStorage(
            bucket="phase8-bucket",
            prefix="tenant/artifacts",
            endpoint_url="https://object-store.invalid",
            client=client,
        )
        content = b"firmware-image"
        digest = hashlib.sha256(content).hexdigest()

        stored = await storage.put(
            "objects/firmware/content",
            _chunks(b"firmware-", b"image"),
            maximum_size_bytes=1024,
            expected_size_bytes=len(content),
            expected_sha256=digest,
        )

        assert client.objects[
            (
                "phase8-bucket",
                "tenant/artifacts/objects/firmware/content",
            )
        ] == (content, {"sha256": digest})
        assert await storage.stat(stored.key) == stored
        assert await storage.exists(stored.key) is True
        assert await _read_all(await storage.get(stored.key)) == content
        reference = storage.reference(stored.key)
        assert reference == ("s3://phase8-bucket/tenant/artifacts/objects/firmware/content")
        assert "object-store" not in reference
        assert storage.key_from_reference(reference) == stored.key
        assert storage.key_from_reference("s3://another-bucket/object") is None

        await storage.delete(stored.key)
        assert await storage.exists(stored.key) is False
        with pytest.raises(ArtifactNotFoundError):
            await storage.get(stored.key)

    asyncio.run(scenario())


def test_s3_storage_validates_configuration_and_upload_contract() -> None:
    with pytest.raises(ValueError, match="bucket"):
        S3CompatibleArtifactStorage(bucket=" ")
    client = _MemoryS3Client()
    with pytest.raises(ValueError, match="either"):
        S3CompatibleArtifactStorage(
            bucket="bucket",
            client=client,
            client_factory=lambda: client,
        )
    with pytest.raises(InvalidArtifactError):
        S3CompatibleArtifactStorage(bucket="bucket", prefix="../escape", client=client)

    async def scenario() -> None:
        storage = S3CompatibleArtifactStorage(bucket=" bucket ", prefix="", client=client)

        async def bad_chunks() -> AsyncIterable[bytes]:
            yield cast(bytes, "not-bytes")

        with pytest.raises(InvalidArtifactError, match="chunks must be bytes"):
            await storage.put("bad", bad_chunks(), maximum_size_bytes=10)
        with pytest.raises(ArtifactTooLargeError, match="upload limit"):
            await storage.put("large", _chunks(b"", b"1234"), maximum_size_bytes=3)
        with pytest.raises(ArtifactTooLargeError, match="declared size"):
            await storage.put(
                "declared",
                _chunks(b"123"),
                maximum_size_bytes=10,
                expected_size_bytes=2,
            )
        with pytest.raises(InvalidArtifactError, match="length does not match"):
            await storage.put(
                "short",
                _chunks(b"1"),
                maximum_size_bytes=10,
                expected_size_bytes=2,
            )
        assert storage.reference("object") == "s3://bucket/object"

    asyncio.run(scenario())


def test_s3_storage_handles_malformed_responses_and_non_not_found_errors() -> None:
    async def scenario() -> None:
        missing_body = S3CompatibleArtifactStorage(
            bucket="bucket",
            client=_MalformedS3Client(),
        )
        with pytest.raises(RuntimeError, match="readable body"):
            await missing_body.get("object")

        body = _ScriptedBody(["not-bytes"])
        malformed_body = S3CompatibleArtifactStorage(
            bucket="bucket",
            client=_MalformedS3Client(body=body),
        )
        with pytest.raises(RuntimeError, match="non-byte"):
            await _read_all(await malformed_body.get("object"))
        assert body.closed is True

        get_failure = S3CompatibleArtifactStorage(
            bucket="bucket",
            client=_MalformedS3Client(get_error=RuntimeError("network down")),
        )
        with pytest.raises(RuntimeError, match="network down"):
            await get_failure.get("object")

        head_failure = S3CompatibleArtifactStorage(
            bucket="bucket",
            client=_FailingHeadS3Client(_S3ResponseError({})),
        )
        with pytest.raises(_S3ResponseError):
            await head_failure.stat("object")

        malformed_error = S3CompatibleArtifactStorage(
            bucket="bucket",
            client=_FailingHeadS3Client(_S3ResponseError({"Error": "bad"})),
        )
        with pytest.raises(_S3ResponseError):
            await malformed_error.stat("object")

    asyncio.run(scenario())


def test_s3_storage_streams_stat_fallback_and_rejects_foreign_references() -> None:
    async def scenario() -> None:
        client = _MemoryS3Client()
        client.objects[("bucket", "prefix/object")] = (b"content", {})
        storage = S3CompatibleArtifactStorage(
            bucket="bucket",
            prefix="prefix",
            client=client,
        )

        assert await storage.stat("object") == StoredArtifact(
            key="object",
            size_bytes=7,
            sha256=hashlib.sha256(b"content").hexdigest(),
        )
        assert storage.key_from_reference("s3://bucket/wrong/object") is None
        assert storage.key_from_reference("s3://bucket/prefix/../escape") is None

    asyncio.run(scenario())


def test_s3_storage_lazily_constructs_and_caches_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        factory_client = _MemoryS3Client()
        factory_calls = 0

        def factory() -> _MemoryS3Client:
            nonlocal factory_calls
            factory_calls += 1
            return factory_client

        factory_storage = S3CompatibleArtifactStorage(
            bucket="bucket",
            client_factory=factory,
        )
        assert await factory_storage.exists("missing") is False
        assert await factory_storage.exists("still-missing") is False
        assert factory_calls == 1

        imported_client = _MemoryS3Client()
        client_calls: list[tuple[str, str | None, str | None]] = []

        def boto_client(
            service: str,
            *,
            endpoint_url: str | None,
            region_name: str | None,
        ) -> _MemoryS3Client:
            client_calls.append((service, endpoint_url, region_name))
            return imported_client

        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name: SimpleNamespace(client=boto_client) if name == "boto3" else None,
        )
        imported_storage = S3CompatibleArtifactStorage(
            bucket="bucket",
            endpoint_url="https://s3.invalid",
            region_name="test-1",
        )
        assert await imported_storage.exists("missing") is False
        assert client_calls == [("s3", "https://s3.invalid", "test-1")]

        def missing_import(_name: str) -> object:
            raise ImportError("boto3 unavailable")

        monkeypatch.setattr(
            importlib,
            "import_module",
            missing_import,
        )
        unavailable = S3CompatibleArtifactStorage(bucket="bucket")
        with pytest.raises(RuntimeError, match="requires an injected client"):
            await unavailable.exists("missing")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    (
        ({"maximum_size_bytes": 0}, ValueError, "positive"),
        (
            {"maximum_size_bytes": 10, "expected_size_bytes": -1},
            ValueError,
            "cannot be negative",
        ),
        (
            {"maximum_size_bytes": 2, "expected_size_bytes": 3},
            ArtifactTooLargeError,
            "upload limit",
        ),
        (
            {"maximum_size_bytes": 10, "expected_sha256": "A" * 64},
            InvalidArtifactError,
            "64 hexadecimal",
        ),
    ),
)
def test_storage_write_contract_rejects_invalid_metadata(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        with pytest.raises(error_type, match=message):
            await storage.put("object", _chunks(b"x"), **kwargs)  # type: ignore[arg-type]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "key",
    (
        "",
        ".",
        "/absolute",
        "../escape",
        "objects/../escape",
        "objects\\escape",
        "bad\x00key",
    ),
)
def test_storage_keys_reject_unsafe_paths(key: str) -> None:
    with pytest.raises(InvalidArtifactError):
        normalize_storage_key(key)
