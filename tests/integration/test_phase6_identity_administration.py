from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import (
    BenchNotFoundError,
    PermissionDeniedError,
    RoleNotAllowedError,
    SessionRevokedError,
    TeamNotFoundError,
)
from lab_platform.core.identity import IdentityAuthenticationService
from lab_platform.core.identity_admin import IdentityAdministrationService
from lab_platform.core.workflows import WorkflowNotFoundError
from lab_platform.models import (
    AuthenticationSource,
    AuthorisationResource,
    BenchVisibility,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationRole,
    ResourceType,
    RoleName,
    RoleSubjectType,
    ServiceAccountStatus,
    TeamRole,
    UserStatus,
    WorkflowDefinition,
    WorkflowVisibility,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.identity import SQLiteIdentityRepository

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


class StaticBenchDirectory:
    def __init__(self, bench: GlobalBenchRecord) -> None:
        self.bench = bench

    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None:
        if bench_id != self.bench.id or organisation_id != self.bench.organisation_id:
            return None
        return self.bench


class StaticWorkflowCatalog:
    def __init__(self, workflow: WorkflowDefinition) -> None:
        self.workflow = workflow

    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None:
        if version not in {None, self.workflow.version}:
            return None
        if name != self.workflow.name or organisation_id != self.workflow.organisation_id:
            return None
        return self.workflow


def test_identity_administration_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "identity-admin.db")
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        await repository.ensure_default_organisation(slug="default", name="Example Lab")
        authentication = IdentityAuthenticationService(repository, clock=lambda: NOW)
        authorisation = AuthorisationService(
            repository,
            audit_repository=repository,
            clock=lambda: NOW,
        )
        administration = IdentityAdministrationService(
            repository,
            authentication,
            authorisation,
            clock=lambda: NOW,
        )

        organisation, owner = await administration.bootstrap_admin(
            organisation_slug="default",
            organisation_name="Example Lab",
            username="owner",
            display_name="Lab Owner",
            password="correct horse battery staple",
        )
        with pytest.raises(RoleNotAllowedError, match="already exists"):
            await administration.bootstrap_admin(
                organisation_slug="default",
                organisation_name="Example Lab",
                username="other-owner",
                display_name="Other Owner",
                password="correct horse battery staple",
            )

        owner_login = await authentication.login(
            organisation_slug="default",
            username="owner",
            password="correct horse battery staple",
        )
        owner_context = await authentication.authenticate_session(owner_login.access_token)
        assert owner_context.principal.id == owner.id

        alice = await administration.create_user(
            owner_context,
            username="alice",
            display_name="Alice Operator",
            password="alice password is long enough",
            organisation_role=OrganisationRole.MEMBER,
        )
        with pytest.raises(ValueError, match="required for a local user"):
            await administration.create_user(
                owner_context,
                username="missing-password",
                display_name="Missing Password",
            )
        with pytest.raises(ValueError, match="must not be supplied"):
            await administration.create_user(
                owner_context,
                username="invalid-oidc",
                display_name="Invalid OIDC",
                password="password that should not be accepted",
                authentication_source=AuthenticationSource.OIDC,
            )
        oidc_user = await administration.create_user(
            owner_context,
            username="oidc-user",
            display_name="OIDC User",
            authentication_source=AuthenticationSource.OIDC,
            organisation_role=OrganisationRole.VIEWER,
        )
        assert oidc_user.authentication_source is AuthenticationSource.OIDC
        assert await repository.get_password_credential(oidc_user.id) is None
        oidc_membership = await repository.get_organisation_membership(
            organisation.id,
            oidc_user.id,
        )
        assert oidc_membership is not None
        assert oidc_membership.role is OrganisationRole.VIEWER
        with pytest.raises(RoleNotAllowedError, match="local users"):
            await administration.reset_password(
                owner_context,
                oidc_user.id,
                "password that should not be accepted",
            )
        team = await administration.create_team(
            owner_context,
            slug="embedded",
            name="Embedded",
        )
        membership = await administration.add_team_member(
            owner_context,
            team.id,
            alice.id,
            role=TeamRole.MEMBER,
        )
        assert membership.team_id == team.id

        assignment = await administration.assign_role(
            owner_context,
            subject_type=RoleSubjectType.TEAM,
            subject_id=team.id,
            role=RoleName.OPERATOR,
            resource_type=ResourceType.BENCH,
            resource_id="home-lab/esp32-01",
        )
        alice_login = await authentication.login(
            organisation_slug="default",
            username="alice",
            password="alice password is long enough",
        )
        alice_context = await authentication.authenticate_session(alice_login.access_token)
        assert await administration.list_user_team_memberships(owner_context, alice.id) == [
            (team, membership)
        ]
        team_members = await administration.list_team_members(owner_context, team.id)
        assert len(team_members) == 1
        assert team_members[0][0] == membership
        assert team_members[0][1].id == alice.id
        user_sessions = await administration.list_user_sessions(owner_context, alice.id)
        assert len(user_sessions) == 1
        assert user_sessions[0].user_id == alice.id
        assert await administration.list_role_assignments(
            owner_context,
            subject_type=RoleSubjectType.TEAM,
            subject_id=team.id,
            resource_type=ResourceType.BENCH,
            resource_id="home-lab/esp32-01",
        ) == (assignment,)
        bench = AuthorisationResource(
            type=ResourceType.BENCH,
            id="home-lab/esp32-01",
            organisation_id=organisation.id,
        )
        decision = await authorisation.evaluate(
            alice_context.principal,
            "benches:flash",
            bench,
        )
        assert decision.allowed
        assert decision.granting_assignment_ids == {assignment.id}

        account = await administration.create_service_account(
            owner_context,
            name="github-ci",
            description="Hardware workflow runner",
        )
        await administration.assign_role(
            owner_context,
            subject_type=RoleSubjectType.SERVICE_ACCOUNT,
            subject_id=account.id,
            role=RoleName.OPERATOR,
            resource_type=ResourceType.BENCH,
            resource_id=bench.id,
        )
        issued_credential = await administration.create_service_account_credential(
            owner_context,
            account.id,
            name="main",
            permission_restrictions={"benches:read"},
        )
        service_context = await authentication.authenticate_api_credential(issued_credential.token)
        assert await authorisation.is_allowed(
            service_context.principal,
            "benches:read",
            bench,
            credential_restrictions=service_context.permission_restrictions,
        )
        assert not await authorisation.is_allowed(
            service_context.principal,
            "benches:flash",
            bench,
            credential_restrictions=service_context.permission_restrictions,
        )

        await administration.reset_password(
            owner_context,
            alice.id,
            "alice replacement password",
        )
        with pytest.raises(SessionRevokedError):
            await authentication.authenticate_session(alice_login.access_token)

        disabled = await administration.set_user_status(
            owner_context,
            alice.id,
            UserStatus.DISABLED,
        )
        assert disabled.status is UserStatus.DISABLED
        revoked_account = await administration.set_service_account_status(
            owner_context,
            account.id,
            ServiceAccountStatus.REVOKED,
        )
        assert revoked_account.status is ServiceAccountStatus.REVOKED
        await administration.remove_team_member(owner_context, team.id, alice.id)
        await administration.revoke_role(owner_context, assignment.id)

        visible_audit = await administration.list_audit_events(owner_context, limit=100)
        actions = {event.action for event in visible_audit}
        assert {
            "USER_CREATED",
            "TEAM_CREATED",
            "TEAM_MEMBER_ADDED",
            "ROLE_ASSIGNED",
            "SERVICE_ACCOUNT_CREATED",
            "CREDENTIAL_CREATED",
            "USER_PASSWORD_RESET",
            "PERMISSION_DENIED",
        } - actions == {"PERMISSION_DENIED"}
        assert all(event.organisation_id == organisation.id for event in visible_audit)
        credential_created = next(
            event
            for event in visible_audit
            if event.action == "CREDENTIAL_CREATED"
            and event.resource_id == str(issued_credential.credential.id)
        )
        assert credential_created.actor_id == owner.id
        assert credential_created.actor_id != account.id

        secret_shaped_filter = f"lp_{uuid4().hex}_{'s' * 43}"
        assert (
            await administration.list_audit_events(
                owner_context,
                action=secret_shaped_filter,
                limit=7,
            )
            == []
        )
        viewed_events = await repository.list_audit_events(
            organisation.id,
            action="AUDIT_LOG_VIEWED",
        )
        filtered_view_event = next(
            event for event in viewed_events if event.metadata.get("requested_limit") == 7
        )
        assert filtered_view_event.metadata == {
            "result_count": 0,
            "actor_filter_applied": False,
            "action_filter_applied": True,
            "resource_filter_applied": False,
            "outcome_filter_applied": False,
            "correlation_filter_applied": False,
            "after_filter_applied": False,
            "before_filter_applied": False,
            "cursor_applied": False,
            "requested_limit": 7,
        }
        assert secret_shaped_filter not in filtered_view_event.model_dump_json()
        database.close()

    asyncio.run(scenario())


