from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from lab_platform.control_plane.workflows import ControlPlaneWorkflowArtifactPort
from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    InMemoryArtifactTransferRepository,
)
from lab_platform.core.artifact_storage import S3CompatibleArtifactStorage
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.errors import ArtifactNotFoundError
from lab_platform.models import ArtifactOwnerType
from lab_platform.persistence import SQLiteDatabase, SQLiteGenericArtifactRepository


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
