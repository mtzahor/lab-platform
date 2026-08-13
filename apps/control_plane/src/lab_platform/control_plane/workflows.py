from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from lab_platform.control_plane.artifact_access import ProtectedArtifactService
from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
)
from lab_platform.control_plane_core.workflows import WorkflowArtifactTransferDescriptor
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.errors import ArtifactNotFoundError, AuthenticationRequiredError
from lab_platform.models import (
    ArtifactRecord,
    ArtifactTransferDirection,
    AuthenticationContext,
    RemoteCommand,
    RemoteCommandType,
)

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
        self._protected_artifacts: ProtectedArtifactService | None = None

    def set_protected_artifact_service(
        self,
        artifacts: ProtectedArtifactService,
    ) -> None:
        self._protected_artifacts = artifacts

    async def require_access(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> None:
        await self._authorised_record(
            artifact_id,
            organisation_id=organisation_id,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )

    async def issue_download(
        self,
        *,
        agent_id: UUID,
        input_name: str,
        artifact_id: UUID,
        target_path: str,
        idempotency_key: str,
        organisation_id: UUID | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> WorkflowArtifactTransferDescriptor:
        del idempotency_key
        record = await self._authorised_record(
            artifact_id,
            organisation_id=organisation_id,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        await self._store.stage_verified_file(
            record.id,
            record.sha256,
            record.size_bytes,
            await self._artifacts.content_path(
                record.id,
                organisation_id=record.organisation_id,
            ),
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

    async def _authorised_record(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None,
        authentication_context: AuthenticationContext | None,
        allow_legacy_authorisation: bool,
        allow_internal_authorisation: bool,
    ) -> ArtifactRecord:
        protected = self._protected_artifacts
        if protected is not None:
            record = await protected.get(
                artifact_id,
                authentication_context=authentication_context,
                allow_legacy_authorisation=allow_legacy_authorisation,
                allow_internal_authorisation=allow_internal_authorisation,
                organisation_id=organisation_id,
            )
            if isinstance(record, ArtifactRecord):
                return record
            raise ArtifactNotFoundError(
                "Workflow inputs must reference a platform-managed artifact.",
                artifact_id=str(artifact_id),
            )

        if allow_legacy_authorisation and allow_internal_authorisation:
            raise ValueError("Legacy and internal workflow artifact escapes are mutually exclusive")
        if authentication_context is not None and (
            allow_legacy_authorisation or allow_internal_authorisation
        ):
            raise ValueError("Workflow artifact escapes cannot carry an authenticated principal")
        if authentication_context is not None:
            principal_scope = authentication_context.principal.organisation_id
            if organisation_id is not None and organisation_id != principal_scope:
                raise ValueError("Workflow artifact organisation does not match the principal")
            organisation_id = principal_scope
        elif not (allow_legacy_authorisation or allow_internal_authorisation):
            raise AuthenticationRequiredError(
                "An authenticated principal is required to stage workflow artifacts."
            )
        return await self._artifacts.get(
            artifact_id,
            organisation_id=organisation_id,
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
