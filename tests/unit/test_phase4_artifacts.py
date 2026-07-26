from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from lab_platform.core.artifacts import ArtifactService, normalize_artifact_name
from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidArtifactError,
)
from lab_platform.models import ArtifactOwnerType, ArtifactRecord


class Repository:
    def __init__(self) -> None:
        self.records: dict[UUID, ArtifactRecord] = {}
        self.keys: dict[tuple[UUID, str], UUID] = {}

    async def save(
        self, record: ArtifactRecord, *, idempotency_key: str | None = None
    ) -> ArtifactRecord:
        self.records[record.id] = record
        if idempotency_key:
            self.keys[(record.owner_id, idempotency_key)] = record.id
        return record

    async def get(self, artifact_id: UUID) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    async def get_by_idempotency_key(
        self, owner_id: UUID, idempotency_key: str
    ) -> ArtifactRecord | None:
        artifact_id = self.keys.get((owner_id, idempotency_key))
        return self.records.get(artifact_id) if artifact_id else None

    async def list_for_owner(
        self, owner_type: ArtifactOwnerType, owner_id: UUID
    ) -> list[ArtifactRecord]:
        return [
            item
            for item in self.records.values()
            if item.owner_type is owner_type and item.owner_id == owner_id
        ]


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def test_artifact_upload_is_streamed_hashed_and_uses_generated_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = Repository()
        service = ArtifactService(repository, tmp_path, maximum_size_bytes=20)
        owner_id = uuid4()
        content = b"firmware"
        record = await service.upload(
            _chunks(content[:3], content[3:]),
            owner_type=ArtifactOwnerType.CI_SESSION,
            owner_id=owner_id,
            name="../../build\\firmware.bin",
            artifact_type="firmware",
            content_type="application/octet-stream",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key="upload-1",
        )
        assert record.name == "firmware.bin"
        assert record.sha256 == hashlib.sha256(content).hexdigest()
        assert record.size_bytes == len(content)
        assert Path(record.path).is_relative_to(tmp_path)
        assert (await service.content_path(record.id)).read_bytes() == content
        assert await service.list_for_owner(ArtifactOwnerType.CI_SESSION, owner_id) == [record]

        replay = await service.upload(
            _chunks(b"different"),
            owner_type=ArtifactOwnerType.CI_SESSION,
            owner_id=owner_id,
            name="other.bin",
            artifact_type="firmware",
            idempotency_key="upload-1",
        )
        assert replay == record

    asyncio.run(scenario())


def test_artifact_rejects_oversize_empty_bad_chunks_and_checksum(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = ArtifactService(Repository(), tmp_path, maximum_size_bytes=3)
        owner_id = uuid4()
        with pytest.raises(ArtifactTooLargeError):
            await service.upload(
                _chunks(b"four"),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="firmware.bin",
                artifact_type="firmware",
            )
        with pytest.raises(InvalidArtifactError, match="empty"):
            await service.upload(
                _chunks(b""),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="firmware.bin",
                artifact_type="firmware",
            )

        async def bad_chunks() -> AsyncIterator[bytes]:
            yield cast(bytes, "not-bytes")

        with pytest.raises(InvalidArtifactError, match="chunks"):
            await service.upload(
                bad_chunks(),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="firmware.bin",
                artifact_type="firmware",
            )
        with pytest.raises(InvalidArtifactError, match="64"):
            await service.upload(
                _chunks(b"one"),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="firmware.bin",
                artifact_type="firmware",
                expected_sha256="bad",
            )
        with pytest.raises(ArtifactChecksumMismatchError):
            await service.upload(
                _chunks(b"one"),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=owner_id,
                name="firmware.bin",
                artifact_type="firmware",
                expected_sha256="0" * 64,
            )
        assert not any((tmp_path / ".incoming").iterdir())

    asyncio.run(scenario())


def test_cancelled_artifact_upload_removes_partial_incoming_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = Repository()
        service = ArtifactService(repository, tmp_path, maximum_size_bytes=1024)

        async def cancelled_chunks() -> AsyncIterator[bytes]:
            yield b"partial"
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await service.upload(
                cancelled_chunks(),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=uuid4(),
                name="firmware.bin",
                artifact_type="firmware",
            )

        assert list((tmp_path / ".incoming").iterdir()) == []
        assert repository.records == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["", ".", "..", "folder/", "bad\x00name"])
def test_artifact_name_rejects_empty_or_special_basename(name: str) -> None:
    with pytest.raises(InvalidArtifactError):
        normalize_artifact_name(name)


def test_artifact_missing_metadata_or_content_is_reported(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = Repository()
        service = ArtifactService(repository, tmp_path, maximum_size_bytes=10)
        with pytest.raises(ArtifactNotFoundError):
            await service.get(uuid4())
        record = await service.upload(
            _chunks(b"data"),
            owner_type=ArtifactOwnerType.CI_SESSION,
            owner_id=uuid4(),
            name="data.txt",
            artifact_type="log",
        )
        Path(record.path).unlink()
        with pytest.raises(ArtifactNotFoundError, match="unavailable"):
            await service.content_path(record.id)

    asyncio.run(scenario())


def test_artifact_service_validates_constructor_and_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        ArtifactService(Repository(), tmp_path, maximum_size_bytes=0)

    async def scenario() -> None:
        service = ArtifactService(Repository(), tmp_path, maximum_size_bytes=10)
        with pytest.raises(InvalidArtifactError, match="type"):
            await service.upload(
                _chunks(b"data"),
                owner_type=ArtifactOwnerType.CI_SESSION,
                owner_id=uuid4(),
                name="data",
                artifact_type=" ",
            )

    asyncio.run(scenario())
