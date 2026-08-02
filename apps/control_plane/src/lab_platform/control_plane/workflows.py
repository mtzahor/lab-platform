from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
)
from lab_platform.control_plane_core.workflows import WorkflowArtifactTransferDescriptor
from lab_platform.core.artifacts import ArtifactService
from lab_platform.models import ArtifactTransferDirection, RemoteCommand, RemoteCommandType

_DURABLE_DESCRIPTOR_KEYS = frozenset(
    {"input_name", "agent_id", "artifact_id", "sha256", "size_bytes", "target_path"}
)


class ControlPlaneWorkflowArtifactPort:
    """Stage workflow inputs durably and hydrate capabilities at command delivery time."""

    def __init__(
        self,
        artifacts: ArtifactService,
        transfers: DistributedArtifactService,
        store: FilesystemTransferStore,
        *,
        public_url: str,
        maximum_size_bytes: int,
    ) -> None:
        self._artifacts = artifacts
        self._transfers = transfers
        self._store = store
        self._public_url = public_url.rstrip("/")
        self._maximum_size = maximum_size_bytes

    async def issue_download(
        self,
        *,
        agent_id: UUID,
        input_name: str,
        artifact_id: UUID,
        target_path: str,
        idempotency_key: str,
    ) -> WorkflowArtifactTransferDescriptor:
        del idempotency_key
        record = await self._artifacts.get(artifact_id)
        await self._store.stage_verified_file(
            record.id,
            record.sha256,
            record.size_bytes,
            await self._artifacts.content_path(record.id),
            maximum_size_bytes=self._maximum_size,
        )
        return WorkflowArtifactTransferDescriptor(
            input_name=input_name,
            agent_id=agent_id,
            artifact_id=record.id,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            target_path=target_path,
        )

    async def hydrate_payload(self, command: RemoteCommand) -> Mapping[str, Any]:
        """Build one non-durable payload with fresh Agent-scoped download capabilities."""

        payload = dict(command.payload)
        if command.command_type is RemoteCommandType.RUN_WORKFLOW:
            raw_descriptors = payload.get("artifact_transfers", [])
            if not isinstance(raw_descriptors, list):
                raise ValueError("Durable workflow artifact descriptors must be a list")
            payload["artifact_transfers"] = [
                await self._hydrate_descriptor(command, raw) for raw in raw_descriptors
            ]
        elif command.command_type is RemoteCommandType.FLASH and "artifact" in payload:
            payload["artifact"] = await self._hydrate_descriptor(command, payload["artifact"])
        return payload

    async def _hydrate_descriptor(
        self,
        command: RemoteCommand,
        raw: object,
    ) -> dict[str, object]:
        descriptor = _durable_descriptor(raw)
        if descriptor.agent_id != command.agent_id:
            raise ValueError("Artifact descriptor is scoped to a different Agent")
        issued = await self._transfers.issue_download(
            agent_id=descriptor.agent_id,
            artifact_id=descriptor.artifact_id,
            sha256=descriptor.sha256,
            size_bytes=descriptor.size_bytes,
        )
        transfer = issued.transfer
        if (
            transfer.agent_id != descriptor.agent_id
            or transfer.artifact_id != descriptor.artifact_id
            or transfer.direction is not ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT
            or transfer.expected_sha256 != descriptor.sha256
            or transfer.expected_size_bytes != descriptor.size_bytes
        ):
            raise RuntimeError("Issued artifact capability does not match its durable descriptor")
        expires_at = min(transfer.expires_at, command.expires_at)
        return {
            **descriptor.as_payload(),
            "transfer_id": str(transfer.id),
            "download_url": (f"{self._public_url}/api/v1/artifact-transfers/{transfer.id}/content"),
            "transfer_token": issued.plaintext_token.get_secret_value(),
            "expires_at": expires_at.isoformat(),
        }


def _durable_descriptor(raw: object) -> WorkflowArtifactTransferDescriptor:
    if not isinstance(raw, Mapping):
        raise ValueError("Durable artifact descriptor must be an object")
    if set(raw) != _DURABLE_DESCRIPTOR_KEYS:
        raise ValueError("Durable artifact descriptor contains transient or unknown fields")
    try:
        input_name = raw["input_name"]
        agent_id = UUID(str(raw["agent_id"]))
        artifact_id = UUID(str(raw["artifact_id"]))
        sha256 = raw["sha256"]
        size_bytes = raw["size_bytes"]
        target_path = raw["target_path"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Durable artifact descriptor is invalid") from exc
    if not isinstance(input_name, str) or not isinstance(sha256, str):
        raise ValueError("Durable artifact descriptor text fields are invalid")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        raise ValueError("Durable artifact descriptor size is invalid")
    if not isinstance(target_path, str):
        raise ValueError("Durable artifact descriptor target path is invalid")
    return WorkflowArtifactTransferDescriptor(
        input_name=input_name,
        agent_id=agent_id,
        artifact_id=artifact_id,
        sha256=sha256,
        size_bytes=size_bytes,
        target_path=target_path,
    )


__all__ = ["ControlPlaneWorkflowArtifactPort"]
