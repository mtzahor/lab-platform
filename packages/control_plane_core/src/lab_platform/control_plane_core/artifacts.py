from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import tempfile
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from lab_platform.control_plane_core.errors import (
    ArtifactTransferFailedError,
    ArtifactTransferTokenExpiredError,
)
from lab_platform.core.errors import ArtifactChecksumMismatchError, ArtifactTooLargeError
from lab_platform.models import (
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    RemoteArtifactMetadata,
)
from pydantic import SecretStr

_MAX_TOKEN_LENGTH = 512


@dataclass(frozen=True, slots=True)
class IssuedArtifactTransfer:
    transfer: ArtifactTransferRecord
    plaintext_token: SecretStr


class ArtifactTransferRepository(Protocol):
    async def create_transfer(self, transfer: ArtifactTransferRecord) -> ArtifactTransferRecord: ...

    async def get_transfer(self, transfer_id: UUID) -> ArtifactTransferRecord | None: ...

    async def get_transfer_by_token_hash(
        self,
        token_hash: str,
    ) -> ArtifactTransferRecord | None: ...

    async def update_transfer(
        self,
        transfer: ArtifactTransferRecord,
        *,
        expected_statuses: set[ArtifactTransferStatus],
    ) -> ArtifactTransferRecord | None: ...

    async def add_attempt(self, attempt: ArtifactTransferAttempt) -> ArtifactTransferAttempt: ...

    async def put_remote_artifact(
        self,
        artifact: RemoteArtifactMetadata,
    ) -> RemoteArtifactMetadata: ...

    async def get_remote_artifact(self, artifact_id: UUID) -> RemoteArtifactMetadata | None: ...


class InMemoryArtifactTransferRepository:
    def __init__(self) -> None:
        self._transfers: dict[UUID, ArtifactTransferRecord] = {}
        self._by_token_hash: dict[str, UUID] = {}
        self._attempts: dict[tuple[UUID, int], ArtifactTransferAttempt] = {}
        self._artifacts: dict[UUID, RemoteArtifactMetadata] = {}

    async def create_transfer(self, transfer: ArtifactTransferRecord) -> ArtifactTransferRecord:
        existing = self._transfers.get(transfer.id)
        if existing is not None:
            if existing != transfer:
                raise ArtifactTransferFailedError("Artifact transfer ID was reused.")
            return existing
        owner = self._by_token_hash.get(transfer.token_hash)
        if owner is not None:
            raise ArtifactTransferFailedError("Artifact transfer token collision detected.")
        self._transfers[transfer.id] = transfer
        self._by_token_hash[transfer.token_hash] = transfer.id
        return transfer

    async def get_transfer(self, transfer_id: UUID) -> ArtifactTransferRecord | None:
        return self._transfers.get(transfer_id)

    async def get_transfer_by_token_hash(
        self,
        token_hash: str,
    ) -> ArtifactTransferRecord | None:
        transfer_id = self._by_token_hash.get(token_hash)
        return self._transfers.get(transfer_id) if transfer_id is not None else None

    async def update_transfer(
        self,
        transfer: ArtifactTransferRecord,
        *,
        expected_statuses: set[ArtifactTransferStatus],
    ) -> ArtifactTransferRecord | None:
        current = self._transfers.get(transfer.id)
        if current is None or current.status not in expected_statuses:
            return None
        self._transfers[transfer.id] = transfer
        return transfer

    async def add_attempt(self, attempt: ArtifactTransferAttempt) -> ArtifactTransferAttempt:
        key = (attempt.transfer_id, attempt.attempt_number)
        existing = self._attempts.get(key)
        if existing is not None:
            if existing != attempt:
                raise ArtifactTransferFailedError("Artifact transfer attempt was reused.")
            return existing
        transfer = self._transfers.get(attempt.transfer_id)
        if transfer is None or transfer.attempt_count != attempt.attempt_number - 1:
            raise ArtifactTransferFailedError("Artifact transfer attempt sequence is invalid.")
        self._attempts[key] = attempt
        self._transfers[transfer.id] = _transfer_copy(
            transfer,
            attempt_count=attempt.attempt_number,
        )
        return attempt

    async def put_remote_artifact(
        self,
        artifact: RemoteArtifactMetadata,
    ) -> RemoteArtifactMetadata:
        for candidate in self._artifacts.values():
            if (
                candidate.agent_id == artifact.agent_id
                and candidate.local_artifact_id == artifact.local_artifact_id
            ):
                if _remote_artifact_fingerprint(candidate) != _remote_artifact_fingerprint(
                    artifact
                ):
                    raise ArtifactTransferFailedError("Remote artifact identity was reused.")
                if artifact.uploaded_at is not None and candidate.uploaded_at is None:
                    uploaded = artifact.model_copy(update={"id": candidate.id})
                    self._artifacts[candidate.id] = uploaded
                    return uploaded
                if (
                    artifact.uploaded_at is not None
                    and candidate.uploaded_at != artifact.uploaded_at
                ):
                    raise ArtifactTransferFailedError(
                        "Remote artifact upload timestamp cannot change."
                    )
                return candidate
        existing = self._artifacts.get(artifact.id)
        if existing is not None and (
            existing.agent_id != artifact.agent_id
            or existing.local_artifact_id != artifact.local_artifact_id
            or existing.sha256 != artifact.sha256
        ):
            raise ArtifactTransferFailedError("Remote artifact identity was reused.")
        self._artifacts[artifact.id] = artifact
        return artifact

    async def get_remote_artifact(self, artifact_id: UUID) -> RemoteArtifactMetadata | None:
        return self._artifacts.get(artifact_id)