def test_final_owner_cannot_be_disabled(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "last-owner.db")
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        authentication = IdentityAuthenticationService(repository, clock=lambda: NOW)
        authorisation = AuthorisationService(repository, clock=lambda: NOW)
        administration = IdentityAdministrationService(
            repository,
            authentication,
            authorisation,
            clock=lambda: NOW,
        )
        _organisation, owner = await administration.bootstrap_admin(
            organisation_slug="owners",
            organisation_name="Owners",
            username="owner",
            display_name="Owner",
            password="correct horse battery staple",
        )
        login = await authentication.login(
            organisation_slug="owners",
            username="owner",
            password="correct horse battery staple",
        )
        context = await authentication.authenticate_session(login.access_token)
        with pytest.raises(RoleNotAllowedError, match="final organisation owner"):
            await administration.set_user_status(context, owner.id, UserStatus.DISABLED)
        database.close()

    asyncio.run(scenario())


def test_access_policy_administration_uses_trusted_resources_and_live_policy(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "access-policy-admin.db")
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        authentication = IdentityAuthenticationService(repository, clock=lambda: NOW)
        authorisation = AuthorisationService(
            repository,
            audit_repository=repository,
            policy_repository=repository,
            clock=lambda: NOW,
        )
        bootstrap = IdentityAdministrationService(
            repository,
            authentication,
            authorisation,
            clock=lambda: NOW,
        )
        organisation, _owner = await bootstrap.bootstrap_admin(
            organisation_slug="policy-lab",
            organisation_name="Policy Lab",
            username="owner",
            display_name="Owner",
            password="correct horse battery staple",
        )
        agent_id = UUID(int=901)
        bench = GlobalBenchRecord(
            id="policy-agent/managed",
            organisation_id=organisation.id,
            agent_id=agent_id,
            agent_slug="policy-agent",
            local_bench_id="managed",
            name="Managed Bench",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.OFFLINE,
            health=HealthStatus.UNHEALTHY,
            capabilities=frozenset({"probe"}),
            created_at=NOW,
            updated_at=NOW,
            last_seen_at=NOW,
        )
        workflow = WorkflowDefinition.model_validate(
            {
                "organisation_id": organisation.id,
                "name": "policy-probe",
                "version": 1,
                "requirements": {"capabilities": ["probe"]},
                "steps": [{"action": "probe"}],
            }
        )
        administration = IdentityAdministrationService(
            repository,
            authentication,
            authorisation,
            bench_directory=StaticBenchDirectory(bench),
            workflow_catalog=StaticWorkflowCatalog(workflow),
            clock=lambda: NOW,
        )
        owner_login = await authentication.login(
            organisation_slug="policy-lab",
            username="owner",
            password="correct horse battery staple",
        )
        owner_context = await authentication.authenticate_session(owner_login.access_token)

        team = await administration.create_team(
            owner_context,
            slug="readers",
            name="Readers",
        )
        reader = await administration.create_user(
            owner_context,
            username="reader",
            display_name="Reader",
            password="reader password is sufficiently long",
            organisation_role=OrganisationRole.MEMBER,
        )
        await administration.add_team_member(owner_context, team.id, reader.id)
        reader_login = await authentication.login(
            organisation_slug="policy-lab",
            username="reader",
            password="reader password is sufficiently long",
        )
        reader_context = await authentication.authenticate_session(reader_login.access_token)
        resource = AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=organisation.id,
            parent_agent_id=bench.agent_id,
        )
        assert not await authorisation.is_allowed(
            reader_context.principal,
            "benches:read",
            resource,
        )

        default_bench = await administration.get_bench_access_policy(owner_context, bench.id)
        assert not default_bench.configured
        assert default_bench.policy.visibility is BenchVisibility.ORGANISATION
        default_workflow = await administration.get_workflow_access_policy(
            owner_context,
            workflow.name,
        )
        assert not default_workflow.configured
        assert default_workflow.policy.visibility is WorkflowVisibility.ORGANISATION

        configured = await administration.set_bench_access_policy(
            owner_context,
            bench.id,
            visibility=BenchVisibility.RESTRICTED,
            reservation_role=RoleName.RESERVER,
            operation_role=RoleName.OPERATOR,
            allowed_team_ids={team.id},
        )
        assert configured.configured
        assert configured.policy.allowed_team_ids == {team.id}
        assert await authorisation.is_allowed(
            reader_context.principal,
            "benches:read",
            resource,
        )

        scoped_admin = await administration.create_user(
            owner_context,
            username="scoped-admin",
            display_name="Scoped Admin",
            password="scoped admin password is long enough",
            organisation_role=OrganisationRole.MEMBER,
        )
        await administration.assign_role(
            owner_context,
            subject_type=RoleSubjectType.USER,
            subject_id=scoped_admin.id,
            role=RoleName.LAB_ADMIN,
            resource_type=ResourceType.AGENT,
            resource_id=str(agent_id),
        )
        await administration.assign_role(
            owner_context,
            subject_type=RoleSubjectType.USER,
            subject_id=scoped_admin.id,
            role=RoleName.LAB_ADMIN,
            resource_type=ResourceType.WORKFLOW,
            resource_id=workflow.name,
        )
        scoped_login = await authentication.login(
            organisation_slug="policy-lab",
            username="scoped-admin",
            password="scoped admin password is long enough",
        )
        scoped_context = await authentication.authenticate_session(scoped_login.access_token)
        assert (await administration.get_bench_access_policy(scoped_context, bench.id)).configured
        workflow_result = await administration.set_workflow_access_policy(
            scoped_context,
            workflow.name,
            visibility=WorkflowVisibility.RESTRICTED,
        )
        assert workflow_result.configured
        assert workflow_result.policy.visibility is WorkflowVisibility.RESTRICTED

        narrowed_context = scoped_context.model_copy(
            update={"permission_restrictions": {"benches:read"}}
        )
        with pytest.raises(PermissionDeniedError):
            await administration.get_bench_access_policy(narrowed_context, bench.id)
        with pytest.raises(TeamNotFoundError):
            await administration.set_bench_access_policy(
                owner_context,
                bench.id,
                visibility=BenchVisibility.RESTRICTED,
                allowed_team_ids={UUID(int=999)},
            )
        with pytest.raises(BenchNotFoundError):
            await administration.get_bench_access_policy(owner_context, "missing/bench")
        with pytest.raises(WorkflowNotFoundError):
            await administration.get_workflow_access_policy(owner_context, "missing-workflow")

        bench_events = await repository.list_audit_events(
            organisation.id,
            action="BENCH_ACCESS_POLICY_UPDATED",
        )
        assert len(bench_events) == 1
        assert bench_events[0].resource_id == bench.id
        workflow_events = await repository.list_audit_events(
            organisation.id,
            action="WORKFLOW_ACCESS_POLICY_UPDATED",
        )
        assert len(workflow_events) == 1
        assert workflow_events[0].actor_id == scoped_admin.id
        database.close()

    asyncio.run(scenario())
