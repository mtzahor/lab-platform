from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import os
import tempfile
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidArtifactError,
)

_READ_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Backend-neutral metadata for one immutable stored object."""

    key: str
    size_bytes: int
    sha256: str


class ArtifactStorage(Protocol):
    """Minimal immutable-object storage boundary used by control-plane artifacts.

    Keys are platform generated and backend relative. ``reference`` is the durable
    value persisted in the existing artifact ``path`` column; it must never contain
    credentials.
    """

    async def put(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        maximum_size_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact: ...

    async def get(self, key: str) -> AsyncIterable[bytes]: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def stat(self, key: str) -> StoredArtifact: ...

    def reference(self, key: str) -> str: ...

    def key_from_reference(self, reference: str) -> str | None: ...


@runtime_checkable
class LocalPathArtifactStorage(ArtifactStorage, Protocol):
    def local_path(self, key: str, *, require_exists: bool = True) -> Path: ...


class LocalArtifactStorage:
    """Atomic local-disk object storage with generated-key confinement."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def put(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        maximum_size_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact:
        _validate_write_contract(
            maximum_size_bytes=maximum_size_bytes,
            expected_size_bytes=expected_size_bytes,
            expected_sha256=expected_sha256,
        )
        normalized = normalize_storage_key(key)
        destination = self.local_path(normalized, require_exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=incoming)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise InvalidArtifactError("Artifact chunks must be bytes.")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > maximum_size_bytes:
                        raise ArtifactTooLargeError(
                            "Artifact exceeds the configured upload limit.",
                            maximum_size_bytes=maximum_size_bytes,
                        )
                    if expected_size_bytes is not None and size > expected_size_bytes:
                        raise ArtifactTooLargeError(
                            "Artifact upload exceeded its declared size.",
                            maximum_size_bytes=min(maximum_size_bytes, expected_size_bytes),
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            actual_sha256 = digest.hexdigest()
            _validate_stored_content(
                size=size,
                sha256=actual_sha256,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
            )
            if destination.exists():
                existing = await self.stat(normalized)
                if existing.size_bytes == size and hmac.compare_digest(
                    existing.sha256, actual_sha256
                ):
                    temporary.unlink(missing_ok=True)
                    return existing
            os.replace(temporary, destination)
            return StoredArtifact(normalized, size, actual_sha256)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def get(self, key: str) -> AsyncIterable[bytes]:
        path = self.local_path(key)

        async def chunks() -> AsyncIterable[bytes]:
            with path.open("rb") as stream:
                while True:
                    chunk = await asyncio.to_thread(stream.read, _READ_SIZE)
                    if not chunk:
                        return
                    yield chunk

        return chunks()

    async def delete(self, key: str) -> None:
        normalized = normalize_storage_key(key)
        candidate = self._lexical_path(normalized)
        parent = candidate.parent.resolve()
        if not parent.is_relative_to(self.root):
            raise InvalidArtifactError("Artifact storage key escaped its local root.")
        target = parent / candidate.name
        target.unlink(missing_ok=True)
        while parent != self.root and parent.name != ".incoming":
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    async def exists(self, key: str) -> bool:
        try:
            path = self.local_path(key)
        except ArtifactNotFoundError:
            return False
        return path.is_file()

    async def stat(self, key: str) -> StoredArtifact:
        normalized = normalize_storage_key(key)
        path = self.local_path(normalized)

        def inspect() -> tuple[int, str]:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(_READ_SIZE):
                    size += len(chunk)
                    digest.update(chunk)
            return size, digest.hexdigest()

        size, sha256 = await asyncio.to_thread(inspect)
        return StoredArtifact(normalized, size, sha256)

    def reference(self, key: str) -> str:
        return str(self.local_path(key, require_exists=False))

    def key_from_reference(self, reference: str) -> str | None:
        candidate = Path(reference)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            parent = candidate.parent.resolve()
        except OSError:
            return None
        lexical = parent / candidate.name
        if not lexical.is_relative_to(self.root):
            return None
        try:
            return normalize_storage_key(lexical.relative_to(self.root).as_posix())
        except (InvalidArtifactError, ValueError):
            return None

    def local_path(self, key: str, *, require_exists: bool = True) -> Path:
        normalized = normalize_storage_key(key)
        candidate = self._lexical_path(normalized)
        try:
            resolved = candidate.resolve(strict=require_exists)
        except (FileNotFoundError, OSError) as exc:
            raise ArtifactNotFoundError(
                "Artifact content is unavailable.", storage_key=normalized
            ) from exc
        if not resolved.is_relative_to(self.root) or (require_exists and not resolved.is_file()):
            raise ArtifactNotFoundError("Artifact content is unavailable.", storage_key=normalized)
        return resolved

    def _lexical_path(self, key: str) -> Path:
        return self.root.joinpath(*PurePosixPath(key).parts)


class S3Body(Protocol):
    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> object: ...


class S3Client(Protocol):
    def upload_fileobj(
        self,
        file_object: Any,
        bucket: str,
        key: str,
        ExtraArgs: Mapping[str, object] | None = None,
    ) -> object: ...

    def get_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: object) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]: ...