class FilesystemTransferStore:
    """Immutable artifact content store addressed only by server-issued UUID and digest."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, artifact_id: UUID, sha256: str) -> Path:
        return self.root / sha256[:2] / str(artifact_id)

    async def stage_verified_file(
        self,
        artifact_id: UUID,
        expected_sha256: str,
        expected_size_bytes: int,
        source: Path,
        *,
        maximum_size_bytes: int,
    ) -> Path:
        resolved = source.resolve(strict=True)
        if not resolved.is_file():
            raise ArtifactTransferFailedError("Artifact source is not a regular file.")

        async def chunks() -> AsyncIterable[bytes]:
            with resolved.open("rb") as stream:
                while True:
                    chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        path, _, _ = await self.write_verified(
            artifact_id,
            expected_sha256,
            expected_size_bytes,
            chunks(),
            maximum_size_bytes=maximum_size_bytes,
        )
        return path

    async def write_verified(
        self,
        artifact_id: UUID,
        expected_sha256: str,
        expected_size_bytes: int,
        chunks: AsyncIterable[bytes],
        *,
        maximum_size_bytes: int,
    ) -> tuple[Path, int, str]:
        if expected_size_bytes > maximum_size_bytes:
            raise ArtifactTooLargeError(
                "Artifact exceeds the configured transfer limit.",
                maximum_size_bytes=maximum_size_bytes,
            )
        destination = self.path_for(artifact_id, expected_sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            size, existing_digest = _hash_file(destination)
            if size == expected_size_bytes and hmac.compare_digest(
                existing_digest, expected_sha256
            ):
                return destination, size, existing_digest

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact_id}.",
            suffix=".part",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        hasher = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ArtifactTransferFailedError("Artifact stream yielded non-byte data.")
                    received += len(chunk)
                    if received > maximum_size_bytes or received > expected_size_bytes:
                        raise ArtifactTooLargeError(
                            "Artifact upload exceeded its declared size.",
                            maximum_size_bytes=min(maximum_size_bytes, expected_size_bytes),
                        )
                    hasher.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            actual_sha256 = hasher.hexdigest()
            if received != expected_size_bytes:
                raise ArtifactTransferFailedError(
                    "Artifact content length does not match metadata.",
                    expected_size_bytes=expected_size_bytes,
                    received_size_bytes=received,
                )
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise ArtifactChecksumMismatchError(
                    "Artifact checksum verification failed.",
                    expected_sha256=expected_sha256,
                    actual_sha256=actual_sha256,
                )
            os.replace(temporary_path, destination)
            return destination, received, actual_sha256
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def open_verified(self, artifact_id: UUID, expected_sha256: str) -> Path:
        path = self.path_for(artifact_id, expected_sha256)
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactTransferFailedError("Artifact content is unavailable.") from exc
        if not resolved.is_relative_to(self.root):
            raise ArtifactTransferFailedError("Artifact content path escaped its storage root.")
        _, digest = _hash_file(resolved)
        if not hmac.compare_digest(digest, expected_sha256):
            raise ArtifactChecksumMismatchError("Stored artifact checksum verification failed.")
        return resolved


class DistributedArtifactService:
    """Issue scoped transfers and finalize checksum-verified Agent artifacts."""

    def __init__(
        self,
        repository: ArtifactTransferRepository,
        store: FilesystemTransferStore,
        *,
        maximum_upload_size_bytes: int = 500 * 1024 * 1024,
        token_ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = lambda: f"lpt_{secrets.token_urlsafe(32)}",
    ) -> None:
        if maximum_upload_size_bytes <= 0:
            raise ValueError("maximum_upload_size_bytes must be positive")
        if token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be positive")
        self._repository = repository
        self._store = store
        self._maximum_upload_size_bytes = maximum_upload_size_bytes
        self._token_ttl = timedelta(seconds=token_ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory

    async def register_remote_artifact(
        self,
        artifact: RemoteArtifactMetadata,
    ) -> RemoteArtifactMetadata:
        if artifact.size_bytes > self._maximum_upload_size_bytes:
            raise ArtifactTooLargeError(
                "Remote artifact exceeds the configured upload limit.",
                maximum_size_bytes=self._maximum_upload_size_bytes,
            )
        if artifact.uploaded_at is not None:
            raise ArtifactTransferFailedError("New remote artifact is already marked uploaded.")
        assigned = artifact.model_copy(update={"id": uuid4()})
        return await self._repository.put_remote_artifact(assigned)

    async def issue_upload(self, artifact_id: UUID) -> IssuedArtifactTransfer:
        artifact = await self._repository.get_remote_artifact(artifact_id)
        if artifact is None:
            raise ArtifactTransferFailedError("Remote artifact metadata does not exist.")
        return await self._issue(
            agent_id=artifact.agent_id,
            artifact_id=artifact.id,
            direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )

    async def issue_download(
        self,
        *,
        agent_id: UUID,
        artifact_id: UUID,
        sha256: str,
        size_bytes: int,
    ) -> IssuedArtifactTransfer:
        # Input content must have been staged into this service's immutable store.
        staged = self._store.open_verified(artifact_id, sha256)
        staged_size, _ = _hash_file(staged)
        if staged_size != size_bytes:
            raise ArtifactTransferFailedError(
                "Staged artifact size does not match its metadata.",
                expected_size_bytes=size_bytes,
                actual_size_bytes=staged_size,
            )
        return await self._issue(
            agent_id=agent_id,
            artifact_id=artifact_id,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            expected_sha256=sha256,
            expected_size_bytes=size_bytes,
        )

    async def authorize(
        self,
        transfer_id: UUID,
        plaintext_token: str,
        *,
        direction: ArtifactTransferDirection,
        agent_id: UUID | None = None,
    ) -> ArtifactTransferRecord:
        if (
            not plaintext_token.startswith("lpt_")
            or len(plaintext_token) < 47
            or len(plaintext_token) > _MAX_TOKEN_LENGTH
        ):
            raise ArtifactTransferFailedError("Artifact transfer authentication failed.")
        token_hash = _hash_token(plaintext_token)
        transfer = await self._repository.get_transfer_by_token_hash(token_hash)
        if (
            transfer is None
            or transfer.id != transfer_id
            or transfer.direction is not direction
            or agent_id is not None
            and transfer.agent_id != agent_id
        ):
            raise ArtifactTransferFailedError("Artifact transfer authentication failed.")
        now = _utc(self._clock())
        if transfer.expires_at <= now or transfer.status is ArtifactTransferStatus.EXPIRED:
            await self._expire(transfer, now)
            raise ArtifactTransferTokenExpiredError("Artifact transfer token has expired.")
        return transfer

    async def upload(
        self,
        transfer_id: UUID,
        plaintext_token: str,
        chunks: AsyncIterable[bytes],
        *,
        content_length: int | None,
        agent_id: UUID | None = None,
    ) -> RemoteArtifactMetadata:
        transfer = await self.authorize(
            transfer_id,
            plaintext_token,
            direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
            agent_id=agent_id,
        )
        artifact = await self._repository.get_remote_artifact(transfer.artifact_id)
        if artifact is None:
            raise ArtifactTransferFailedError("Remote artifact metadata does not exist.")
        if transfer.status is ArtifactTransferStatus.COMPLETED:
            if artifact.uploaded_at is None:
                raise ArtifactTransferFailedError("Completed transfer has incomplete metadata.")
            return artifact
        if content_length is not None and content_length != transfer.expected_size_bytes:
            raise ArtifactTransferFailedError(
                "Content-Length does not match artifact metadata.",
                expected_size_bytes=transfer.expected_size_bytes,
                content_length=content_length,
            )
        now = _utc(self._clock())
        attempt_number = transfer.attempt_count + 1
        active = _transfer_copy(
            transfer,
            status=ArtifactTransferStatus.IN_PROGRESS,
            error_code=None,
        )
        persisted = await self._repository.update_transfer(
            active,
            expected_statuses={
                ArtifactTransferStatus.PENDING,
                ArtifactTransferStatus.IN_PROGRESS,
                ArtifactTransferStatus.FAILED,
            },
        )
        transfer = persisted or await self._require_transfer(transfer.id)
        started = now
        try:
            _, received, digest = await self._store.write_verified(
                artifact.id,
                transfer.expected_sha256,
                transfer.expected_size_bytes,
                chunks,
                maximum_size_bytes=self._maximum_upload_size_bytes,
            )
        except Exception as exc:
            failed_at = _utc(self._clock())
            await self._repository.add_attempt(
                ArtifactTransferAttempt(
                    transfer_id=transfer.id,
                    attempt_number=attempt_number,
                    started_at=started,
                    completed_at=failed_at,
                    error_code=getattr(exc, "code", ArtifactTransferFailedError.code),
                    error_message=str(exc),
                )
            )
            transfer = await self._require_transfer(transfer.id)
            failed = _transfer_copy(
                transfer,
                status=ArtifactTransferStatus.FAILED,
                error_code=getattr(exc, "code", ArtifactTransferFailedError.code),
            )
            await self._repository.update_transfer(
                failed,
                expected_statuses={ArtifactTransferStatus.IN_PROGRESS},
            )
            raise
        completed_at = _utc(self._clock())
        await self._repository.add_attempt(
            ArtifactTransferAttempt(
                transfer_id=transfer.id,
                attempt_number=attempt_number,
                started_at=started,
                completed_at=completed_at,
                bytes_transferred=received,
                sha256=digest,
            )
        )
        transfer = await self._require_transfer(transfer.id)
        completed = _transfer_copy(
            transfer,
            status=ArtifactTransferStatus.COMPLETED,
            completed_at=completed_at,
        )
        await self._repository.update_transfer(
            completed,
            expected_statuses={ArtifactTransferStatus.IN_PROGRESS},
        )
        uploaded = RemoteArtifactMetadata.model_validate(
            {**artifact.model_dump(), "uploaded_at": completed_at}
        )
        return await self._repository.put_remote_artifact(uploaded)

    async def download_path(
        self,
        transfer_id: UUID,
        plaintext_token: str,
        *,
        agent_id: UUID | None = None,
    ) -> Path:
        transfer = await self.authorize(
            transfer_id,
            plaintext_token,
            direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            agent_id=agent_id,
        )
        path = self._store.open_verified(transfer.artifact_id, transfer.expected_sha256)
        if transfer.status is not ArtifactTransferStatus.COMPLETED:
            now = _utc(self._clock())
            attempt_number = transfer.attempt_count + 1
            active = _transfer_copy(
                transfer,
                status=ArtifactTransferStatus.IN_PROGRESS,
                error_code=None,
            )
            transfer = await self._repository.update_transfer(
                active,
                expected_statuses={
                    ArtifactTransferStatus.PENDING,
                    ArtifactTransferStatus.IN_PROGRESS,
                    ArtifactTransferStatus.FAILED,
                },
            ) or await self._require_transfer(transfer.id)
            await self._repository.add_attempt(
                ArtifactTransferAttempt(
                    transfer_id=transfer.id,
                    attempt_number=attempt_number,
                    started_at=now,
                    completed_at=now,
                    bytes_transferred=transfer.expected_size_bytes,
                    sha256=transfer.expected_sha256,
                )
            )
            transfer = await self._require_transfer(transfer.id)
            await self._repository.update_transfer(
                _transfer_copy(
                    transfer,
                    status=ArtifactTransferStatus.COMPLETED,
                    completed_at=now,
                ),
                expected_statuses={ArtifactTransferStatus.IN_PROGRESS},
            )
        return path

    async def _issue(
        self,
        *,
        agent_id: UUID,
        artifact_id: UUID,
        direction: ArtifactTransferDirection,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> IssuedArtifactTransfer:
        now = _utc(self._clock())
        plaintext = self._token_factory()
        if (
            not plaintext.startswith("lpt_")
            or len(plaintext) < 47
            or len(plaintext) > _MAX_TOKEN_LENGTH
        ):
            raise ValueError("Artifact transfer token generator returned insufficient entropy")
        transfer = ArtifactTransferRecord(
            id=uuid4(),
            agent_id=agent_id,
            artifact_id=artifact_id,
            direction=direction,
            token_hash=_hash_token(plaintext),
            created_at=now,
            expires_at=now + self._token_ttl,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        created = await self._repository.create_transfer(transfer)
        return IssuedArtifactTransfer(created, SecretStr(plaintext))

    async def _expire(
        self,
        transfer: ArtifactTransferRecord,
        now: datetime,
    ) -> None:
        if transfer.status in {
            ArtifactTransferStatus.COMPLETED,
            ArtifactTransferStatus.EXPIRED,
        }:
            return
        await self._repository.update_transfer(
            _transfer_copy(transfer, status=ArtifactTransferStatus.EXPIRED),
            expected_statuses={
                ArtifactTransferStatus.PENDING,
                ArtifactTransferStatus.IN_PROGRESS,
                ArtifactTransferStatus.FAILED,
            },
        )

    async def _require_transfer(self, transfer_id: UUID) -> ArtifactTransferRecord:
        transfer = await self._repository.get_transfer(transfer_id)
        if transfer is None:
            raise ArtifactTransferFailedError("Artifact transfer does not exist.")
        return transfer


def _transfer_copy(
    transfer: ArtifactTransferRecord,
    **updates: object,
) -> ArtifactTransferRecord:
    return ArtifactTransferRecord.model_validate({**transfer.model_dump(), **updates})


def _remote_artifact_fingerprint(
    artifact: RemoteArtifactMetadata,
) -> dict[str, object]:
    return artifact.model_dump(exclude={"id", "uploaded_at"})


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Artifact transfer timestamps must be timezone-aware")
    return value.astimezone(UTC)
