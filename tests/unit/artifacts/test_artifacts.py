from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane.workflows import ControlPlaneWorkflowArtifactPort
from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    InMemoryArtifactTransferRepository,
)
from lab_platform.core.artifact_storage import S3CompatibleArtifactStorage
from lab_platform.core.artifacts import ArtifactService, normalize_artifact_name
from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidArtifactError,
)
from lab_platform.models import ArtifactOwnerType, ArtifactRecord, RemoteArtifactMetadata
from lab_platform.persistence import SQLiteDatabase, SQLiteGenericArtifactRepository


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

    async def delete(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> bool:
        record = self.records.get(artifact_id)
        if record is None or (
            organisation_id is not None and record.organisation_id != organisation_id
        ):
            return False
        del self.records[artifact_id]
        return True


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


def test_artifact_delete_removes_managed_content_and_metadata(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = Repository()
        service = ArtifactService(repository, tmp_path, maximum_size_bytes=10)
        record = await service.upload(
            _chunks(b"data"),
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="data.txt",
            artifact_type="log",
        )
        path = Path(record.path)
        assert path.is_file()

        assert await service.delete(record.id) == record
        assert not path.exists()
        assert record.id not in repository.records
        with pytest.raises(ArtifactNotFoundError):
            await service.delete(record.id)

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
        raw_metadata = ExtraArgs.get("Metadata", {}) if ExtraArgs is not None else {}
        metadata = (
            {str(name): str(value) for name, value in raw_metadata.items()}
            if isinstance(raw_metadata, Mapping)
            else {}
        )
        content = file_object.read()
        assert isinstance(content, bytes)
        self.objects[(bucket, key)] = (content, metadata)

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        content, _ = self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]
        return {"Body": io.BytesIO(content)}

    def head_object(self, **kwargs: object) -> Mapping[str, Any]:
        content, metadata = self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]
        return {"ContentLength": len(content), "Metadata": metadata}

    def delete_object(self, **kwargs: object) -> Mapping[str, Any]:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)
        return {}


async def _collect(stream: AsyncIterable[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


def test_workflow_input_staging_and_transfer_download_support_s3_storage(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "metadata.db")
        database.initialize()
        try:
            client = _MemoryS3Client()
            platform_storage = S3CompatibleArtifactStorage(
                bucket="platform-artifacts",
                prefix="objects",
                client=client,
            )
            transfer_storage = S3CompatibleArtifactStorage(
                bucket="agent-transfers",
                prefix="objects",
                client=client,
            )
            platform = ArtifactService(
                SQLiteGenericArtifactRepository(database),
                maximum_size_bytes=1024,
                storage=platform_storage,
            )
            transfer_store = FilesystemTransferStore(storage=transfer_storage)
            transfers = DistributedArtifactService(
                InMemoryArtifactTransferRepository(),
                transfer_store,
                maximum_upload_size_bytes=1024,
                token_factory=lambda: "lpt_" + "s" * 48,
            )
            workflow_artifacts = ControlPlaneWorkflowArtifactPort(
                platform,
                transfers,
                transfer_store,
                public_url="https://control.example.test",
                maximum_size_bytes=1024,
            )
            content = b"firmware-from-object-storage"
            record = await platform.store_bytes(
                content,
                owner_type=ArtifactOwnerType.OPERATION,
                owner_id=uuid4(),
                name="firmware.bin",
                artifact_type="firmware",
            )

            assert transfer_store.root is None
            with pytest.raises(ArtifactNotFoundError, match="unavailable"):
                await platform.content_path(record.id)

            descriptor = await workflow_artifacts.issue_download(
                agent_id=uuid4(),
                input_name="firmware",
                artifact_id=record.id,
                target_path="artifacts/firmware.bin",
                idempotency_key="stage-once",
                allow_internal_authorisation=True,
            )
            staged = await transfer_store.open_verified_object(record.id, record.sha256)
            assert staged.size_bytes == len(content)
            assert (
                await _collect(await transfer_store.open_verified_stream(record.id, record.sha256))
                == content
            )

            issued = await transfers.issue_download(
                agent_id=descriptor.agent_id,
                artifact_id=descriptor.artifact_id,
                sha256=descriptor.sha256,
                size_bytes=descriptor.size_bytes,
            )
            downloaded = await transfers.download_stream(
                issued.transfer.id,
                issued.plaintext_token.get_secret_value(),
                agent_id=descriptor.agent_id,
            )
            assert await _collect(downloaded) == content
        finally:
            database.close()

    asyncio.run(scenario())


def test_control_plane_assigns_global_id_and_upload_completion_is_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        content = b"agent-produced-serial-log"
        digest = hashlib.sha256(content).hexdigest()
        agent_id = uuid4()
        local_id = uuid4()
        reported = RemoteArtifactMetadata(
            id=local_id,
            agent_id=agent_id,
            local_artifact_id=local_id,
            command_id=uuid4(),
            operation_id=uuid4(),
            name="serial.log",
            artifact_type="serial_log",
            content_type="text/plain",
            size_bytes=len(content),
            sha256=digest,
            created_at=datetime.now(UTC),
        )
        repository = InMemoryArtifactTransferRepository()
        service = DistributedArtifactService(
            repository,
            FilesystemTransferStore(tmp_path / "artifacts"),
            token_factory=lambda: "lpt_" + "x" * 48,
        )

        assigned = await service.register_remote_artifact(reported)
        replay = await service.register_remote_artifact(reported)

        assert assigned.id != local_id
        assert replay == assigned
        assert assigned.local_artifact_id == local_id

        issued = await service.issue_upload(assigned.id)

        async def chunks() -> AsyncIterator[bytes]:
            yield content[:7]
            yield content[7:]

        uploaded = await service.upload(
            issued.transfer.id,
            issued.plaintext_token.get_secret_value(),
            chunks(),
            content_length=len(content),
            agent_id=agent_id,
        )
        repeated = await service.upload(
            issued.transfer.id,
            issued.plaintext_token.get_secret_value(),
            chunks(),
            content_length=len(content),
            agent_id=agent_id,
        )

        assert uploaded.uploaded_at is not None
        assert repeated == uploaded

    asyncio.run(scenario())
