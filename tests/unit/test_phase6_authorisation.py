from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.core.authorisation import (
    ALL_PERMISSIONS,
    ORGANISATION_ROLE_PERMISSIONS,
    ROLE_PERMISSIONS,
    AuthorisationService,
)
from lab_platform.core.errors import PermissionDeniedError
from lab_platform.models import (
    AuditEvent,
    AuthorisationResource,
    BenchAccessPolicy,
    BenchVisibility,
    OrganisationMembership,
    OrganisationRole,
    Principal,
    PrincipalType,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    WorkflowAccessPolicy,
    WorkflowVisibility,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
ORGANISATION_ID = UUID(int=1)
OTHER_ORGANISATION_ID = UUID(int=2)
USER_ID = UUID(int=3)
SERVICE_ACCOUNT_ID = UUID(int=4)
TEAM_ID = UUID(int=5)
AGENT_ID = UUID(int=6)
OTHER_AGENT_ID = UUID(int=7)
CREATOR_ID = UUID(int=8)


class MemoryAuthorisationRepository:
    def __init__(
        self,
        *,
        membership: OrganisationMembership | None = None,
        team_ids: Collection[UUID] = (),
        assignments: Sequence[RoleAssignment] = (),
    ) -> None:
        self.membership = membership
        self.team_ids = tuple(team_ids)
        self.assignments = tuple(assignments)
        self.membership_calls = 0
        self.team_calls = 0
        self.assignment_calls = 0
        self.requested_subjects: frozenset[tuple[RoleSubjectType, UUID]] = frozenset()

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None:
        self.membership_calls += 1
        if organisation_id != ORGANISATION_ID or user_id != USER_ID:
            return None
        return self.membership

    async def list_team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]:
        self.team_calls += 1
        if organisation_id != ORGANISATION_ID or user_id != USER_ID:
            return ()
        return self.team_ids

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]],
    ) -> Sequence[RoleAssignment]:
        self.assignment_calls += 1
        self.requested_subjects = frozenset(subjects)
        return self.assignments if organisation_id == ORGANISATION_ID else ()


class MemoryAuditRepository:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event


class MemoryPolicyRepository:
    def __init__(
        self,
        *,
        bench: BenchAccessPolicy | None = None,
        workflow: WorkflowAccessPolicy | None = None,
    ) -> None:
        self.bench = bench
        self.workflow = workflow
        self.bench_calls = 0
        self.workflow_calls = 0

    async def get_bench_access_policy(
        self,
        organisation_id: UUID,
        bench_id: str,
    ) -> BenchAccessPolicy | None:
        self.bench_calls += 1
        if organisation_id != ORGANISATION_ID:
            return None
        return self.bench if self.bench is not None and self.bench.bench_id == bench_id else None

    async def get_workflow_access_policy(
        self,
        organisation_id: UUID,
        workflow_id: str,
    ) -> WorkflowAccessPolicy | None:
        self.workflow_calls += 1
        if organisation_id != ORGANISATION_ID:
            return None
        return (
            self.workflow
            if self.workflow is not None and self.workflow.workflow_id == workflow_id
            else None
        )


def _principal(
    *,
    principal_type: PrincipalType = PrincipalType.USER,
    organisation_id: UUID = ORGANISATION_ID,
) -> Principal:
    return Principal(
        id=USER_ID if principal_type is PrincipalType.USER else SERVICE_ACCOUNT_ID,
        type=principal_type,
        organisation_id=organisation_id,
        display_name="Test principal",
    )


def _membership(role: OrganisationRole) -> OrganisationMembership:
    return OrganisationMembership(
        id=UUID(int=20),
        organisation_id=ORGANISATION_ID,
        user_id=USER_ID,
        role=role,
        created_at=NOW - timedelta(days=2),
    )


def _resource(
    resource_type: ResourceType = ResourceType.BENCH,
    resource_id: str = "home-lab/esp32-01",
    *,
    organisation_id: UUID = ORGANISATION_ID,
    parent_agent_id: UUID | None = AGENT_ID,
) -> AuthorisationResource:
    return AuthorisationResource(
        type=resource_type,
        id=resource_id,
        organisation_id=organisation_id,
        parent_agent_id=parent_agent_id,
    )


