from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane.operational_access import OperationalAccessService
from lab_platform.control_plane_core.errors import AgentNotFoundError, RemoteCommandNotFoundError
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
    PrincipalType,
    ResourceType,
    WaitWorkflowStep,
    WorkflowDefinition,
    WorkflowRequirements,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
ORGANISATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_ORGANISATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PRINCIPAL_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PRINCIPAL_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeAuthorisation:
    def __init__(
        self,
        *,
        anywhere: Collection[str] = (),
        allowed: Collection[tuple[str, ResourceType, str]] = (),
    ) -> None:
        self.anywhere = set(anywhere)
        self.allowed = set(allowed)
        self.require_anywhere_calls: list[tuple[str, Collection[str] | None]] = []
        self.is_allowed_calls: list[tuple[str, AuthorisationResource, Collection[str] | None]] = []
        self.denials: list[tuple[str, str, str | None]] = []
        self.successes: list[tuple[str, str, str | None, dict[str, object] | None]] = []

    @staticmethod
    def _restriction_allows(
        permission: str,
        restrictions: Collection[str] | None,
    ) -> bool:
        return restrictions is None or permission in restrictions

    async def is_allowed_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool:
        del principal
        return permission in self.anywhere and self._restriction_allows(
            permission,
            credential_restrictions,
        )

    async def require_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None:
        self.require_anywhere_calls.append((permission, credential_restrictions))
        if await self.is_allowed_anywhere(
            principal,
            permission,
            credential_restrictions=credential_restrictions,
        ):
            return
        await self.audit_permission_denied(
            principal,
            permission,
            resource_type=ResourceType.ORGANISATION.value,
            resource_id=str(principal.organisation_id),
        )
        raise PermissionDeniedError("denied", required_permission=permission)

    async def is_allowed(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool:
        del principal
        self.is_allowed_calls.append((permission, resource, credential_restrictions))
        return (
            permission,
            resource.type,
            resource.id,
        ) in self.allowed and self._restriction_allows(permission, credential_restrictions)

    async def require(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None:
        if await self.is_allowed(
            principal,
            permission,
            resource,
            credential_restrictions=credential_restrictions,
        ):
            return
        await self.audit_permission_denied(
            principal,
            permission,
            resource_type=resource.type.value,
            resource_id=resource.id,
        )
        raise PermissionDeniedError("denied", required_permission=permission)

    async def audit_permission_denied(
        self,
        principal: Principal,
        permission: str,
        *,
        resource_type: str,
        resource_id: str | None,
        reason: str = "Required permission was not granted.",
    ) -> object:
        del principal, reason
        self.denials.append((permission, resource_type, resource_id))
        return object()

    async def audit_success(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> object:
        del principal
        self.successes.append((action, resource_type, resource_id, metadata))
        return object()


class FakeAgents:
    def __init__(self, agents: Collection[AgentRecord]) -> None:
        self.agents = list(agents)
        self.reads = 0

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord:
        self.reads += 1
        for agent in self.agents:
            if agent.id == agent_id and (
                organisation_id is None or agent.organisation_id == organisation_id
            ):
                return agent
        raise AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
        status: AgentStatus | None = None,
        location: str | None = None,
        labels: Mapping[str, str] | None = None,
        version: str | None = None,
    ) -> list[AgentRecord]:
        self.reads += 1
        result = [
            agent
            for agent in self.agents
            if organisation_id is None or agent.organisation_id == organisation_id
        ]
        if status is not None:
            result = [agent for agent in result if agent.status is status]
        if location is not None:
            result = [agent for agent in result if agent.location == location]
        for key, value in (labels or {}).items():
            result = [agent for agent in result if agent.labels.get(key) == value]
        if version is not None:
            result = [agent for agent in result if agent.version == version]
        return result


class FakeBenches:
    def __init__(self, benches: Collection[GlobalBenchRecord]) -> None:
        self.benches = list(benches)

    async def get_bench(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord:
        for bench in self.benches:
            if bench.id == bench_id and (
                organisation_id is None or bench.organisation_id == organisation_id
            ):
                return bench
        raise BenchNotFoundError("Bench does not exist.", bench_id=bench_id)

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
    ) -> list[GlobalBenchRecord]:
        result = [
            bench
            for bench in self.benches
            if organisation_id is None or bench.organisation_id == organisation_id
        ]
        if agent_id is not None:
            result = [bench for bench in result if bench.agent_id == agent_id]
        if agent_slug is not None:
            result = [bench for bench in result if bench.agent_slug == agent_slug]
        if status is not None:
            result = [bench for bench in result if bench.status is status]
        if health is not None:
            result = [bench for bench in result if bench.health is health]
        if kind is not None:
            result = [bench for bench in result if bench.kind is kind]
        if capability is not None:
            result = [bench for bench in result if capability in bench.capabilities]
        for key, value in (labels or {}).items():
            result = [bench for bench in result if bench.labels.get(key) == value]
        if online is not None:
            result = [bench for bench in result if bench.online is online]
        return result


class FakeWorkflows:
    def __init__(self, definitions: Collection[WorkflowDefinition]) -> None:
        self.definitions = list(definitions)
        self.saved: list[WorkflowDefinition] = []

    async def save_definition(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        self.saved.append(definition)
        self.definitions.append(definition)
        return definition

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None:
        candidates = [
            definition
            for definition in self.definitions
            if definition.name == name
            and (version is None or definition.version == version)
            and (organisation_id is None or definition.organisation_id == organisation_id)
        ]
        return max(candidates, key=lambda item: item.version) if candidates else None

    async def list_definitions(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[WorkflowDefinition]:
        return [
            definition
            for definition in self.definitions
            if organisation_id is None or definition.organisation_id == organisation_id
        ]


class FakeOperations:
    def __init__(self, operations: Collection[DistributedOperation]) -> None:
        self.operations = list(operations)

    async def get(
        self,
        operation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> DistributedOperation | None:
        return next(
            (
                operation
                for operation in self.operations
                if operation.id == operation_id
                and (organisation_id is None or operation.organisation_id == organisation_id)
            ),
            None,
        )

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        bench_id: str | None = None,
        status: DistributedOperationStatus | None = None,
        limit: int = 500,
    ) -> list[DistributedOperation]:
        result = [
            operation
            for operation in self.operations
            if organisation_id is None or operation.organisation_id == organisation_id
        ]
        if agent_id is not None:
            result = [operation for operation in result if operation.agent_id == agent_id]
        if bench_id is not None:
            result = [operation for operation in result if operation.bench_id == bench_id]
        if status is not None:
            result = [operation for operation in result if operation.status is status]
        return result[:limit]


class FakeCi:
    def __init__(self, sessions: Collection[CiSession]) -> None:
        self.sessions = list(sessions)
        self.get_synchronizations: list[bool] = []
        self.details_calls = 0

    async def get(
        self,
        session_id: UUID,
        *,
        synchronize: bool = True,
        organisation_id: UUID | None = None,
    ) -> CiSession:
        self.get_synchronizations.append(synchronize)
        for session in self.sessions:
            if session.id == session_id and (
                organisation_id is None
                or session.organisation_id == organisation_id
                or (session.organisation_id is None and organisation_id == ORGANISATION_ID)
            ):
                return session
        raise CiSessionNotFoundError("CI session does not exist.", ci_session_id=str(session_id))

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> list[CiSession]:
        result = [
            session
            for session in self.sessions
            if organisation_id is None
            or session.organisation_id == organisation_id
            or (session.organisation_id is None and organisation_id == ORGANISATION_ID)
        ]
        if status is not None:
            result = [session for session in result if session.status is status]
        if provider is not None:
            result = [session for session in result if session.provider is provider]
        return result[:limit]

    async def details(
        self,
        session_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> dict[str, object]:
        self.details_calls += 1
        session = await self.get(
            session_id,
            synchronize=True,
            organisation_id=organisation_id,
        )
        return {**session.model_dump(mode="json"), "details": True}


def _context(*, restrictions: set[str] | None = None) -> AuthenticationContext:
    return AuthenticationContext(
        principal=Principal(
            id=PRINCIPAL_ID,
            type=PrincipalType.USER,
            organisation_id=ORGANISATION_ID,
            display_name="Alice",
        ),
        permission_restrictions=restrictions,
    )


def _agent(agent_id: UUID, slug: str) -> AgentRecord:
    return AgentRecord(
        id=agent_id,
        organisation_id=ORGANISATION_ID,
        slug=slug,
        name=slug,
        status=AgentStatus.ONLINE,
        version="1.0",
        protocol_version="1.0",
        registered_at=NOW,
    )


def _bench(agent: AgentRecord, local_id: str) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=f"{agent.slug}/{local_id}",
        organisation_id=agent.organisation_id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=local_id,
        name=local_id,
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe"}),
        created_at=NOW,
        updated_at=NOW,
    )


def _workflow(name: str, *, organisation_id: UUID = ORGANISATION_ID) -> WorkflowDefinition:
    return WorkflowDefinition(
        organisation_id=organisation_id,
        name=name,
        version=1,
        requirements=WorkflowRequirements(),
        steps=[WaitWorkflowStep(action="wait", seconds=1)],
    )


def _operation(
    bench: GlobalBenchRecord,
    *,
    agent_id: UUID | None = None,
) -> DistributedOperation:
    return DistributedOperation(
        organisation_id=bench.organisation_id,
        remote_command_id=uuid4(),
        agent_id=agent_id or bench.agent_id,
        bench_id=bench.id,
        operation_type="probe",
        created_at=NOW,
    )


def _session(principal_id: UUID) -> CiSession:
    return CiSession(
        provider=CiProvider.LOCAL,
        external_run_id=str(uuid4()),
        requested_by="CI",
        organisation_id=ORGANISATION_ID,
        requested_by_principal_id=principal_id,
        requested_by_principal_type=PrincipalType.USER,
        created_at=NOW,
    )


def _service(
    authorisation: FakeAuthorisation,
    *,
    agents: Collection[AgentRecord] = (),
    benches: Collection[GlobalBenchRecord] = (),
    workflows: Collection[WorkflowDefinition] = (),
    operations: Collection[DistributedOperation] = (),
    sessions: Collection[CiSession] = (),
    hide: bool = True,
) -> tuple[
    OperationalAccessService,
    FakeAgents,
    FakeWorkflows,
    FakeCi,
]:
    agent_catalog = FakeAgents(agents)
    workflow_catalog = FakeWorkflows(workflows)
    ci_catalog = FakeCi(sessions)
    return (
        OperationalAccessService(
            agent_catalog,
            FakeBenches(benches),
            workflow_catalog,
            FakeOperations(operations),
            ci_catalog,
            authorisation,
            hide_unauthorised_resources=hide,
        ),
        agent_catalog,
        workflow_catalog,
        ci_catalog,
    )


def test_operational_boundary_rejects_missing_conflicting_and_cross_tenant_modes() -> None:
    async def scenario() -> None:
        service, agents, _, _ = _service(FakeAuthorisation())
        with pytest.raises(AuthenticationRequiredError):
            await service.list_agents()
        with pytest.raises(ValueError, match="mutually exclusive"):
            await service.list_agents(
                allow_legacy_authorisation=True,
                allow_internal_authorisation=True,
            )
        with pytest.raises(ValueError, match="cannot carry"):
            await service.list_agents(
                authentication_context=_context(),
                allow_legacy_authorisation=True,
            )
        with pytest.raises(ValueError, match="does not match"):
            await service.list_agents(
                authentication_context=_context(),
                organisation_id=OTHER_ORGANISATION_ID,
            )
        assert agents.reads == 0

    asyncio.run(scenario())


def test_agent_and_bench_reads_gate_filter_exact_resources_and_honour_restrictions() -> None:
    async def scenario() -> None:
        first = _agent(UUID(int=10), "first")
        second = _agent(UUID(int=11), "second")
        first_bench = _bench(first, "one")
        second_bench = _bench(second, "two")
        authorisation = FakeAuthorisation(
            anywhere={"agents:read", "benches:read"},
            allowed={
                ("agents:read", ResourceType.AGENT, str(first.id)),
                ("benches:read", ResourceType.BENCH, first_bench.id),
            },
        )
        service, agents, _, _ = _service(
            authorisation,
            agents=(first, second),
            benches=(first_bench, second_bench),
        )
        context = _context(restrictions={"agents:read", "benches:read"})

        assert await service.list_agents(authentication_context=context) == [first]
        assert await service.list_benches(authentication_context=context) == [first_bench]
        bench_call = next(
            call for call in authorisation.is_allowed_calls if call[1].id == first_bench.id
        )
        assert bench_call[1].parent_agent_id == first.id
        assert bench_call[2] == {"agents:read", "benches:read"}
        with pytest.raises(AgentNotFoundError):
            await service.get_agent(second.id, authentication_context=context)
        with pytest.raises(BenchNotFoundError):
            await service.get_bench(second_bench.id, authentication_context=context)

        reads_before = agents.reads
        with pytest.raises(PermissionDeniedError):
            await service.list_agents(
                authentication_context=_context(restrictions={"benches:read"})
            )
        assert agents.reads == reads_before
        assert await service.list_agents(allow_legacy_authorisation=True) == [first, second]

        visible_denials, _, _, _ = _service(
            authorisation,
            agents=(first, second),
            hide=False,
        )
        with pytest.raises(PermissionDeniedError):
            await visible_denials.get_agent(second.id, authentication_context=context)

    asyncio.run(scenario())


def test_bench_collection_admits_exact_policy_grant_not_visible_to_anywhere_probe() -> None:
    async def scenario() -> None:
        agent = _agent(UUID(int=12), "policy-agent")
        bench = _bench(agent, "policy-bench")
        authorisation = FakeAuthorisation(
            allowed={("benches:read", ResourceType.BENCH, bench.id)},
        )
        service, _, _, _ = _service(
            authorisation,
            agents=(agent,),
            benches=(bench,),
        )

        assert await service.list_benches(
            authentication_context=_context(restrictions={"benches:read"})
        ) == [bench]
        assert authorisation.require_anywhere_calls == []
        exact = [
            resource
            for permission, resource, _ in authorisation.is_allowed_calls
            if permission == "benches:read"
        ]
        assert exact
        assert all(resource.parent_agent_id == agent.id for resource in exact)

    asyncio.run(scenario())


def test_bench_collection_applies_trusted_parent_agent_location_and_label_filters() -> None:
    async def scenario() -> None:
        selected = _agent(UUID(int=13), "selected")
        selected = selected.model_copy(
            update={"location": "rack-a", "labels": {"site": "north", "pool": "esp32"}}
        )
        excluded = _agent(UUID(int=14), "excluded").model_copy(
            update={"location": "rack-b", "labels": {"site": "south", "pool": "esp32"}}
        )
        selected_bench = _bench(selected, "one")
        excluded_bench = _bench(excluded, "two")
        authorisation = FakeAuthorisation(
            anywhere={"benches:read"},
            allowed={
                ("benches:read", ResourceType.BENCH, selected_bench.id),
                ("benches:read", ResourceType.BENCH, excluded_bench.id),
            },
        )
        service, _, _, _ = _service(
            authorisation,
            agents=(selected, excluded),
            benches=(selected_bench, excluded_bench),
        )

        assert await service.list_benches(
            location="rack-a",
            agent_labels={"site": "north", "pool": "esp32"},
            authentication_context=_context(restrictions={"benches:read"}),
        ) == [selected_bench]

    asyncio.run(scenario())


def test_visible_bench_collection_has_no_admission_and_honours_narrowed_credentials() -> None:
    async def scenario() -> None:
        agent = _agent(UUID(int=15), "summary-agent")
        bench = _bench(agent, "summary-bench")
        authorisation = FakeAuthorisation(
            allowed={("benches:read", ResourceType.BENCH, bench.id)},
        )
        service, _, _, _ = _service(
            authorisation,
            agents=(agent,),
            benches=(bench,),
        )

        narrowed = _context(restrictions={"agents:read"})
        assert (
            await service.list_visible_benches(
                agent_id=agent.id,
                authentication_context=narrowed,
            )
            == []
        )
        assert authorisation.require_anywhere_calls == []
        with pytest.raises(PermissionDeniedError):
            await service.list_benches(
                agent_id=agent.id,
                authentication_context=narrowed,
            )
        assert await service.list_visible_benches(
            agent_id=agent.id,
            allow_legacy_authorisation=True,
        ) == [bench]

        assert await service.list_visible_benches(
            agent_id=agent.id,
            authentication_context=_context(restrictions={"benches:read"}),
        ) == [bench]

    asyncio.run(scenario())


def test_workflow_catalog_forces_identity_tenant_filters_and_audits_registration() -> None:
    async def scenario() -> None:
        visible = _workflow("visible")
        hidden = _workflow("hidden")
        authorisation = FakeAuthorisation(
            anywhere={"workflows:read"},
            allowed={
                ("workflows:manage", ResourceType.ORGANISATION, str(ORGANISATION_ID)),
                ("workflows:read", ResourceType.WORKFLOW, visible.name),
            },
        )
        service, _, workflows, _ = _service(
            authorisation,
            workflows=(visible, hidden),
        )
        context = _context(restrictions={"workflows:read", "workflows:manage"})
        incoming = _workflow("new-workflow", organisation_id=OTHER_ORGANISATION_ID)

        stored = await service.save_workflow(incoming, authentication_context=context)
        assert stored.organisation_id == ORGANISATION_ID
        assert workflows.saved == [stored]
        assert authorisation.successes == [
            (
                "WORKFLOW_REGISTERED",
                ResourceType.WORKFLOW.value,
                stored.name,
                {"version": stored.version},
            )
        ]
        assert await service.list_workflows(authentication_context=context) == [visible]
        assert (
            await service.get_workflow(
                visible.name,
                authentication_context=context,
            )
            == visible
        )
        with pytest.raises(WorkflowNotFoundError):
            await service.get_workflow(hidden.name, authentication_context=context)

        denied_authorisation = FakeAuthorisation()
        denied_service, _, denied_workflows, _ = _service(denied_authorisation)
        with pytest.raises(PermissionDeniedError):
            await denied_service.save_workflow(
                incoming,
                authentication_context=context,
            )
        assert denied_workflows.saved == []
        assert denied_authorisation.successes == []

    asyncio.run(scenario())


def test_operations_resolve_and_validate_the_exact_bench_route() -> None:
    async def scenario() -> None:
        agent = _agent(UUID(int=20), "route")
        bench = _bench(agent, "bench")
        valid = _operation(bench)
        corrupt = _operation(bench, agent_id=UUID(int=999))
        authorisation = FakeAuthorisation(
            anywhere={"operations:read"},
            allowed={("operations:read", ResourceType.BENCH, bench.id)},
        )
        service, _, _, _ = _service(
            authorisation,
            agents=(agent,),
            benches=(bench,),
            operations=(valid, corrupt),
        )
        context = _context(restrictions={"operations:read"})

        assert await service.list_operations(authentication_context=context) == [valid]
        assert await service.get_operation(valid.id, authentication_context=context) == valid
        resource = next(
            call[1] for call in authorisation.is_allowed_calls if call[0] == "operations:read"
        )
        assert resource.id == bench.id
        assert resource.parent_agent_id == agent.id
        with pytest.raises(RemoteCommandNotFoundError):
            await service.get_operation(corrupt.id, authentication_context=context)
        assert (
            "operations:read",
            ResourceType.BENCH.value,
            bench.id,
        ) in authorisation.denials

    asyncio.run(scenario())


def test_ci_reads_preserve_owner_or_organisation_semantics_and_authorise_before_sync() -> None:
    async def scenario() -> None:
        own = _session(PRINCIPAL_ID)
        other = _session(OTHER_PRINCIPAL_ID)
        authorisation = FakeAuthorisation(anywhere={"ci:sessions:read"})
        service, _, _, ci = _service(
            authorisation,
            sessions=(own, other),
        )
        context = _context(restrictions={"ci:sessions:read"})

        assert await service.list_ci_sessions(authentication_context=context) == [own]
        assert (
            await service.get_ci_session(
                own.id,
                authentication_context=context,
            )
            == own
        )
        assert ci.get_synchronizations[-2:] == [False, True]
        with pytest.raises(CiSessionNotFoundError):
            await service.get_ci_session(other.id, authentication_context=context)
        assert ci.get_synchronizations[-1] is False
        with pytest.raises(CiSessionNotFoundError):
            await service.get_ci_session_details(other.id, authentication_context=context)
        assert ci.details_calls == 0

        authorisation.allowed.add(
            ("ci:sessions:read", ResourceType.ORGANISATION, str(ORGANISATION_ID))
        )
        details = await service.get_ci_session_details(
            other.id,
            authentication_context=context,
        )
        assert details["details"] is True
        assert ci.details_calls == 1
        assert await service.list_ci_sessions(authentication_context=context) == [own, other]

    asyncio.run(scenario())


def test_default_organisation_admin_can_read_migrated_legacy_ci_row() -> None:
    async def scenario() -> None:
        legacy = CiSession(
            provider=CiProvider.LOCAL,
            external_run_id="legacy-run",
            requested_by="legacy-ci",
            organisation_id=None,
            created_at=NOW,
        )
        authorisation = FakeAuthorisation(
            anywhere={"ci:sessions:read"},
            allowed={
                (
                    "ci:sessions:read",
                    ResourceType.ORGANISATION,
                    str(ORGANISATION_ID),
                )
            },
        )
        service, _, _, _ = _service(authorisation, sessions=(legacy,))
        context = _context(restrictions={"ci:sessions:read"})

        assert await service.list_ci_sessions(authentication_context=context) == [legacy]
        assert (
            await service.get_ci_session(
                legacy.id,
                synchronize=False,
                authentication_context=context,
            )
            == legacy
        )

    asyncio.run(scenario())
