from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    InMemoryArtifactTransferRepository,
)
from lab_platform.models import RemoteArtifactMetadata


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
