from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from typing import NoReturn, Protocol
from uuid import UUID

from lab_platform.control_plane_core.errors import (
    AgentNotFoundError,
    RemoteCommandNotFoundError,
)
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    BenchNotFoundError,
    CiSessionNotFoundError,
    PermissionDeniedError,
)
from lab_platform.core.workflows import WorkflowNotFoundError
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    AuthenticationContext,
    AuthorisationResource,
    CiProvider,
    CiSession,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Principal,
    ResourceType,
    WorkflowDefinition,
)


class AgentCatalog(Protocol):
    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord: ...

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
        status: AgentStatus | None = None,
        location: str | None = None,
        labels: Mapping[str, str] | None = None,
        version: str | None = None,
    ) -> list[AgentRecord]: ...


class BenchCatalog(Protocol):
    async def get_bench(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord: ...

    async def list_benches(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        agent_slug: str | None = None,
        status: GlobalBenchStatus | None = None,
        health: HealthStatus | None = None,
        kind: GlobalBenchKind | None = None,
        capability: str | None = None,
        labels: Mapping[str, str] | None = None,
        online: bool | None = None,
    ) -> list[GlobalBenchRecord]: ...


class WorkflowCatalog(Protocol):
    async def save_definition(self, definition: WorkflowDefinition) -> WorkflowDefinition: ...

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None: ...

    async def list_definitions(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[WorkflowDefinition]: ...


class OperationCatalog(Protocol):
    async def get(
        self,
        operation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation | None: ...

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        status: DistributedOperationStatus | None = None,
        limit: int = 500,
    ) -> list[DistributedOperation]: ...


class CiCatalog(Protocol):
    async def get(
        self,
        session_id: UUID,
        *,
        synchronize: bool = True,
        organisation_id: UUID | None = None,
    ) -> CiSession: ...

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> list[CiSession]: ...

    async def details(
        self,
        session_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> dict[str, object]: ...


class OperationalAuthorisation(Protocol):
    async def require(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None: ...

    async def require_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None: ...

    async def is_allowed(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool: ...

    async def is_allowed_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool: ...

    async def audit_permission_denied(
        self,
        principal: Principal,
        permission: str,
        *,
        resource_type: str,
        resource_id: str | None,
        reason: str = "Required permission was not granted.",
    ) -> object: ...

    async def audit_success(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> object: ...


class OperationalAccessService:
    """Authorised application boundary for identity-facing operational reads.

    Raw catalogs remain available to trusted infrastructure. Identity-facing callers
    must use this service and select exactly one authorisation mode.
    """

    def __init__(
        self,
        agents: AgentCatalog,
        benches: BenchCatalog,
        workflows: WorkflowCatalog,
        operations: OperationCatalog,
        ci: CiCatalog,
        authorisation: OperationalAuthorisation,
        *,
        hide_unauthorised_resources: bool = True,
    ) -> None:
        self._agents = agents
        self._benches = benches
        self._workflows = workflows
        self._operations = operations
        self._ci = ci
        self._authorisation = authorisation
        self._hide_unauthorised_resources = hide_unauthorised_resources

    async def list_agents(
        self,
        *,
        status: AgentStatus | None = None,
        location: str | None = None,
        labels: Mapping[str, str] | None = None,
        version: str | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[AgentRecord]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            await self._require_anywhere(authentication_context, "agents:read")
        agents = await self._agents.list_agents(
            organisation_id=scope,
            status=status,
            location=location,
            labels=labels,
            version=version,
        )
        if authentication_context is None:
            return agents
        allowed = await asyncio.gather(
            *(
                self._is_allowed(
                    authentication_context,
                    "agents:read",
                    self._agent_resource(agent),
                )
                for agent in agents
                if agent.organisation_id == scope
            )
        )
        scoped_agents = [agent for agent in agents if agent.organisation_id == scope]
        return [agent for agent, visible in zip(scoped_agents, allowed, strict=True) if visible]

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> AgentRecord:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        agent = await self._agents.get_agent(agent_id, organisation_id=scope)
        if authentication_context is not None:
            if agent.organisation_id != scope:
                raise self._agent_not_found(agent_id)
            await self._require_named(
                authentication_context,
                "agents:read",
                self._agent_resource(agent),
                self._agent_not_found(agent_id),
            )
        return agent

    async def list_benches(
        self,
        *,
        agent_id: UUID | None = None,
        agent_slug: str | None = None,
        location: str | None = None,
        agent_labels: Mapping[str, str] | None = None,
        status: GlobalBenchStatus | None = None,
        health: HealthStatus | None = None,
        kind: GlobalBenchKind | None = None,
        capability: str | None = None,
        labels: Mapping[str, str] | None = None,
        online: bool | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[GlobalBenchRecord]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            policy_catalog = await self._require_bench_collection_access(
                authentication_context,
                scope,
            )
        else:
            policy_catalog = None
        benches = await self._query_benches(
            scope,
            agent_id=agent_id,
            agent_slug=agent_slug,
            location=location,
            agent_labels=agent_labels,
            status=status,
            health=health,
            kind=kind,
            capability=capability,
            labels=labels,
            online=online,
            policy_catalog=policy_catalog,
        )
        if authentication_context is None:
            return benches
        return await self._filter_visible_benches(
            authentication_context,
            benches,
            scope,
        )

    async def list_visible_benches(
        self,
        *,
        agent_id: UUID | None = None,
        agent_slug: str | None = None,
        location: str | None = None,
        agent_labels: Mapping[str, str] | None = None,
        status: GlobalBenchStatus | None = None,
        health: HealthStatus | None = None,
        kind: GlobalBenchKind | None = None,
        capability: str | None = None,
        labels: Mapping[str, str] | None = None,
        online: bool | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[GlobalBenchRecord]:
        """Return visible benches without imposing collection-level admission.

        This is intended for nested summaries reached through another authorised
        resource. Identity callers receive only exact ``benches:read`` matches;
        holding no bench grant is represented by an empty collection.
        """

        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        benches = await self._query_benches(
            scope,
            agent_id=agent_id,
            agent_slug=agent_slug,
            location=location,
            agent_labels=agent_labels,
            status=status,
            health=health,
            kind=kind,
            capability=capability,
            labels=labels,
            online=online,
        )
        if authentication_context is None:
            return benches
        return await self._filter_visible_benches(
            authentication_context,
            benches,
            scope,
        )

    async def _require_bench_collection_access(
        self,
        context: AuthenticationContext,
        organisation_id: UUID | None,
    ) -> list[GlobalBenchRecord] | None:
        if await self._authorisation.is_allowed_anywhere(
            context.principal,
            "benches:read",
            credential_restrictions=context.permission_restrictions,
        ):
            return None
        # BenchAccessPolicy.allowed_team_ids is evaluated only against an exact
        # bench, so the assignment-based is_allowed_anywhere probe cannot see it.
        benches = await self._benches.list_benches(organisation_id=organisation_id)
        scoped_benches = [bench for bench in benches if bench.organisation_id == organisation_id]
        policy_decisions = await asyncio.gather(
            *(
                self._is_allowed(
                    context,
                    "benches:read",
                    self._bench_resource(bench),
                )
                for bench in scoped_benches
            )
        )
        if any(policy_decisions):
            return scoped_benches
        await self._require_anywhere(context, "benches:read")
        raise AssertionError("Unreachable after denied bench collection admission")

    async def _query_benches(
        self,
        organisation_id: UUID | None,
        *,
        agent_id: UUID | None,
        agent_slug: str | None,
        location: str | None,
        agent_labels: Mapping[str, str] | None,
        status: GlobalBenchStatus | None,
        health: HealthStatus | None,
        kind: GlobalBenchKind | None,
        capability: str | None,
        labels: Mapping[str, str] | None,
        online: bool | None,
        policy_catalog: list[GlobalBenchRecord] | None = None,
    ) -> list[GlobalBenchRecord]:
        selected_agent_ids: set[UUID] | None = None
        if location is not None or agent_labels:
            selected_agents = await self._agents.list_agents(
                organisation_id=organisation_id,
                location=location,
                labels=agent_labels,
            )
            selected_agent_ids = {
                agent.id
                for agent in selected_agents
                if organisation_id is None or agent.organisation_id == organisation_id
            }
        if (
            policy_catalog is not None
            and agent_id is None
            and agent_slug is None
            and location is None
            and not agent_labels
            and status is None
            and health is None
            and kind is None
            and capability is None
            and not labels
            and online is None
        ):
            benches = policy_catalog
        else:
            benches = await self._benches.list_benches(
                organisation_id=organisation_id,
                agent_id=agent_id,
                agent_slug=agent_slug,
                status=status,
                health=health,
                kind=kind,
                capability=capability,
                labels=labels,
                online=online,
            )
        if selected_agent_ids is None:
            return benches
        return [bench for bench in benches if bench.agent_id in selected_agent_ids]

    async def _filter_visible_benches(
        self,
        context: AuthenticationContext,
        benches: Collection[GlobalBenchRecord],
        organisation_id: UUID | None,
    ) -> list[GlobalBenchRecord]:
        scoped_benches = [bench for bench in benches if bench.organisation_id == organisation_id]
        allowed = await asyncio.gather(
            *(
                self._is_allowed(
                    context,
                    "benches:read",
                    self._bench_resource(bench),
                )
                for bench in scoped_benches
            )
        )
        return [bench for bench, visible in zip(scoped_benches, allowed, strict=True) if visible]

    async def get_bench(
        self,
        bench_id: str,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        bench = await self._benches.get_bench(bench_id, organisation_id=scope)
        if authentication_context is not None:
            if bench.organisation_id != scope:
                raise self._bench_not_found(bench_id)
            await self._require_named(
                authentication_context,
                "benches:read",
                self._bench_resource(bench),
                self._bench_not_found(bench_id),
            )
        return bench

    async def save_workflow(
        self,
        definition: WorkflowDefinition,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            await self._authorisation.require(
                authentication_context.principal,
                "workflows:manage",
                self._organisation_resource(authentication_context.principal),
                credential_restrictions=authentication_context.permission_restrictions,
            )
        if scope is not None:
            definition = definition.model_copy(update={"organisation_id": scope})
        stored = await self._workflows.save_definition(definition)
        if authentication_context is not None:
            await self._authorisation.audit_success(
                authentication_context.principal,
                "WORKFLOW_REGISTERED",
                resource_type=ResourceType.WORKFLOW.value,
                resource_id=stored.name,
                metadata={"version": stored.version},
            )
        return stored

    async def list_workflows(
        self,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[WorkflowDefinition]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            await self._require_anywhere(authentication_context, "workflows:read")
        definitions = await self._workflows.list_definitions(organisation_id=scope)
        if authentication_context is None:
            return definitions
        scoped_definitions = [
            definition for definition in definitions if definition.organisation_id == scope
        ]
        allowed = await asyncio.gather(
            *(
                self._is_allowed(
                    authentication_context,
                    "workflows:read",
                    self._workflow_resource(definition),
                )
                for definition in scoped_definitions
            )
        )
        return [
            definition
            for definition, visible in zip(scoped_definitions, allowed, strict=True)
            if visible
        ]

    async def get_workflow(
        self,
        name: str,
        version: int | None = None,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        definition = await self._workflows.get_definition(
            name,
            version,
            organisation_id=scope,
        )
        if definition is None or (
            authentication_context is not None and definition.organisation_id != scope
        ):
            raise self._workflow_not_found(name, version)
        if authentication_context is not None:
            await self._require_named(
                authentication_context,
                "workflows:read",
                self._workflow_resource(definition),
                self._workflow_not_found(name, version),
            )
        return definition

    async def list_operations(
        self,
        *,
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        status: DistributedOperationStatus | None = None,
        limit: int = 500,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[DistributedOperation]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            await self._require_anywhere(authentication_context, "operations:read")
        operations = await self._operations.list(
            organisation_id=scope,
            agent_id=agent_id,
            bench_id=bench_id,
            status=status,
            limit=limit,
        )
        if authentication_context is None:
            return operations
        resources = await asyncio.gather(
            *(self._operation_resource(operation, scope) for operation in operations)
        )
        allowed = await asyncio.gather(
            *(
                self._is_allowed(authentication_context, "operations:read", resource)
                if resource is not None
                else self._false()
                for resource in resources
            )
        )
        return [
            operation for operation, visible in zip(operations, allowed, strict=True) if visible
        ]

    async def get_operation(
        self,
        operation_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        operation = await self._operations.get(operation_id, organisation_id=scope)
        if operation is None:
            raise self._operation_not_found(operation_id)
        if authentication_context is not None:
            resource = await self._operation_resource(operation, scope)
            if resource is None:
                await self._deny_unresolved(
                    authentication_context,
                    "operations:read",
                    resource_type=ResourceType.BENCH.value,
                    resource_id=operation.bench_id,
                    hidden=self._operation_not_found(operation_id),
                )
            assert resource is not None
            await self._require_named(
                authentication_context,
                "operations:read",
                resource,
                self._operation_not_found(operation_id),
            )
        return operation

    async def list_ci_sessions(
        self,
        *,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> list[CiSession]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        if authentication_context is not None:
            await self._require_anywhere(authentication_context, "ci:sessions:read")
        sessions = await self._ci.list(
            organisation_id=scope,
            status=status,
            provider=provider,
            limit=limit,
        )
        if authentication_context is None:
            return sessions
        allowed = await asyncio.gather(
            *(self._ci_session_is_allowed(authentication_context, session) for session in sessions)
        )
        return [session for session, visible in zip(sessions, allowed, strict=True) if visible]

    async def get_ci_session(
        self,
        session_id: UUID,
        *,
        synchronize: bool = True,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> CiSession:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        session = await self._ci.get(
            session_id,
            synchronize=False,
            organisation_id=scope,
        )
        if authentication_context is not None:
            await self._require_ci_session(authentication_context, session)
        if synchronize:
            session = await self._ci.get(
                session_id,
                synchronize=True,
                organisation_id=scope,
            )
        return session

    async def get_ci_session_details(
        self,
        session_id: UUID,
        *,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
        organisation_id: UUID | None = None,
    ) -> dict[str, object]:
        scope = self._authorisation_scope(
            authentication_context,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
            organisation_id=organisation_id,
        )
        session = await self._ci.get(
            session_id,
            synchronize=False,
            organisation_id=scope,
        )
        if authentication_context is not None:
            await self._require_ci_session(authentication_context, session)
        return await self._ci.details(session_id, organisation_id=scope)

    async def _operation_resource(
        self,
        operation: DistributedOperation,
        organisation_id: UUID | None,
    ) -> AuthorisationResource | None:
        if organisation_id is None:
            return None
        try:
            bench = await self._benches.get_bench(
                operation.bench_id,
                organisation_id=organisation_id,
            )
        except BenchNotFoundError:
            return None
        if (
            operation.organisation_id != organisation_id
            or bench.organisation_id != organisation_id
            or bench.agent_id != operation.agent_id
        ):
            return None
        return self._bench_resource(bench)

    async def _ci_session_is_allowed(
        self,
        context: AuthenticationContext,
        session: CiSession,
    ) -> bool:
        principal = context.principal
        if (
            session.organisation_id is not None
            and session.organisation_id != principal.organisation_id
        ):
            return False
        if self._ci_session_is_owned_by(session, principal):
            return await self._authorisation.is_allowed_anywhere(
                principal,
                "ci:sessions:read",
                credential_restrictions=context.permission_restrictions,
            )
        return await self._is_allowed(
            context,
            "ci:sessions:read",
            self._organisation_resource(principal),
        )

    async def _require_ci_session(
        self,
        context: AuthenticationContext,
        session: CiSession,
    ) -> None:
        if (
            session.organisation_id is not None
            and session.organisation_id != context.principal.organisation_id
        ):
            await self._deny_unresolved(
                context,
                "ci:sessions:read",
                resource_type="CI_SESSION",
                resource_id=str(session.id),
                hidden=self._ci_session_not_found(session.id),
            )
        try:
            if self._ci_session_is_owned_by(session, context.principal):
                await self._require_anywhere(context, "ci:sessions:read")
            else:
                await self._authorisation.require(
                    context.principal,
                    "ci:sessions:read",
                    self._organisation_resource(context.principal),
                    credential_restrictions=context.permission_restrictions,
                )
        except PermissionDeniedError:
            if self._hide_unauthorised_resources:
                raise self._ci_session_not_found(session.id) from None
            raise

    async def _require_named(
        self,
        context: AuthenticationContext,
        permission: str,
        resource: AuthorisationResource,
        hidden: Exception,
    ) -> None:
        try:
            await self._authorisation.require(
                context.principal,
                permission,
                resource,
                credential_restrictions=context.permission_restrictions,
            )
        except PermissionDeniedError:
            if self._hide_unauthorised_resources:
                raise hidden from None
            raise

    async def _deny_unresolved(
        self,
        context: AuthenticationContext,
        permission: str,
        *,
        resource_type: str,
        resource_id: str | None,
        hidden: Exception,
    ) -> NoReturn:
        await self._authorisation.audit_permission_denied(
            context.principal,
            permission,
            resource_type=resource_type,
            resource_id=resource_id,
            reason="The resource's trusted operational parent could not be resolved.",
        )
        if self._hide_unauthorised_resources:
            raise hidden
        raise PermissionDeniedError(
            "The principal cannot access a resource without its trusted operational parent.",
            required_permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    async def _require_anywhere(
        self,
        context: AuthenticationContext,
        permission: str,
    ) -> None:
        await self._authorisation.require_anywhere(
            context.principal,
            permission,
            credential_restrictions=context.permission_restrictions,
        )

    async def _is_allowed(
        self,
        context: AuthenticationContext,
        permission: str,
        resource: AuthorisationResource,
    ) -> bool:
        return await self._authorisation.is_allowed(
            context.principal,
            permission,
            resource,
            credential_restrictions=context.permission_restrictions,
        )

    @staticmethod
    async def _false() -> bool:
        return False

    @staticmethod
    def _agent_resource(agent: AgentRecord) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.AGENT,
            id=str(agent.id),
            organisation_id=agent.organisation_id,
        )

    @staticmethod
    def _bench_resource(bench: GlobalBenchRecord) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        )

    @staticmethod
    def _workflow_resource(definition: WorkflowDefinition) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.WORKFLOW,
            id=definition.name,
            organisation_id=definition.organisation_id,
        )

    @staticmethod
    def _organisation_resource(principal: Principal) -> AuthorisationResource:
        return AuthorisationResource(
            type=ResourceType.ORGANISATION,
            id=str(principal.organisation_id),
            organisation_id=principal.organisation_id,
        )

    @staticmethod
    def _ci_session_is_owned_by(session: CiSession, principal: Principal) -> bool:
        return (
            session.organisation_id == principal.organisation_id
            and session.requested_by_principal_id == principal.id
            and session.requested_by_principal_type is principal.type
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
                "Legacy and internal operational authorisation escapes are mutually exclusive"
            )
        if authentication_context is not None and (
            allow_legacy_authorisation or allow_internal_authorisation
        ):
            raise ValueError(
                "Operational authorisation escapes cannot carry an authenticated principal"
            )
        if authentication_context is not None:
            principal_scope = authentication_context.principal.organisation_id
            if organisation_id is not None and organisation_id != principal_scope:
                raise ValueError(
                    "Operational organisation does not match the authenticated principal"
                )
            return principal_scope
        if allow_legacy_authorisation or allow_internal_authorisation:
            return organisation_id
        raise AuthenticationRequiredError(
            "An authenticated principal is required to access operational resources."
        )

    @staticmethod
    def _agent_not_found(agent_id: UUID) -> AgentNotFoundError:
        return AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))

    @staticmethod
    def _bench_not_found(bench_id: str) -> BenchNotFoundError:
        return BenchNotFoundError("Bench does not exist.", bench_id=bench_id)

    @staticmethod
    def _workflow_not_found(name: str, version: int | None) -> WorkflowNotFoundError:
        return WorkflowNotFoundError(
            "Workflow does not exist.",
            workflow_name=name,
            workflow_version=version,
        )

    @staticmethod
    def _operation_not_found(operation_id: UUID) -> RemoteCommandNotFoundError:
        return RemoteCommandNotFoundError(
            "Distributed operation does not exist.",
            operation_id=str(operation_id),
        )

    @staticmethod
    def _ci_session_not_found(session_id: UUID) -> CiSessionNotFoundError:
        return CiSessionNotFoundError(
            "CI session does not exist.",
            ci_session_id=str(session_id),
        )


__all__ = ["OperationalAccessService"]
