from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.agent.distributed import (
    AgentArtifactCache,
    ArtifactTransferDescriptor,
)
from lab_platform.control_plane_core.errors import ArtifactTransferFailedError
from pydantic import SecretStr


class _Transport:
    def __init__(self, content: dict[UUID, bytes]) -> None:
        self.content = content
        self.downloads: list[UUID] = []

    def download(
        self,
        descriptor: ArtifactTransferDescriptor,
        destination: Path,
        *,
        maximum_size_bytes: int,
    ) -> tuple[int, str]:
        value = self.content[descriptor.artifact_id]
        assert len(value) <= maximum_size_bytes
        destination.write_bytes(value)
        self.downloads.append(descriptor.artifact_id)
        return len(value), hashlib.sha256(value).hexdigest()

    def upload(
        self,
        url: str,
        token: str,
        source: Path,
        *,
        expected_sha256: str,
        maximum_size_bytes: int,
    ) -> None:  # pragma: no cover - cache tests exercise downloads
        raise AssertionError((url, token, source, expected_sha256, maximum_size_bytes))


def _descriptor(
    artifact_id: UUID,
    content: bytes,
    *,
    url: str = "https://control.example/api/v1/artifact-transfers/id/content",
    expires_at: datetime | None = None,
) -> ArtifactTransferDescriptor:
    return ArtifactTransferDescriptor(
        artifact_id=artifact_id,
        agent_id=uuid4(),
        transfer_id=uuid4(),
        input_name="firmware",
        download_url=url,
        transfer_token=SecretStr("secret-transfer-token"),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        target_path=f"inputs/{artifact_id}",
    )


@pytest.mark.anyio
async def test_cache_verifies_once_and_reuses_immutable_content(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"firmware"
    transport = _Transport({artifact_id: content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "wss://control.example/api/v1/agent-gateway",
        maximum_size_bytes=100,
        transport=transport,
    )
    descriptor = _descriptor(artifact_id, content)

    first = await cache.fetch(descriptor)
    second = await cache.fetch(descriptor)

    assert first == second
    assert first.read_bytes() == content
    assert transport.downloads == [artifact_id]


@pytest.mark.anyio
async def test_cache_rejects_cross_origin_and_expired_capabilities(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"firmware"
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=100,
        transport=_Transport({artifact_id: content}),
    )

    with pytest.raises(ArtifactTransferFailedError, match="configured control plane"):
        await cache.fetch(_descriptor(artifact_id, content, url="https://attacker.invalid/file"))
    with pytest.raises(ArtifactTransferFailedError, match="expired"):
        await cache.fetch(
            _descriptor(
                artifact_id,
                content,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )


@pytest.mark.anyio
async def test_cache_evicts_lru_but_never_an_active_artifact(tmp_path: Path) -> None:
    first_id, second_id = uuid4(), uuid4()
    first_content, second_content = b"1111", b"2222"
    transport = _Transport({first_id: first_content, second_id: second_content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=6,
        transport=transport,
    )
    first = _descriptor(first_id, first_content)
    second = _descriptor(second_id, second_content)

    first_path = await cache.fetch(first, pin=True)
    with pytest.raises(ArtifactTransferFailedError, match="active operations"):
        await cache.fetch(second)
    assert first_path.is_file()

    cache.release(first.sha256)
    second_path = await cache.fetch(second)
    assert second_path.read_bytes() == second_content
    assert not first_path.exists()


@pytest.mark.anyio
async def test_cache_discards_corrupt_content_before_reusing_it(tmp_path: Path) -> None:
    artifact_id = uuid4()
    content = b"valid-content"
    transport = _Transport({artifact_id: content})
    cache = AgentArtifactCache(
        tmp_path / "cache",
        "https://control.example",
        maximum_size_bytes=100,
        transport=transport,
    )
    descriptor = _descriptor(artifact_id, content)
    path = await cache.fetch(descriptor)
    path.write_bytes(b"corrupt")

    restored = await cache.fetch(descriptor)

    assert restored.read_bytes() == content
    assert transport.downloads == [artifact_id, artifact_id]