def _assignment(
    role: RoleName,
    resource_type: ResourceType,
    resource_id: str,
    *,
    assignment_id: int,
    subject_type: RoleSubjectType = RoleSubjectType.USER,
    subject_id: UUID = USER_ID,
    organisation_id: UUID = ORGANISATION_ID,
    created_at: datetime = NOW - timedelta(days=1),
    expires_at: datetime | None = None,
) -> RoleAssignment:
    return RoleAssignment(
        id=UUID(int=assignment_id),
        organisation_id=organisation_id,
        subject_type=subject_type,
        subject_id=subject_id,
        role=role,
        resource_type=resource_type,
        resource_id=resource_id,
        created_by=CREATOR_ID,
        created_at=created_at,
        expires_at=expires_at,
    )


@pytest.mark.parametrize(
    ("role", "can_view", "can_reserve", "can_flash", "can_manage_agent"),
    [
        (RoleName.VIEWER, True, False, False, False),
        (RoleName.RESERVER, True, True, False, False),
        (RoleName.OPERATOR, True, True, True, False),
        (RoleName.LAB_ADMIN, True, True, True, True),
    ],
)
def test_builtin_role_permission_matrix(
    role: RoleName,
    can_view: bool,
    can_reserve: bool,
    can_flash: bool,
    can_manage_agent: bool,
) -> None:
    permissions = ROLE_PERMISSIONS[role]
    assert ("benches:read" in permissions) is can_view
    assert ("benches:reserve" in permissions) is can_reserve
    assert ("benches:flash" in permissions) is can_flash
    assert ("agents:manage" in permissions) is can_manage_agent


def test_organisation_roles_are_explicit_and_conservative() -> None:
    assert ROLE_PERMISSIONS[RoleName.ORGANISATION_OWNER] == ALL_PERMISSIONS
    assert ORGANISATION_ROLE_PERMISSIONS[OrganisationRole.OWNER] == ALL_PERMISSIONS
    assert "organisation:manage" not in ORGANISATION_ROLE_PERMISSIONS[OrganisationRole.ADMIN]
    assert ORGANISATION_ROLE_PERMISSIONS[OrganisationRole.MEMBER] == {"organisation:read"}
    assert "audit:read" not in ORGANISATION_ROLE_PERMISSIONS[OrganisationRole.VIEWER]
    assert set(ROLE_PERMISSIONS) == set(RoleName)
    assert all(permissions <= ALL_PERMISSIONS for permissions in ROLE_PERMISSIONS.values())


def test_direct_and_team_assignments_union_only_on_matching_resources() -> None:
    async def scenario() -> None:
        direct = _assignment(
            RoleName.RESERVER,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=30,
        )
        team = _assignment(
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            "esp32-smoke-test",
            assignment_id=31,
            subject_type=RoleSubjectType.TEAM,
            subject_id=TEAM_ID,
        )
        repository = MemoryAuthorisationRepository(
            membership=_membership(OrganisationRole.MEMBER),
            team_ids={TEAM_ID},
            assignments=(direct, team),
        )
        service = AuthorisationService(repository, clock=lambda: NOW)

        bench = await service.evaluate(_principal(), "benches:reserve", _resource())
        assert bench.allowed
        assert bench.roles == {RoleName.RESERVER}
        assert bench.granting_assignment_ids == {direct.id}
        assert "organisation:read" in bench.permissions
        assert RoleName.WORKFLOW_RUNNER not in bench.roles

        workflow = await service.evaluate(
            _principal(),
            "workflows:run",
            _resource(ResourceType.WORKFLOW, "esp32-smoke-test", parent_agent_id=None),
        )
        assert workflow.allowed
        assert workflow.roles == {RoleName.WORKFLOW_RUNNER}
        assert workflow.granting_assignment_ids == {team.id}
        assert repository.requested_subjects == {
            (RoleSubjectType.USER, USER_ID),
            (RoleSubjectType.TEAM, TEAM_ID),
        }

    asyncio.run(scenario())


