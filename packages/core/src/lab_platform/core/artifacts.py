from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import AsyncIterable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidArtifactError,
)
from lab_platform.models import (
    LEGACY_ORGANISATION_ID,
    ArtifactOwnerType,
    ArtifactRecord,
    EventRecord,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GenericArtifactRepository(Protocol):
    async def save(
        self,
        record: ArtifactRecord,
        *,
        idempotency_key: str | None = None,
    ) -> ArtifactRecord: ...

    async def get(self, artifact_id: UUID) -> ArtifactRecord | None: ...

    async def get_by_idempotency_key(
        self, owner_id: UUID, idempotency_key: str
    ) -> ArtifactRecord | None: ...

    async def list_for_owner(
        self, owner_type: ArtifactOwnerType, owner_id: UUID
    ) -> list[ArtifactRecord]: ...


class ArtifactRetentionRepository(Protocol):
    async def list_expired(
        self, *, expires_at_or_before: datetime, limit: int
    ) -> list[ArtifactRecord]: ...

    async def delete(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> bool: ...


class ArtifactEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...


class ArtifactService:
    """Stream immutable artifact content into generated, platform-owned paths."""

    def __init__(
        self,
        repository: GenericArtifactRepository,
        storage_root: Path,
        *,
        maximum_size_bytes: int,
        events: ArtifactEventRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention_seconds: int | None = None,
    ) -> None:
        if maximum_size_bytes <= 0:
            raise ValueError("maximum artifact size must be positive")
        if retention_seconds is not None and retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._repository = repository
        self._storage_root = storage_root
        self._maximum_size_bytes = maximum_size_bytes
        self._events = events
        self._clock = clock
        self._retention_seconds = retention_seconds

    async def upload(
        self,
        chunks: AsyncIterable[bytes],
        *,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        name: str,
        artifact_type: str,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        if idempotency_key:
            existing = (
                await self._repository.get_by_idempotency_key(owner_id, idempotency_key)
                if organisation_id is None
                else await self._repository.get_by_idempotency_key(  # type: ignore[call-arg]
                    owner_id,
                    idempotency_key,
                    organisation_id=organisation_id,
                )
            )
            if existing is not None:
                return existing
        safe_name = normalize_artifact_name(name)
        if not artifact_type.strip():
            raise InvalidArtifactError("Artifact type cannot be empty.")
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.casefold()
            if not _SHA256.fullmatch(expected_sha256):
                raise InvalidArtifactError("Expected SHA-256 must contain 64 hexadecimal digits.")

        artifact_id = uuid4()
        incoming = self._storage_root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        temporary = incoming / str(artifact_id)
        digest = hashlib.sha256()
        size = 0
        directory: Path | None = None
        destination: Path | None = None
        try:
            with temporary.open("xb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise InvalidArtifactError("Artifact chunks must be bytes.")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self._maximum_size_bytes:
                        raise ArtifactTooLargeError(
                            "Artifact exceeds the configured upload limit.",
                            maximum_size_bytes=self._maximum_size_bytes,
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            if size == 0:
                raise InvalidArtifactError("Artifact content cannot be empty.")
            checksum = digest.hexdigest()
            if expected_sha256 is not None and not _constant_time_equal(checksum, expected_sha256):
                raise ArtifactChecksumMismatchError(
                    "Artifact checksum does not match the supplied SHA-256.",
                    expected_sha256=expected_sha256,
                    actual_sha256=checksum,
                )
            directory = self._storage_root / "objects" / str(artifact_id)
            directory.mkdir(parents=True, exist_ok=False)
            destination = directory / "content"
            temporary.replace(destination)
            created_at = self._clock()
            effective_expiry = expires_at
            if effective_expiry is None and self._retention_seconds is not None:
                effective_expiry = created_at + timedelta(seconds=self._retention_seconds)
            record = ArtifactRecord(
                id=artifact_id,
                organisation_id=organisation_id or LEGACY_ORGANISATION_ID,
                owner_type=owner_type,
                owner_id=owner_id,
                name=safe_name,
                artifact_type=artifact_type,
                content_type=content_type,
                path=str(destination),
                size_bytes=size,
                sha256=checksum,
                created_at=created_at,
                expires_at=effective_expiry,
                metadata=dict(metadata or {}),
            )
            saved = await self._repository.save(record, idempotency_key=idempotency_key)
        except BaseException:
            temporary.unlink(missing_ok=True)
            if destination is not None:
                destination.unlink(missing_ok=True)
            if directory is not None:
                with suppress(OSError):
                    directory.rmdir()
            raise
        if saved.id != record.id:
            destination.unlink(missing_ok=True)
            directory.rmdir()
        await self._emit("ARTIFACT_UPLOADED", saved)
        return saved

    async def store_bytes(
        self,
        content: bytes,
        *,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        name: str,
        artifact_type: str,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        async def chunks() -> AsyncIterable[bytes]:
            yield content

        return await self.upload(
            chunks(),
            owner_type=owner_type,
            owner_id=owner_id,
            name=name,
            artifact_type=artifact_type,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expires_at=expires_at,
            metadata=metadata,
            idempotency_key=idempotency_key,
            organisation_id=organisation_id,
        )

    async def get(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        record = (
            await self._repository.get(artifact_id)
            if organisation_id is None
            else await self._repository.get(  # type: ignore[call-arg]
                artifact_id,
                organisation_id=organisation_id,
            )
        )
        if record is None:
            raise ArtifactNotFoundError(
                f"Artifact {artifact_id} does not exist.", artifact_id=str(artifact_id)
            )
        return record

    async def get_by_idempotency_key(
        self,
        owner_id: UUID,
        idempotency_key: str,
        *,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord | None:
        if organisation_id is None:
            return await self._repository.get_by_idempotency_key(owner_id, idempotency_key)
        return await self._repository.get_by_idempotency_key(  # type: ignore[call-arg]
            owner_id,
            idempotency_key,
            organisation_id=organisation_id,
        )

    async def list_for_owner(
        self,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> list[ArtifactRecord]:
        if organisation_id is None:
            return await self._repository.list_for_owner(owner_type, owner_id)
        return await self._repository.list_for_owner(  # type: ignore[call-arg]
            owner_type,
            owner_id,
            organisation_id=organisation_id,
        )

    async def content_path(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> Path:
        record = await self.get(artifact_id, organisation_id=organisation_id)
        path = Path(record.path).resolve()
        root = self._storage_root.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ArtifactNotFoundError(
                "Artifact content is unavailable.", artifact_id=str(artifact_id)
            )
        await self._emit("ARTIFACT_DOWNLOADED", record)
        return path

    async def delete(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        """Delete one platform-managed artifact within its tenant boundary.

        Filesystem cleanup follows the same generated-path validation and ordering
        used by retention expiry. Legacy callers may omit ``organisation_id``;
        Phase 6 callers must pass their authenticated tenant.
        """

        record = await self.get(artifact_id, organisation_id=organisation_id)
        self._remove_managed_content(record)
        repository = cast(ArtifactRetentionRepository, self._repository)
        deleted = await repository.delete(
            artifact_id,
            organisation_id=organisation_id,
        )
        if not deleted:
            raise ArtifactNotFoundError(
                f"Artifact {artifact_id} does not exist.",
                artifact_id=str(artifact_id),
            )
        await self._emit("ARTIFACT_DELETED", record)
        return record

    async def expire_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """Remove expired platform-managed content and its metadata.

        Metadata with a tampered or legacy path outside the generated object layout is
        still expired, but that path is never unlinked. A metadata delete is the
        idempotency boundary: only the worker that deletes the row emits the event.
        """

        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff = now or self._clock()
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("artifact expiry time must be timezone-aware")
        cutoff = cutoff.astimezone(UTC)
        repository = cast(ArtifactRetentionRepository, self._repository)
        records = await repository.list_expired(
            expires_at_or_before=cutoff,
            limit=limit,
        )
        expired = 0
        for record in records:
            self._remove_managed_content(record)
            if not await repository.delete(record.id):
                continue
            expired += 1
            await self._emit("ARTIFACT_EXPIRED", record)
        return expired

    def _remove_managed_content(self, record: ArtifactRecord) -> None:
        root = self._storage_root.resolve()
        expected_directory = root / "objects" / str(record.id)
        candidate = Path(record.path)
        try:
            candidate_directory = candidate.parent.resolve()
        except OSError:
            return
        if (
            candidate.name != "content"
            or candidate_directory != expected_directory
            or not candidate_directory.is_relative_to(root)
        ):
            return
        managed_candidate = candidate_directory / "content"
        managed_candidate.unlink(missing_ok=True)
        with suppress(OSError):
            candidate_directory.rmdir()

    async def _emit(self, event_type: str, record: ArtifactRecord) -> None:
        if self._events is None:
            return
        await self._events.create(
            EventRecord(
                timestamp=self._clock(),
                type=event_type,
                source="artifacts",
                payload={
                    "artifact_id": str(record.id),
                    "owner_type": record.owner_type.value,
                    "owner_id": str(record.owner_id),
                    "artifact_type": record.artifact_type,
                    "size_bytes": record.size_bytes,
                    "sha256": record.sha256,
                },
            )
        )


def normalize_artifact_name(name: str) -> str:
    if "\x00" in name:
        raise InvalidArtifactError("Artifact name contains a NUL byte.")
    normalized = unicodedata.normalize("NFKC", name).replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].strip()
    if basename in {"", ".", ".."}:
        raise InvalidArtifactError("Artifact name is invalid.")
    return basename[:255]


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
