from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    IssuedArtifactTransfer,
)
from lab_platform.control_plane_core.errors import AgentNotFoundError
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import (
    ArtifactNotFoundError,
    AuthenticationRequiredError,
    CiSessionNotFoundError,
    PermissionDeniedError,
)
from lab_platform.models import (
    AgentRecord,
    ArtifactOwnerType,
    ArtifactRecord,
    AuthenticationContext,
    AuthorisationResource,
    CiSession,
    DistributedCiWorkflowBinding,
    DistributedOperation,
    GlobalBenchRecord,
    Principal,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandType,
    ResourceType,
    WorkflowDefinition,
    WorkflowRun,
)


class RemoteArtifactRepository(Protocol):
    async def get(
        self,
        artifact_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteArtifactMetadata | None: ...

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        command_id: UUID | None = None,
        limit: int = 500,
    ) -> list[RemoteArtifactMetadata]: ...


class OperationRepository(Protocol):
    async def get(
        self,
        operation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation | None: ...


class CommandRepository(Protocol):
    async def get_command(
        self,
        command_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> RemoteCommand | None: ...


class WorkflowRepository(Protocol):
    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None: ...

    async def get_run(
        self,
        run_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowRun | None: ...


class InventoryRepository(Protocol):
    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None: ...


class PresenceService(Protocol):
    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord: ...


class CiService(Protocol):
    async def get(
        self,
        session_id: UUID,
        *,
        synchronize: bool = True,
        organisation_id: UUID | None = None,
    ) -> CiSession: ...


class CiRepository(Protocol):
    async def get_distributed_workflow(
        self,
        session_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedCiWorkflowBinding | None: ...

    async def get_distributed_workflow_for_operation(
        self,
        operation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedCiWorkflowBinding | None: ...


@dataclass(frozen=True, slots=True)
class ArtifactCollection:
    platform: list[ArtifactRecord]
    remote: list[RemoteArtifactMetadata]


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    record: ArtifactRecord | RemoteArtifactMetadata
    stream: AsyncIterable[bytes]


@dataclass(frozen=True, slots=True)
class _ArtifactParent:
    resources: tuple[AuthorisationResource, ...]
    ci_session: CiSession | None = None


class ProtectedArtifactService:
    """Authorised application boundary for identity-facing artifact operations.

    Raw artifact repositories and transfer services remain infrastructure for the
    trusted workflow, CI and Agent protocol paths. Calls crossing from an identity
    boundary must use this service and select exactly one authorisation mode.
    """

    def __init__(
        self,
        platform: ArtifactService,
        remote: RemoteArtifactRepository,
        operations: OperationRepository,
        commands: CommandRepository,
        workflows: WorkflowRepository,
        inventory: InventoryRepository,
        presence: PresenceService,
        ci: CiService,
        ci_repository: CiRepository,
        authorisation: AuthorisationService,
        transfers: DistributedArtifactService,
        transfer_store: FilesystemTransferStore,
        *,
        maximum_transfer_size_bytes: int,
        hide_unauthorised_resources: bool = True,
    ) -> None:
        self._platform = platform
        self._remote = remote
        self._operations = operations
        self._commands = commands
        self._workflows = workflows
        self._inventory = inventory
        self._presence = presence
        self._ci = ci
        self._ci_repository = ci_repository
        self._authorisation = authorisation
        self._transfers = transfers
        self._transfer_store = transfer_store
        self._maximum_transfer_size_bytes = maximum_transfer_size_bytes
        self._hide_unauthorised_resources = hide_unauthorised_resources

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
        idempotency_key: str | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            parent = await self._owner_parent(owner_type, owner_id, scope)
            await self._require_parent(
                authentication_context,
                parent,
                "artifacts:write",
                owner_type=owner_type,
                owner_id=owner_id,
            )
        return await self._store_upload(
            chunks,
            owner_type=owner_type,
            owner_id=owner_id,
            name=name,
            artifact_type=artifact_type,
            content_type=content_type,
            expected_sha256=expected_sha256,
            idempotency_key=idempotency_key,
            organisation_id=scope,
        )

    async def upload_for_operation_route(
        self,
        chunks: AsyncIterable[bytes],
        *,
        operation_id: UUID,
        agent_id: UUID,
        bench_id: str,
        name: str,
        artifact_type: str,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        """Store an input under an operation ID allocated immediately before creation."""

        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        try:
            agent = await self._presence.get_agent(agent_id, organisation_id=scope)
        except AgentNotFoundError:
            agent = None
        bench = await self._inventory.get(bench_id, organisation_id=scope)
        parent = (
            _ArtifactParent((self._bench_resource(bench.organisation_id, bench),))
            if agent is not None
            and bench is not None
            and bench.agent_id == agent.id
            and bench.organisation_id == agent.organisation_id
            else None
        )
        if authentication_context is not None:
            await self._require_parent(
                authentication_context,
                parent,
                "artifacts:write",
                owner_type=ArtifactOwnerType.OPERATION,
                owner_id=operation_id,
            )
        elif parent is None:
            raise ArtifactNotFoundError(
                "The future operation's trusted Agent and bench route does not exist.",
                owner_type=ArtifactOwnerType.OPERATION.value,
                owner_id=str(operation_id),
            )
        return await self._store_upload(
            chunks,
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=operation_id,
            name=name,
            artifact_type=artifact_type,
            content_type=content_type,
            expected_sha256=expected_sha256,
            metadata=metadata,
            idempotency_key=idempotency_key,
            organisation_id=scope,
        )

    async def _store_upload(
        self,
        chunks: AsyncIterable[bytes],
        *,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        name: str,
        artifact_type: str,
        content_type: str | None,
        expected_sha256: str | None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None,
        organisation_id: UUID | None,
    ) -> ArtifactRecord:
        # Parent or route resolution and permission checks deliberately precede
        # iteration of the request stream, so denied uploads cannot persist content.
        record = await self._platform.upload(
            chunks,
            owner_type=owner_type,
            owner_id=owner_id,
            name=name,
            artifact_type=artifact_type,
            content_type=content_type,
            expected_sha256=expected_sha256,
            metadata=metadata,
            idempotency_key=idempotency_key,
            organisation_id=organisation_id,
        )
        source = await self._platform.content_stream(
            record.id,
            organisation_id=organisation_id,
        )
        await self._transfer_store.write_verified_object(
            record.id,
            record.sha256,
            record.size_bytes,
            source,
            maximum_size_bytes=self._maximum_transfer_size_bytes,
        )
        return record

    async def list(
        self,
        *,
        owner_type: ArtifactOwnerType | None = None,
        owner_id: UUID | None = None,
        agent_id: UUID | None = None,
        command_id: UUID | None = None,
        limit: int = 500,
        include_remote: bool = True,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactCollection:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if (owner_type is None) != (owner_id is None):
            raise ValueError("owner_type and owner_id must be supplied together")
        if owner_type is not None and owner_id is not None:
            platform = await self._platform.list_for_owner(
                owner_type,
                owner_id,
                organisation_id=scope,
            )
        elif agent_id is None and command_id is None:
            platform = await self._platform.list_all(organisation_id=scope, limit=limit)
        else:
            platform = []
        remote = (
            await self._remote.list(
                organisation_id=scope,
                agent_id=agent_id,
                command_id=command_id,
                limit=limit,
            )
            if include_remote
            else []
        )
        if authentication_context is None:
            return ArtifactCollection(platform, remote)
        return await self._filter(authentication_context, platform, remote)

    async def get(
        self,
        artifact_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord | RemoteArtifactMetadata:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        try:
            record = await self._platform.get(artifact_id, organisation_id=scope)
        except ArtifactNotFoundError:
            remote = await self._remote.get(artifact_id, organisation_id=scope)
            if remote is None:
                raise ArtifactNotFoundError(
                    f"Artifact {artifact_id} does not exist.", artifact_id=str(artifact_id)
                ) from None
            if authentication_context is not None:
                parent = await self._remote_parent(remote, scope)
                await self._require_parent(
                    authentication_context,
                    parent,
                    "artifacts:read",
                    artifact_id=artifact_id,
                )
            return remote
        if authentication_context is not None:
            parent = await self._platform_parent(record, scope)
            await self._require_parent(
                authentication_context,
                parent,
                "artifacts:read",
                artifact_id=artifact_id,
            )
        return record

    async def content(
        self,
        artifact_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactContent:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        record = await self.get(
            artifact_id,
            authentication_context=authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=scope,
        )
        stream = (
            await self._transfer_store.open_verified_stream(record.id, record.sha256)
            if isinstance(record, RemoteArtifactMetadata)
            else await self._platform.content_stream(record.id, organisation_id=scope)
        )
        return ArtifactContent(record, stream)

    async def delete(
        self,
        artifact_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> ArtifactRecord:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        record = await self._platform.get(artifact_id, organisation_id=scope)
        if authentication_context is not None:
            parent = await self._platform_parent(record, scope)
            await self._require_parent(
                authentication_context,
                parent,
                "artifacts:delete",
                artifact_id=artifact_id,
            )
        return await self._platform.delete(artifact_id, organisation_id=scope)

    async def issue_download(
        self,
        artifact_id: UUID,
        *,
        agent_id: UUID,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> IssuedArtifactTransfer:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        record = await self._platform.get(artifact_id, organisation_id=scope)
        if authentication_context is not None:
            parent = await self._platform_parent(record, scope)
            await self._require_parent(
                authentication_context,
                parent,
                "artifacts:read",
                artifact_id=artifact_id,
            )
        # Validate the target Agent against the same trusted tenant before issuing
        # a bearer capability or writing a transfer record.
        await self._presence.get_agent(agent_id, organisation_id=scope)
        return await self._transfers.issue_download(
            agent_id=agent_id,
            artifact_id=record.id,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
        )

    async def _owner_parent(
        self,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
        organisation_id: UUID | None,
    ) -> _ArtifactParent | None:
        if organisation_id is None:
            return None
        if owner_type is ArtifactOwnerType.OPERATION:
            operation = await self._operations.get(owner_id, organisation_id=organisation_id)
            return await self._operation_parent(operation, organisation_id)
        if owner_type is ArtifactOwnerType.WORKFLOW_RUN:
            run = await self._workflows.get_run(owner_id, organisation_id=organisation_id)
            if run is None or run.organisation_id != organisation_id:
                return None
            definition, bench = await asyncio.gather(
                self._workflows.get_definition(
                    run.workflow_name,
                    run.workflow_version,
                    organisation_id=organisation_id,
                ),
                self._inventory.get(run.bench_id, organisation_id=organisation_id),
            )
            if (
                definition is None
                or definition.organisation_id != organisation_id
                or bench is None
                or bench.organisation_id != organisation_id
            ):
                return None
            return _ArtifactParent(
                (
                    AuthorisationResource(
                        type=ResourceType.WORKFLOW,
                        id=definition.name,
                        organisation_id=organisation_id,
                    ),
                    self._bench_resource(organisation_id, bench),
                )
            )
        if owner_type is ArtifactOwnerType.CI_SESSION:
            try:
                session = await self._ci.get(
                    owner_id,
                    synchronize=False,
                    organisation_id=organisation_id,
                )
            except CiSessionNotFoundError:
                return None
            binding = await self._ci_repository.get_distributed_workflow(
                session.id,
                organisation_id=organisation_id,
            )
            if binding is None:
                return _ArtifactParent((), ci_session=session)
            definition, operation = await asyncio.gather(
                self._workflows.get_definition(
                    binding.workflow_name,
                    binding.workflow_version,
                    organisation_id=organisation_id,
                ),
                self._operations.get(
                    binding.operation_id,
                    organisation_id=organisation_id,
                ),
            )
            operation_parent = await self._bench_operation_parent(operation, organisation_id)
            if (
                definition is None
                or operation is None
                or operation_parent is None
                or operation.remote_command_id != binding.remote_command_id
                or operation.agent_id != binding.agent_id
                or operation.bench_id != binding.bench_id
            ):
                return None
            workflow_resource = AuthorisationResource(
                type=ResourceType.WORKFLOW,
                id=definition.name,
                organisation_id=organisation_id,
            )
            return _ArtifactParent(
                (workflow_resource, *operation_parent.resources),
                ci_session=session,
            )
        # A workflow-step UUID cannot yet be resolved through a trusted,
        # tenant-scoped repository route, so Phase 6 access fails closed.
        return None

    async def _operation_parent(
        self,
        operation: DistributedOperation | None,
        organisation_id: UUID,
    ) -> _ArtifactParent | None:
        route = await self._bench_operation_parent(operation, organisation_id)
        if route is None or operation is None:
            return None
        command = await self._commands.get_command(
            operation.remote_command_id,
            organisation_id=organisation_id,
        )
        if (
            command is None
            or command.operation_id != operation.id
            or command.agent_id != operation.agent_id
            or command.bench_id != operation.bench_id
        ):
            return None
        if command.command_type is not RemoteCommandType.RUN_WORKFLOW:
            return route
        return await self._workflow_operation_parent(
            command,
            operation,
            route,
            organisation_id,
        )

    async def _bench_operation_parent(
        self,
        operation: DistributedOperation | None,
        organisation_id: UUID,
    ) -> _ArtifactParent | None:
        if operation is None or operation.organisation_id != organisation_id:
            return None
        bench = await self._inventory.get(
            operation.bench_id,
            organisation_id=organisation_id,
        )
        if bench is None or bench.agent_id != operation.agent_id:
            return None
        return _ArtifactParent((self._bench_resource(organisation_id, bench),))

    async def _workflow_operation_parent(
        self,
        command: RemoteCommand,
        operation: DistributedOperation,
        route: _ArtifactParent,
        organisation_id: UUID,
    ) -> _ArtifactParent | None:
        raw_definition = command.payload.get("definition")
        try:
            definition = WorkflowDefinition.model_validate(raw_definition)
        except (TypeError, ValueError):
            return None
        catalog_definition = await self._workflows.get_definition(
            definition.name,
            definition.version,
            organisation_id=organisation_id,
        )
        if (
            definition.organisation_id != organisation_id
            or catalog_definition is None
            or catalog_definition != definition
        ):
            return None
        workflow_resource = AuthorisationResource(
            type=ResourceType.WORKFLOW,
            id=definition.name,
            organisation_id=organisation_id,
        )
        binding = await self._ci_repository.get_distributed_workflow_for_operation(
            operation.id,
            organisation_id=organisation_id,
        )
        if binding is None:
            return _ArtifactParent((workflow_resource, *route.resources))
        if (
            binding.remote_command_id != command.id
            or binding.operation_id != operation.id
            or binding.agent_id != operation.agent_id
            or binding.bench_id != operation.bench_id
            or binding.workflow_name != definition.name
            or binding.workflow_version != definition.version
        ):
            return None
        try:
            session = await self._ci.get(
                binding.ci_session_id,
                synchronize=False,
                organisation_id=organisation_id,
            )
        except CiSessionNotFoundError:
            return None
        return _ArtifactParent(
            (workflow_resource, *route.resources),
            ci_session=session,
        )

    async def _platform_parent(
        self,
        artifact: ArtifactRecord,
        organisation_id: UUID | None,
    ) -> _ArtifactParent | None:
        if organisation_id is None or artifact.organisation_id != organisation_id:
            return None
        return await self._owner_parent(
            artifact.owner_type,
            artifact.owner_id,
            organisation_id,
        )

    async def _remote_parent(
        self,
        artifact: RemoteArtifactMetadata,
        organisation_id: UUID | None,
    ) -> _ArtifactParent | None:
        if organisation_id is None or artifact.organisation_id != organisation_id:
            return None
        command = await self._commands.get_command(
            artifact.command_id,
            organisation_id=organisation_id,
        )
        if command is None or command.agent_id != artifact.agent_id:
            return None
        operation_id = artifact.operation_id or command.operation_id
        operation: DistributedOperation | None = None
        if operation_id is not None:
            operation = await self._operations.get(
                operation_id,
                organisation_id=organisation_id,
            )
            if (
                operation is None
                or operation.remote_command_id != command.id
                or operation.agent_id != command.agent_id
                or operation.bench_id != command.bench_id
            ):
                return None
        try:
            agent = await self._presence.get_agent(
                command.agent_id,
                organisation_id=organisation_id,
            )
        except AgentNotFoundError:
            return None
        bench = await self._inventory.get(
            command.bench_id,
            organisation_id=organisation_id,
        )
        if bench is None or bench.agent_id != agent.id:
            return None
        if operation is not None and (
            operation.agent_id != agent.id or operation.bench_id != bench.id
        ):
            return None
        operation_parent = await self._operation_parent(operation, organisation_id)
        if operation is not None:
            return operation_parent
        # A workflow command without its durable operation cannot be tied to a
        # workflow/CI launch through trusted state.  Falling back to the bench
        # alone would let workflow and session checks be skipped.
        if command.command_type is RemoteCommandType.RUN_WORKFLOW:
            return None
        return _ArtifactParent((self._bench_resource(organisation_id, bench),))

    async def _filter(
        self,
        context: AuthenticationContext,
        platform: Sequence[ArtifactRecord],
        remote: Sequence[RemoteArtifactMetadata],
    ) -> ArtifactCollection:
        organisation_id = context.principal.organisation_id
        platform_parents, remote_parents = await asyncio.gather(
            asyncio.gather(*(self._platform_parent(item, organisation_id) for item in platform)),
            asyncio.gather(*(self._remote_parent(item, organisation_id) for item in remote)),
        )
        platform_allowed, remote_allowed = await asyncio.gather(
            asyncio.gather(
                *(
                    self._parent_is_allowed(context, parent, "artifacts:read")
                    for parent in platform_parents
                )
            ),
            asyncio.gather(
                *(
                    self._parent_is_allowed(context, parent, "artifacts:read")
                    for parent in remote_parents
                )
            ),
        )
        return ArtifactCollection(
            [
                artifact
                for artifact, allowed in zip(platform, platform_allowed, strict=True)
                if allowed
            ],
            [artifact for artifact, allowed in zip(remote, remote_allowed, strict=True) if allowed],
        )

    async def _parent_is_allowed(
        self,
        context: AuthenticationContext,
        parent: _ArtifactParent | None,
        permission: str,
    ) -> bool:
        if parent is None:
            return False
        if parent.ci_session is not None and not await self._ci_session_is_allowed(
            context,
            parent.ci_session,
            self._ci_permission(permission),
        ):
            return False
        if parent.resources:
            decisions = await asyncio.gather(
                *(
                    self._authorisation.is_allowed(
                        context.principal,
                        permission,
                        resource,
                        credential_restrictions=context.permission_restrictions,
                    )
                    for resource in parent.resources
                )
            )
            return all(decisions)
        if parent.ci_session is not None:
            return await self._authorisation.is_allowed_anywhere(
                context.principal,
                permission,
                credential_restrictions=context.permission_restrictions,
            )
        return False

    async def _require_parent(
        self,
        context: AuthenticationContext,
        parent: _ArtifactParent | None,
        permission: str,
        *,
        artifact_id: UUID | None = None,
        owner_type: ArtifactOwnerType | None = None,
        owner_id: UUID | None = None,
    ) -> None:
        if parent is None:
            await self._authorisation.audit_permission_denied(
                context.principal,
                permission,
                resource_type="ARTIFACT",
                resource_id=str(artifact_id or owner_id) if artifact_id or owner_id else None,
                reason="The artifact's trusted parent could not be resolved.",
            )
            self._raise_hidden_or_denied(
                permission,
                artifact_id=artifact_id,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        assert parent is not None
        try:
            if parent.ci_session is not None:
                session_permission = self._ci_permission(permission)
                if self._ci_session_is_owned_by(parent.ci_session, context.principal):
                    await self._authorisation.require_anywhere(
                        context.principal,
                        session_permission,
                        credential_restrictions=context.permission_restrictions,
                    )
                else:
                    await self._authorisation.require(
                        context.principal,
                        session_permission,
                        self._organisation_resource(context.principal),
                        credential_restrictions=context.permission_restrictions,
                    )
            if parent.resources:
                for resource in parent.resources:
                    await self._authorisation.require(
                        context.principal,
                        permission,
                        resource,
                        credential_restrictions=context.permission_restrictions,
                    )
            elif parent.ci_session is not None:
                await self._authorisation.require_anywhere(
                    context.principal,
                    permission,
                    credential_restrictions=context.permission_restrictions,
                )
        except PermissionDeniedError:
            if self._hide_unauthorised_resources:
                raise ArtifactNotFoundError(
                    "The artifact does not exist.",
                    artifact_id=str(artifact_id) if artifact_id is not None else None,
                    owner_type=owner_type.value if owner_type is not None else None,
                    owner_id=str(owner_id) if owner_id is not None else None,
                ) from None
            raise

    def _raise_hidden_or_denied(
        self,
        permission: str,
        *,
        artifact_id: UUID | None,
        owner_type: ArtifactOwnerType | None,
        owner_id: UUID | None,
    ) -> None:
        if self._hide_unauthorised_resources:
            raise ArtifactNotFoundError(
                "The artifact or its trusted parent does not exist.",
                artifact_id=str(artifact_id) if artifact_id is not None else None,
                owner_type=owner_type.value if owner_type is not None else None,
                owner_id=str(owner_id) if owner_id is not None else None,
            )
        raise PermissionDeniedError(
            "The principal is not allowed to access an artifact without a trusted parent.",
            required_permission=permission,
            resource_type="ARTIFACT",
            resource_id=str(artifact_id or owner_id) if artifact_id or owner_id else None,
        )

    async def _ci_session_is_allowed(
        self,
        context: AuthenticationContext,
        session: CiSession,
        permission: str,
    ) -> bool:
        if self._ci_session_is_owned_by(session, context.principal):
            return await self._authorisation.is_allowed_anywhere(
                context.principal,
                permission,
                credential_restrictions=context.permission_restrictions,
            )
        return await self._authorisation.is_allowed(
            context.principal,
            permission,
            self._organisation_resource(context.principal),
            credential_restrictions=context.permission_restrictions,
        )

    @staticmethod
    def _ci_session_is_owned_by(session: CiSession, principal: Principal) -> bool:
        return (
            session.organisation_id == principal.organisation_id
            and session.requested_by_principal_id == principal.id
            and session.requested_by_principal_type is principal.type
        )

    @staticmethod
    def _ci_permission(permission: str) -> str:
        return "ci:sessions:read" if permission == "artifacts:read" else "ci:sessions:create"

    @staticmethod
    def _bench_resource(
        organisation_id: UUID,
        bench: GlobalBenchRecord,
    ) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=organisation_id,
            parent_agent_id=bench.agent_id,
        )

    @staticmethod
    def _organisation_resource(principal: Principal) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.ORGANISATION,
            id=str(principal.organisation_id),
            organisation_id=principal.organisation_id,
        )

    @staticmethod
    def _authorisation_scope(
        authentication_context: AuthenticationContext | None,
        *,
        allow_legacy_authorisation: bool,
        allow_internal_authorisation: bool,
        organisation_id: UUID | None,
    ) -> UUID | None:
        if allow_legacy_authorisation and allow_internal_authorisation:
            raise ValueError(
                "Legacy and internal artifact authorisation escapes are mutually exclusive"
            )
        if authentication_context is not None and (
            allow_legacy_authorisation or allow_internal_authorisation
        ):
            raise ValueError(
                "Artifact authorisation escapes cannot carry an authenticated principal"
            )
        if authentication_context is not None:
            principal_scope = authentication_context.principal.organisation_id
            if organisation_id is not None and organisation_id != principal_scope:
                raise ValueError("Artifact organisation does not match the authenticated principal")
            return principal_scope
        if allow_legacy_authorisation or allow_internal_authorisation:
            return organisation_id
        raise AuthenticationRequiredError(
            "An authenticated principal is required to access artifacts."
        )


__all__ = [
    "ArtifactCollection",
    "ArtifactContent",
    "ProtectedArtifactService",
]