def test_permission_anywhere_allows_scoped_ci_allocation_but_honours_narrowing() -> None:
    async def scenario() -> None:
        workflow_assignment = _assignment(
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            "esp32-smoke-test",
            assignment_id=32,
        )
        repository = MemoryAuthorisationRepository(
            membership=_membership(OrganisationRole.MEMBER),
            assignments=(workflow_assignment,),
        )
        audit = MemoryAuditRepository()
        service = AuthorisationService(
            repository,
            audit_repository=audit,
            clock=lambda: NOW,
        )

        assert await service.is_allowed_anywhere(_principal(), "ci:sessions:create")
        assert not await service.is_allowed_anywhere(
            _principal(),
            "ci:sessions:create",
            credential_restrictions={"workflows:run"},
        )
        with pytest.raises(PermissionDeniedError):
            await service.require_anywhere(
                _principal(),
                "ci:sessions:create",
                credential_restrictions={"workflows:run"},
            )
        assert audit.events[-1].action == "PERMISSION_DENIED"
        assert audit.events[-1].metadata == {"required_permission": "ci:sessions:create"}

    asyncio.run(scenario())


def test_organisation_and_agent_assignments_inherit_through_trusted_hierarchy() -> None:
    async def scenario() -> None:
        organisation_assignment = _assignment(
            RoleName.VIEWER,
            ResourceType.ORGANISATION,
            str(ORGANISATION_ID),
            assignment_id=40,
        )
        agent_assignment = _assignment(
            RoleName.OPERATOR,
            ResourceType.AGENT,
            str(AGENT_ID),
            assignment_id=41,
        )
        repository = MemoryAuthorisationRepository(
            assignments=(organisation_assignment, agent_assignment)
        )
        service = AuthorisationService(repository, clock=lambda: NOW)

        inherited = await service.evaluate(_principal(), "benches:flash", _resource())
        assert inherited.allowed
        assert inherited.roles == {RoleName.VIEWER, RoleName.OPERATOR}
        assert inherited.granting_assignment_ids == {agent_assignment.id}

        spoofed_id = _resource(
            resource_id=f"{AGENT_ID}/looks-related",
            parent_agent_id=OTHER_AGENT_ID,
        )
        not_inherited = await service.evaluate(_principal(), "benches:flash", spoofed_id)
        assert not not_inherited.allowed
        assert not_inherited.roles == {RoleName.VIEWER}
        assert await service.is_allowed(_principal(), "benches:read", spoofed_id)

    asyncio.run(scenario())


def test_expired_future_and_wrong_subject_assignments_do_not_grant() -> None:
    async def scenario() -> None:
        expired = _assignment(
            RoleName.OPERATOR,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=50,
            expires_at=NOW - timedelta(seconds=1),
        )
        future = _assignment(
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=51,
            created_at=NOW + timedelta(seconds=1),
        )
        wrong_subject = _assignment(
            RoleName.OPERATOR,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=52,
            subject_id=UUID(int=99),
        )
        service = AuthorisationService(
            MemoryAuthorisationRepository(assignments=(expired, future, wrong_subject)),
            clock=lambda: NOW,
        )
        decision = await service.evaluate(_principal(), "benches:flash", _resource())
        assert not decision.allowed
        assert decision.roles == frozenset()
        assert decision.granting_assignment_ids == frozenset()

    asyncio.run(scenario())


def test_credential_restrictions_can_only_reduce_effective_permissions() -> None:
    async def scenario() -> None:
        operator = _assignment(
            RoleName.OPERATOR,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=60,
        )
        service = AuthorisationService(
            MemoryAuthorisationRepository(assignments=(operator,)),
            clock=lambda: NOW,
        )

        narrowed = await service.evaluate(
            _principal(),
            "benches:flash",
            _resource(),
            credential_restrictions={"benches:read", "users:manage"},
        )
        assert not narrowed.allowed
        assert narrowed.roles == {RoleName.OPERATOR}
        assert narrowed.permissions == {"benches:read"}
        assert narrowed.granting_assignment_ids == frozenset()
        assert not await service.is_allowed(
            _principal(),
            "users:manage",
            _resource(),
            credential_restrictions={"users:manage"},
        )

    asyncio.run(scenario())