class S3CompatibleArtifactStorage:
    """S3-compatible storage without a mandatory SDK import.

    Production composition may inject any client implementing ``S3Client``. If no
    client or factory is supplied, boto3 is imported only when the first operation
    runs, so local/OSS installs do not need the optional dependency.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "lab-platform",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: S3Client | None = None,
        client_factory: Callable[[], S3Client] | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("S3 artifact bucket must not be empty")
        if client is not None and client_factory is not None:
            raise ValueError("Configure either an S3 client or client factory, not both")
        self.bucket = bucket.strip()
        self.prefix = prefix.strip().strip("/")
        if self.prefix:
            normalize_storage_key(self.prefix)
        self._endpoint_url = endpoint_url
        self._region_name = region_name
        self._client = client
        self._client_factory = client_factory

    async def put(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        maximum_size_bytes: int,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact:
        _validate_write_contract(
            maximum_size_bytes=maximum_size_bytes,
            expected_size_bytes=expected_size_bytes,
            expected_sha256=expected_sha256,
        )
        normalized = normalize_storage_key(key)
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile(mode="w+b") as stream:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise InvalidArtifactError("Artifact chunks must be bytes.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > maximum_size_bytes:
                    raise ArtifactTooLargeError(
                        "Artifact exceeds the configured upload limit.",
                        maximum_size_bytes=maximum_size_bytes,
                    )
                if expected_size_bytes is not None and size > expected_size_bytes:
                    raise ArtifactTooLargeError(
                        "Artifact upload exceeded its declared size.",
                        maximum_size_bytes=min(maximum_size_bytes, expected_size_bytes),
                    )
                digest.update(chunk)
                stream.write(chunk)
            sha256 = digest.hexdigest()
            _validate_stored_content(
                size=size,
                sha256=sha256,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
            )
            stream.seek(0)
            await asyncio.to_thread(
                self._require_client().upload_fileobj,
                stream,
                self.bucket,
                self._object_key(normalized),
                ExtraArgs={"Metadata": {"sha256": sha256}},
            )
        return StoredArtifact(normalized, size, sha256)

    async def get(self, key: str) -> AsyncIterable[bytes]:
        normalized = normalize_storage_key(key)
        try:
            response = await asyncio.to_thread(
                self._require_client().get_object,
                Bucket=self.bucket,
                Key=self._object_key(normalized),
            )
        except Exception as exc:
            if _is_s3_not_found(exc):
                raise ArtifactNotFoundError(
                    "Artifact content is unavailable.", storage_key=normalized
                ) from exc
            raise
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError("S3 get_object response did not include a readable body")

        async def chunks() -> AsyncIterable[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, _READ_SIZE)
                    if not chunk:
                        return
                    if not isinstance(chunk, bytes):
                        raise RuntimeError("S3 response body yielded non-byte content")
                    yield chunk
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    await asyncio.to_thread(close)

        return chunks()

    async def delete(self, key: str) -> None:
        normalized = normalize_storage_key(key)
        await asyncio.to_thread(
            self._require_client().delete_object,
            Bucket=self.bucket,
            Key=self._object_key(normalized),
        )

    async def exists(self, key: str) -> bool:
        try:
            await self.stat(key)
        except ArtifactNotFoundError:
            return False
        return True

    async def stat(self, key: str) -> StoredArtifact:
        normalized = normalize_storage_key(key)
        try:
            response = await asyncio.to_thread(
                self._require_client().head_object,
                Bucket=self.bucket,
                Key=self._object_key(normalized),
            )
        except Exception as exc:
            if _is_s3_not_found(exc):
                raise ArtifactNotFoundError(
                    "Artifact content is unavailable.", storage_key=normalized
                ) from exc
            raise
        size = response.get("ContentLength")
        metadata = response.get("Metadata", {})
        sha256 = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if not isinstance(size, int) or size < 0 or not isinstance(sha256, str):
            return await self._stat_by_streaming(normalized)
        _validate_sha256(sha256)
        return StoredArtifact(normalized, size, sha256)

    def reference(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._object_key(normalize_storage_key(key))}"

    def key_from_reference(self, reference: str) -> str | None:
        base = f"s3://{self.bucket}/"
        if not reference.startswith(base):
            return None
        object_key = reference.removeprefix(base)
        prefix = f"{self.prefix}/" if self.prefix else ""
        if prefix and not object_key.startswith(prefix):
            return None
        key = object_key.removeprefix(prefix)
        try:
            return normalize_storage_key(key)
        except InvalidArtifactError:
            return None

    async def _stat_by_streaming(self, key: str) -> StoredArtifact:
        stream = await self.get(key)
        digest = hashlib.sha256()
        size = 0
        async for chunk in stream:
            size += len(chunk)
            digest.update(chunk)
        return StoredArtifact(key, size, digest.hexdigest())

    def _object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _require_client(self) -> S3Client:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        try:
            boto3 = importlib.import_module("boto3")
        except ImportError as exc:
            raise RuntimeError(
                "S3 artifact storage requires an injected client or the optional boto3 package"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region_name,
        )
        return self._client


def normalize_storage_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key or "\\" in key:
        raise InvalidArtifactError("Artifact storage key is invalid.")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidArtifactError("Artifact storage key is invalid.")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise InvalidArtifactError("Artifact storage key is invalid.")
    return normalized


def _validate_write_contract(
    *,
    maximum_size_bytes: int,
    expected_size_bytes: int | None,
    expected_sha256: str | None,
) -> None:
    if maximum_size_bytes <= 0:
        raise ValueError("maximum artifact size must be positive")
    if expected_size_bytes is not None:
        if expected_size_bytes < 0:
            raise ValueError("expected artifact size cannot be negative")
        if expected_size_bytes > maximum_size_bytes:
            raise ArtifactTooLargeError(
                "Artifact exceeds the configured upload limit.",
                maximum_size_bytes=maximum_size_bytes,
            )
    if expected_sha256 is not None:
        _validate_sha256(expected_sha256)


def _validate_stored_content(
    *,
    size: int,
    sha256: str,
    expected_size_bytes: int | None,
    expected_sha256: str | None,
) -> None:
    if expected_size_bytes is not None and size != expected_size_bytes:
        raise InvalidArtifactError(
            "Artifact content length does not match metadata.",
            expected_size_bytes=expected_size_bytes,
            received_size_bytes=size,
        )
    if expected_sha256 is not None and not hmac.compare_digest(sha256, expected_sha256):
        raise ArtifactChecksumMismatchError(
            "Artifact checksum does not match the supplied SHA-256.",
            expected_sha256=expected_sha256,
            actual_sha256=sha256,
        )


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise InvalidArtifactError("Expected SHA-256 must contain 64 hexadecimal digits.")


def _is_s3_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return False
    return str(error.get("Code", "")).casefold() in {
        "404",
        "nosuchkey",
        "notfound",
        "no_such_key",
    }
