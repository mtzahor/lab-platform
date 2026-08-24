from __future__ import annotations

import re
import unicodedata
from collections.abc import AsyncIterable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from lab_platform.core.artifact_storage import (
    ArtifactStorage,
    LocalArtifactStorage,
    LocalPathArtifactStorage,
)
from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    InvalidArtifactError,
)
from lab_platform.core.retention import RetentionPolicy, retention_class_for_artifact_type
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


class ArtifactListingRepository(Protocol):
    async def list_all(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 500,
    ) -> list[ArtifactRecord]: ...


class ArtifactEventRepository(Protocol):
    async def create(self, event: EventRecord) -> EventRecord: ...


class ArtifactService:
    """Stream immutable artifact content into generated, platform-owned paths."""

    def __init__(
        self,
        repository: GenericArtifactRepository,
        storage_root: Path | None = None,
        *,
        maximum_size_bytes: int,
        events: ArtifactEventRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention_seconds: int | None = None,
        retention_policy: RetentionPolicy | None = None,
        storage: ArtifactStorage | None = None,
    ) -> None:
        if maximum_size_bytes <= 0:
            raise ValueError("maximum artifact size must be positive")
        if retention_seconds is not None and retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        if retention_seconds is not None and retention_policy is not None:
            raise ValueError("Configure either retention seconds or a retention policy, not both")
        if storage is None:
            if storage_root is None:
                raise ValueError("Artifact storage or a local storage root is required")
            storage = LocalArtifactStorage(storage_root)
        elif storage_root is not None:
            raise ValueError("Configure either artifact storage or a local storage root, not both")
        self._repository = repository
        self._storage = storage
        self._maximum_size_bytes = maximum_size_bytes
        self._events = events
        self._clock = clock
        self._retention_seconds = retention_seconds
        self._retention_policy = retention_policy

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
        storage_key = self._storage_key(artifact_id)
        stored = None
        try:
            stored = await self._storage.put(
                storage_key,
                chunks,
                maximum_size_bytes=self._maximum_size_bytes,
                expected_sha256=expected_sha256,
            )
            if stored.size_bytes == 0:
                raise InvalidArtifactError("Artifact content cannot be empty.")
            created_at = self._clock()
            effective_expiry = expires_at
            if effective_expiry is None and self._retention_seconds is not None:
                effective_expiry = created_at + timedelta(seconds=self._retention_seconds)
            if effective_expiry is None and self._retention_policy is not None:
                failed_workflow = str((metadata or {}).get("workflow_status", "")).casefold() in {
                    "failed",
                    "error",
                }
                effective_expiry = self._retention_policy.expires_at(
                    created_at,
                    retention_class_for_artifact_type(artifact_type),
                    failed_workflow=failed_workflow,
                )
            record = ArtifactRecord(
                id=artifact_id,
                organisation_id=organisation_id or LEGACY_ORGANISATION_ID,
                owner_type=owner_type,
                owner_id=owner_id,
                name=safe_name,
                artifact_type=artifact_type,
                content_type=content_type,
                path=self._storage.reference(storage_key),
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                created_at=created_at,
                expires_at=effective_expiry,
                metadata=dict(metadata or {}),
            )
            saved = await self._repository.save(record, idempotency_key=idempotency_key)
        except BaseException:
            if stored is not None:
                with suppress(Exception):
                    await self._storage.delete(storage_key)
            raise
        if saved.id != record.id:
            await self._storage.delete(storage_key)
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

    async def list_all(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 500,
    ) -> list[ArtifactRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        repository = cast(ArtifactListingRepository, self._repository)
        return await repository.list_all(organisation_id=organisation_id, limit=limit)

    async def content_path(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> Path:
        record = await self.get(artifact_id, organisation_id=organisation_id)
        key = self._managed_key(record)
        if key is None or not isinstance(self._storage, LocalPathArtifactStorage):
            raise ArtifactNotFoundError(
                "Artifact content is unavailable.", artifact_id=str(artifact_id)
            )
        path = self._storage.local_path(key)
        await self._emit("ARTIFACT_DOWNLOADED", record)
        return path

    async def content_stream(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AsyncIterable[bytes]:
        """Return backend-neutral content for HTTP streaming and backup tooling."""

        record = await self.get(artifact_id, organisation_id=organisation_id)
        key = self._managed_key(record)
        if key is None:
            raise ArtifactNotFoundError(
                "Artifact content is unavailable.", artifact_id=str(artifact_id)
            )
        stored = await self._storage.stat(key)
        if stored.size_bytes != record.size_bytes or not _constant_time_equal(
            stored.sha256, record.sha256
        ):
            raise ArtifactChecksumMismatchError(
                "Stored artifact content does not match its metadata.",
                artifact_id=str(record.id),
                expected_sha256=record.sha256,
                actual_sha256=stored.sha256,
            )
        stream = await self._storage.get(key)
        await self._emit("ARTIFACT_DOWNLOADED", record)
        return stream

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
        await self._remove_managed_content(record)
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
            await self._remove_managed_content(record)
            if not await repository.delete(record.id):
                continue
            expired += 1
            await self._emit("ARTIFACT_EXPIRED", record)
            await self._emit("RETENTION_DELETION", record)
        return expired

    async def _remove_managed_content(self, record: ArtifactRecord) -> None:
        key = self._managed_key(record)
        if key is not None:
            await self._storage.delete(key)

    @staticmethod
    def _storage_key(artifact_id: UUID) -> str:
        return f"objects/{artifact_id}/content"

    def _managed_key(self, record: ArtifactRecord) -> str | None:
        key = self._storage.key_from_reference(record.path)
        return key if key == self._storage_key(record.id) else None

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