def test_organisation_membership_roles_and_service_account_subjects() -> None:
    async def scenario() -> None:
        owner_repository = MemoryAuthorisationRepository(
            membership=_membership(OrganisationRole.OWNER)
        )
        owner_service = AuthorisationService(owner_repository, clock=lambda: NOW)
        owner = await owner_service.evaluate(_principal(), "users:manage", _resource())
        assert owner.allowed
        assert owner.roles == {RoleName.ORGANISATION_OWNER}
        assert owner.permissions == ALL_PERMISSIONS
        assert owner.granting_assignment_ids == frozenset()

        assignment = _assignment(
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            "ci-workflow",
            assignment_id=70,
            subject_type=RoleSubjectType.SERVICE_ACCOUNT,
            subject_id=SERVICE_ACCOUNT_ID,
        )
        service_repository = MemoryAuthorisationRepository(assignments=(assignment,))
        service = AuthorisationService(service_repository, clock=lambda: NOW)
        decision = await service.evaluate(
            _principal(principal_type=PrincipalType.SERVICE_ACCOUNT),
            "workflows:run",
            _resource(ResourceType.WORKFLOW, "ci-workflow", parent_agent_id=None),
        )
        assert decision.allowed
        assert service_repository.membership_calls == 0
        assert service_repository.team_calls == 0
        assert service_repository.requested_subjects == {
            (RoleSubjectType.SERVICE_ACCOUNT, SERVICE_ACCOUNT_ID)
        }

    asyncio.run(scenario())


def test_cross_organisation_access_is_denied_before_repository_queries() -> None:
    async def scenario() -> None:
        repository = MemoryAuthorisationRepository(membership=_membership(OrganisationRole.OWNER))
        service = AuthorisationService(repository, clock=lambda: NOW)
        decision = await service.evaluate(
            _principal(),
            "benches:read",
            _resource(organisation_id=OTHER_ORGANISATION_ID),
        )
        assert not decision.allowed
        assert decision.permissions == frozenset()
        assert repository.membership_calls == 0
        assert repository.team_calls == 0
        assert repository.assignment_calls == 0

    asyncio.run(scenario())


def test_request_scope_reuses_principal_and_policy_reads_across_large_permission_matrix() -> None:
    async def scenario() -> None:
        repository = MemoryAuthorisationRepository(membership=_membership(OrganisationRole.OWNER))
        policies = MemoryPolicyRepository()
        service = AuthorisationService(
            repository,
            policy_repository=policies,
            clock=lambda: NOW,
        )
        permissions = (
            "benches:read",
            "benches:reserve",
            "benches:operate",
            "benches:flash",
            "benches:reset",
            "benches:serial",
            "benches:manage",
        )

        with service.request_scope():
            for index in range(1_000):
                resource = _resource(resource_id=f"home-lab/bench-{index:04d}")
                for permission in permissions:
                    assert (await service.evaluate(_principal(), permission, resource)).allowed
                # An identical visibility check is served by the decision cache.
                assert (await service.evaluate(_principal(), "benches:read", resource)).allowed

        assert repository.membership_calls == 1
        assert repository.team_calls == 1
        assert repository.assignment_calls == 1
        assert policies.bench_calls == 1_000

        # No authorization data survives the explicit request boundary.
        assert (
            await service.evaluate(
                _principal(),
                "benches:read",
                _resource(resource_id="home-lab/bench-0000"),
            )
        ).allowed
        assert repository.membership_calls == 2
        assert policies.bench_calls == 1_001

    asyncio.run(scenario())


def test_restricted_and_private_bench_policies_remove_implicit_visibility() -> None:
    async def scenario() -> None:
        direct = _assignment(
            RoleName.VIEWER,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=80,
        )
        team = _assignment(
            RoleName.VIEWER,
            ResourceType.BENCH,
            "home-lab/esp32-01",
            assignment_id=81,
            subject_type=RoleSubjectType.TEAM,
            subject_id=TEAM_ID,
        )
        base = MemoryAuthorisationRepository(
            membership=_membership(OrganisationRole.VIEWER),
            team_ids={TEAM_ID},
            assignments=(team,),
        )
        restricted = AuthorisationService(
            base,
            policy_repository=MemoryPolicyRepository(
                bench=BenchAccessPolicy(
                    bench_id="home-lab/esp32-01",
                    visibility=BenchVisibility.RESTRICTED,
                )
            ),
            clock=lambda: NOW,
        )
        assert await restricted.is_allowed(_principal(), "benches:read", _resource())

        private = AuthorisationService(
            base,
            policy_repository=MemoryPolicyRepository(
                bench=BenchAccessPolicy(
                    bench_id="home-lab/esp32-01",
                    visibility=BenchVisibility.PRIVATE,
                )
            ),
            clock=lambda: NOW,
        )
        assert not await private.is_allowed(_principal(), "benches:read", _resource())

        direct_service = AuthorisationService(
            MemoryAuthorisationRepository(
                membership=_membership(OrganisationRole.MEMBER),
                assignments=(direct,),
            ),
            policy_repository=MemoryPolicyRepository(
                bench=BenchAccessPolicy(
                    bench_id="home-lab/esp32-01",
                    visibility=BenchVisibility.PRIVATE,
                )
            ),
            clock=lambda: NOW,
        )
        assert await direct_service.is_allowed(_principal(), "benches:read", _resource())

    asyncio.run(scenario())


def test_workflow_admin_only_policy_overrides_scoped_runner_assignment() -> None:
    async def scenario() -> None:
        workflow = _resource(
            ResourceType.WORKFLOW,
            "hardware-validation",
            parent_agent_id=None,
        )
        runner = _assignment(
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            workflow.id,
            assignment_id=82,
        )
        policies = MemoryPolicyRepository(
            workflow=WorkflowAccessPolicy(
                workflow_id=workflow.id,
                visibility=WorkflowVisibility.ADMIN_ONLY,
            )
        )
        member = AuthorisationService(
            MemoryAuthorisationRepository(
                membership=_membership(OrganisationRole.MEMBER),
                assignments=(runner,),
            ),
            policy_repository=policies,
            clock=lambda: NOW,
        )
        assert not await member.is_allowed(_principal(), "workflows:run", workflow)

        owner = AuthorisationService(
            MemoryAuthorisationRepository(membership=_membership(OrganisationRole.OWNER)),
            policy_repository=policies,
            clock=lambda: NOW,
        )
        assert await owner.is_allowed(_principal(), "workflows:run", workflow)

    asyncio.run(scenario())


def test_require_raises_structured_permission_denial_and_decision_is_frozen() -> None:
    async def scenario() -> None:
        audit = MemoryAuditRepository()
        service = AuthorisationService(
            MemoryAuthorisationRepository(),
            audit_repository=audit,
            clock=lambda: NOW,
        )
        resource = _resource()
        with pytest.raises(PermissionDeniedError) as captured:
            await service.require(_principal(), "benches:flash", resource)
        assert captured.value.details == {
            "required_permission": "benches:flash",
            "resource_type": "BENCH",
            "resource_id": "home-lab/esp32-01",
        }
        assert len(audit.events) == 1
        assert audit.events[0].action == "PERMISSION_DENIED"
        assert audit.events[0].outcome.value == "DENIED"
        assert audit.events[0].metadata == {"required_permission": "benches:flash"}

        decision = await service.evaluate(_principal(), "benches:read", resource)
        with pytest.raises(FrozenInstanceError):
            decision.allowed = True  # type: ignore[misc]

    asyncio.run(scenario())


def test_success_audit_is_bounded_to_identity_audit_configuration() -> None:
    async def scenario() -> None:
        audit = MemoryAuditRepository()
        enabled = AuthorisationService(
            MemoryAuthorisationRepository(),
            audit_repository=audit,
            clock=lambda: NOW,
        )
        event = await enabled.audit_success(
            _principal(),
            "BENCH_RESET_REQUESTED",
            resource_type="BENCH",
            resource_id="home-lab/esp32-01",
            metadata={"operation_id": "operation-1"},
        )
        assert event is audit.events[0]
        assert event.outcome.value == "SUCCEEDED"

        disabled = AuthorisationService(
            MemoryAuthorisationRepository(),
            audit_repository=audit,
            audit_enabled=False,
            clock=lambda: NOW,
        )
        assert (
            await disabled.audit_success(
                _principal(),
                "BENCH_RESET_REQUESTED",
                resource_type="BENCH",
                resource_id="home-lab/esp32-01",
            )
            is None
        )
        with pytest.raises(PermissionDeniedError):
            await disabled.require(_principal(), "benches:reset", _resource())
        assert len(audit.events) == 1

    asyncio.run(scenario())


def test_naive_authorisation_clock_is_rejected() -> None:
    async def scenario() -> None:
        service = AuthorisationService(
            MemoryAuthorisationRepository(),
            clock=lambda: datetime(2026, 8, 2, 12),
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.evaluate(_principal(), "benches:read", _resource())

    asyncio.run(scenario())
