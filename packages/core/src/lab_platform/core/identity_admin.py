from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lab_platform.core.authorisation import (
    ROLE_PERMISSIONS,
    AuthorisationDecision,
    AuthorisationService,
)
from lab_platform.core.errors import (
    AuditEventNotFoundError,
    BenchNotFoundError,
    PermissionDeniedError,
    RoleAssignmentConflictError,
    RoleAssignmentNotFoundError,
    RoleNotAllowedError,
    ServiceAccountNotFoundError,
    TeamAlreadyExistsError,
    TeamMembershipNotFoundError,
    TeamNotFoundError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
)
from lab_platform.core.identity import (
    IdentityAuthenticationService,
    IssuedApiCredential,
)
from lab_platform.core.workflows import WorkflowNotFoundError
from lab_platform.models import (
    ApiCredential,
    AuditEvent,
    AuditOutcome,
    AuthenticationContext,
    AuthenticationSource,
    AuthorisationResource,
    BenchAccessPolicy,
    BenchVisibility,
    GlobalBenchRecord,
    Organisation,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    Principal,
    PrincipalType,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    ServiceAccount,
    ServiceAccountStatus,
    Team,
    TeamMembership,
    TeamRole,
    User,
    UserSession,
    UserStatus,
    WorkflowAccessPolicy,
    WorkflowDefinition,
    WorkflowVisibility,
)


class IdentityAdministrationRepository(Protocol):
    async def create_organisation(self, organisation: Organisation) -> Organisation: ...

    async def get_organisation(self, organisation_id: UUID) -> Organisation | None: ...

    async def get_organisation_by_slug(self, slug: str) -> Organisation | None: ...

    async def update_organisation(self, organisation: Organisation) -> Organisation: ...

    async def create_user_with_password(
        self,
        user: User,
        password_credential: PasswordCredential,
        membership: OrganisationMembership,
    ) -> User: ...

    async def create_user_with_membership(
        self,
        user: User,
        membership: OrganisationMembership,
    ) -> User: ...

    async def get_user(self, organisation_id: UUID, user_id: UUID) -> User | None: ...

    async def get_user_by_username(
        self,
        organisation_id: UUID,
        username: str,
    ) -> User | None: ...

    async def list_users(
        self,
        organisation_id: UUID,
        *,
        status: UserStatus | None = None,
        limit: int = 500,
    ) -> list[User]: ...

    async def update_user(self, user: User) -> User: ...

    async def set_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential: ...

    async def set_organisation_membership(
        self,
        membership: OrganisationMembership,
    ) -> OrganisationMembership: ...

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None: ...

    async def count_organisation_owners(self, organisation_id: UUID) -> int: ...

    async def list_sessions(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> list[UserSession]: ...

    async def update_session(self, session: UserSession) -> UserSession: ...

    async def create_team(self, team: Team) -> Team: ...

    async def get_team(self, organisation_id: UUID, team_id: UUID) -> Team | None: ...

    async def get_team_by_slug(
        self,
        organisation_id: UUID,
        slug: str,
    ) -> Team | None: ...

    async def list_teams(self, organisation_id: UUID, *, limit: int = 500) -> list[Team]: ...

    async def update_team(self, team: Team) -> Team: ...

    async def delete_team(self, organisation_id: UUID, team_id: UUID) -> bool: ...

    async def create_team_membership(
        self,
        organisation_id: UUID,
        membership: TeamMembership,
    ) -> TeamMembership: ...

    async def list_team_memberships(
        self,
        organisation_id: UUID,
        team_id: UUID,
    ) -> list[TeamMembership]: ...

    async def delete_team_membership(
        self,
        organisation_id: UUID,
        team_id: UUID,
        user_id: UUID,
    ) -> bool: ...

    async def create_service_account(self, account: ServiceAccount) -> ServiceAccount: ...

    async def get_service_account(
        self,
        organisation_id: UUID,
        service_account_id: UUID,
    ) -> ServiceAccount | None: ...

    async def list_service_accounts(
        self,
        organisation_id: UUID,
        *,
        limit: int = 500,
    ) -> list[ServiceAccount]: ...

    async def update_service_account(self, service_account: ServiceAccount) -> ServiceAccount: ...

    async def revoke_service_account_and_credentials(
        self,
        service_account: ServiceAccount,
        revoked_at: datetime,
    ) -> tuple[ServiceAccount, list[ApiCredential]]: ...

    async def get_api_credential(
        self,
        organisation_id: UUID,
        credential_id: UUID,
    ) -> ApiCredential | None: ...

    async def list_api_credentials(
        self,
        organisation_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
    ) -> list[ApiCredential]: ...

    async def create_role_assignment(self, assignment: RoleAssignment) -> RoleAssignment: ...

    async def get_role_assignment(
        self,
        organisation_id: UUID,
        assignment_id: UUID,
    ) -> RoleAssignment | None: ...

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]] | None = None,
    ) -> Sequence[RoleAssignment]: ...

    async def delete_role_assignment(
        self,
        organisation_id: UUID,
        assignment_id: UUID,
    ) -> bool: ...

    async def set_bench_access_policy(
        self,
        organisation_id: UUID,
        policy: BenchAccessPolicy,
    ) -> BenchAccessPolicy: ...

    async def get_bench_access_policy(
        self,
        organisation_id: UUID,
        bench_id: str,
    ) -> BenchAccessPolicy | None: ...

    async def set_workflow_access_policy(
        self,
        organisation_id: UUID,
        policy: WorkflowAccessPolicy,
    ) -> WorkflowAccessPolicy: ...

    async def get_workflow_access_policy(
        self,
        organisation_id: UUID,
        workflow_id: str,
    ) -> WorkflowAccessPolicy | None: ...

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    async def get_audit_event(
        self,
        organisation_id: UUID,
        event_id: UUID,
    ) -> AuditEvent | None: ...

    async def list_audit_events(
        self,
        organisation_id: UUID,
        *,
        action: str | None = None,
        outcome: AuditOutcome | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 500,
    ) -> list[AuditEvent]: ...


class IdentityAdministrationBenchDirectory(Protocol):
    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None: ...


class IdentityAdministrationWorkflowCatalog(Protocol):
    async def get_definition(
        self,
        name: str,
        version: int | None = None,
        *,
        organisation_id: UUID | None = None,
    ) -> WorkflowDefinition | None: ...


@dataclass(frozen=True, slots=True)
class BenchAccessPolicyResult:
    policy: BenchAccessPolicy
    configured: bool


@dataclass(frozen=True, slots=True)
class WorkflowAccessPolicyResult:
    policy: WorkflowAccessPolicy
    configured: bool


class IdentityAdministrationService:
    """Permission-enforcing application service for identity administration."""

    def __init__(
        self,
        repository: IdentityAdministrationRepository,
        authentication: IdentityAuthenticationService,
        authorisation: AuthorisationService,
        *,
        bench_directory: IdentityAdministrationBenchDirectory | None = None,
        workflow_catalog: IdentityAdministrationWorkflowCatalog | None = None,
        default_bench_visibility: BenchVisibility = BenchVisibility.ORGANISATION,
        default_workflow_visibility: WorkflowVisibility = WorkflowVisibility.ORGANISATION,
        audit_enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._authentication = authentication
        self._authorisation = authorisation
        self._bench_directory = bench_directory
        self._workflow_catalog = workflow_catalog
        self._default_bench_visibility = default_bench_visibility
        self._default_workflow_visibility = default_workflow_visibility
        self._audit_enabled = audit_enabled
        self._clock = clock

    async def bootstrap_admin(
        self,
        *,
        organisation_slug: str,
        organisation_name: str,
        username: str,
        display_name: str,
        password: str,
        email: str | None = None,
        recovery: bool = False,
    ) -> tuple[Organisation, User]:
        now = self._clock()
        organisation = await self._repository.get_organisation_by_slug(organisation_slug)
        if organisation is None:
            organisation = await self._repository.create_organisation(
                Organisation(
                    slug=organisation_slug,
                    name=organisation_name,
                    created_at=now,
                    updated_at=now,
                )
            )
        owner_count = await self._repository.count_organisation_owners(organisation.id)
        if owner_count and not recovery:
            raise RoleNotAllowedError(
                "An organisation owner already exists; use explicit recovery mode if required."
            )
        existing = await self._repository.get_user_by_username(organisation.id, username)
        password_hash = self._authentication.hash_password(password)
        if existing is not None:
            if not recovery:
                raise UsernameAlreadyExistsError("The username already exists.")
            user = existing.model_copy(
                update={
                    "display_name": display_name,
                    "email": email,
                    "status": UserStatus.ACTIVE,
                    "authentication_source": AuthenticationSource.LOCAL,
                    "updated_at": now,
                }
            )
            await self._repository.update_user(user)
            await self._repository.set_password_credential(
                PasswordCredential(
                    user_id=user.id,
                    password_hash=password_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._repository.set_organisation_membership(
                OrganisationMembership(
                    organisation_id=organisation.id,
                    user_id=user.id,
                    role=OrganisationRole.OWNER,
                    created_at=now,
                )
            )
            await self._revoke_user_sessions(organisation.id, user.id, now)
        else:
            user = User(
                organisation_id=organisation.id,
                username=username,
                display_name=display_name,
                email=email,
                authentication_source=AuthenticationSource.LOCAL,
                created_at=now,
                updated_at=now,
            )
            await self._repository.create_user_with_password(
                user,
                PasswordCredential(
                    user_id=user.id,
                    password_hash=password_hash,
                    created_at=now,
                    updated_at=now,
                ),
                OrganisationMembership(
                    organisation_id=organisation.id,
                    user_id=user.id,
                    role=OrganisationRole.OWNER,
                    created_at=now,
                ),
            )
        await self._audit(
            organisation.id,
            actor=_principal(user),
            action="USER_CREATED" if existing is None else "USER_PASSWORD_RESET",
            resource_type="USER",
            resource_id=str(user.id),
        )
        return organisation, user

    async def get_organisation(self, context: AuthenticationContext) -> Organisation:
        await self._require(context, "organisation:read")
        organisation = await self._repository.get_organisation(context.principal.organisation_id)
        if organisation is None:
            raise PermissionDeniedError("The organisation is unavailable.")
        return organisation

    async def update_organisation(
        self,
        context: AuthenticationContext,
        *,
        name: str,
    ) -> Organisation:
        await self._require(context, "organisation:manage")
        organisation = await self.get_organisation(context)
        updated = organisation.model_copy(update={"name": name, "updated_at": self._clock()})
        result = await self._repository.update_organisation(updated)
        await self._audit_success(context, "ORGANISATION_UPDATED", "ORGANISATION", str(result.id))
        return result

    async def create_user(
        self,
        context: AuthenticationContext,
        *,
        username: str,
        display_name: str,
        password: str | None = None,
        email: str | None = None,
        organisation_role: OrganisationRole = OrganisationRole.MEMBER,
        authentication_source: AuthenticationSource = AuthenticationSource.LOCAL,
    ) -> User:
        await self._require(context, "users:manage")
        source = AuthenticationSource(authentication_source)
        if source is AuthenticationSource.LOCAL and password is None:
            raise ValueError("A password is required for a local user.")
        if source is AuthenticationSource.OIDC and password is not None:
            raise ValueError("A password must not be supplied for an OIDC user.")
        organisation_id = context.principal.organisation_id
        if await self._repository.get_user_by_username(organisation_id, username) is not None:
            raise UsernameAlreadyExistsError("The username already exists.")
        if organisation_role is OrganisationRole.OWNER:
            membership = await self._repository.get_organisation_membership(
                organisation_id,
                context.principal.id,
            )
            if membership is None or membership.role is not OrganisationRole.OWNER:
                raise RoleNotAllowedError("Only an organisation owner may create another owner.")
        now = self._clock()
        user = User(
            organisation_id=organisation_id,
            username=username,
            display_name=display_name,
            email=email,
            authentication_source=source,
            created_at=now,
            updated_at=now,
        )
        membership = OrganisationMembership(
            organisation_id=organisation_id,
            user_id=user.id,
            role=organisation_role,
            created_at=now,
        )
        if source is AuthenticationSource.LOCAL:
            assert password is not None
            await self._repository.create_user_with_password(
                user,
                PasswordCredential(
                    user_id=user.id,
                    password_hash=self._authentication.hash_password(password),
                    created_at=now,
                    updated_at=now,
                ),
                membership,
            )
        else:
            await self._repository.create_user_with_membership(user, membership)
        await self._audit_success(context, "USER_CREATED", "USER", str(user.id))
        return user

    async def list_users(self, context: AuthenticationContext) -> list[User]:
        await self._require(context, "users:read")
        return await self._repository.list_users(context.principal.organisation_id)

    async def get_user(self, context: AuthenticationContext, user_id: UUID) -> User:
        await self._require(context, "users:read")
        user = await self._repository.get_user(context.principal.organisation_id, user_id)
        if user is None:
            raise UserNotFoundError("The user does not exist.")
        return user

    async def update_user(
        self,
        context: AuthenticationContext,
        user_id: UUID,
        changes: Mapping[str, str | None],
    ) -> User:
        await self._require(context, "users:manage")
        unexpected = set(changes) - {"display_name", "email"}
        if unexpected or not changes:
            raise ValueError("User updates require display_name and/or email.")
        user = await self.get_user(context, user_id)
        payload = user.model_dump()
        payload.update(changes)
        payload["updated_at"] = self._clock()
        updated = User.model_validate(payload)
        result = await self._repository.update_user(updated)
        await self._audit_success(context, "USER_UPDATED", "USER", str(user.id))
        return result

    async def set_user_status(
        self,
        context: AuthenticationContext,
        user_id: UUID,
        status: UserStatus,
    ) -> User:
        await self._require(context, "users:manage")
        user = await self.get_user(context, user_id)
        membership = await self._repository.get_organisation_membership(
            user.organisation_id,
            user.id,
        )
        if (
            status is not UserStatus.ACTIVE
            and membership is not None
            and membership.role is OrganisationRole.OWNER
            and await self._repository.count_organisation_owners(user.organisation_id) <= 1
        ):
            raise RoleNotAllowedError("The final organisation owner cannot be disabled.")
        now = self._clock()
        updated = user.model_copy(update={"status": status, "updated_at": now})
        result = await self._repository.update_user(updated)
        if status is not UserStatus.ACTIVE:
            await self._revoke_user_sessions(user.organisation_id, user.id, now)
        action = "USER_ENABLED" if status is UserStatus.ACTIVE else "USER_DISABLED"
        await self._audit_success(context, action, "USER", str(user.id))
        return result

    async def reset_password(
        self,
        context: AuthenticationContext,
        user_id: UUID,
        password: str,
    ) -> None:
        await self._require(context, "users:manage")
        user = await self.get_user(context, user_id)
        if user.authentication_source is not AuthenticationSource.LOCAL:
            raise RoleNotAllowedError("Passwords may only be reset for local users.")
        now = self._clock()
        await self._repository.set_password_credential(
            PasswordCredential(
                user_id=user.id,
                password_hash=self._authentication.hash_password(password),
                created_at=now,
                updated_at=now,
            )
        )
        await self._revoke_user_sessions(user.organisation_id, user.id, now)
        await self._audit_success(context, "USER_PASSWORD_RESET", "USER", str(user.id))

    async def create_team(
        self,
        context: AuthenticationContext,
        *,
        slug: str,
        name: str,
        description: str | None = None,
    ) -> Team:
        await self._require(context, "teams:manage")
        if (
            await self._repository.get_team_by_slug(
                context.principal.organisation_id,
                slug,
            )
            is not None
        ):
            raise TeamAlreadyExistsError("The team slug already exists.")
        now = self._clock()
        team = Team(
            organisation_id=context.principal.organisation_id,
            slug=slug,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        result = await self._repository.create_team(team)
        await self._audit_success(context, "TEAM_CREATED", "TEAM", str(result.id))
        return result

    async def list_teams(self, context: AuthenticationContext) -> list[Team]:
        await self._require(context, "teams:read")
        return await self._repository.list_teams(context.principal.organisation_id)

    async def get_team(self, context: AuthenticationContext, team_id: UUID) -> Team:
        await self._require(context, "teams:read")
        team = await self._repository.get_team(context.principal.organisation_id, team_id)
        if team is None:
            raise TeamNotFoundError("The team does not exist.")
        return team

    async def update_team(
        self,
        context: AuthenticationContext,
        team_id: UUID,
        changes: Mapping[str, str | None],
    ) -> Team:
        await self._require(context, "teams:manage")
        unexpected = set(changes) - {"slug", "name", "description"}
        if unexpected or not changes:
            raise ValueError("Team updates require slug, name, and/or description.")
        team = await self.get_team(context, team_id)
        requested_slug = changes.get("slug")
        if requested_slug is not None and requested_slug.casefold() != team.slug.casefold():
            existing = await self._repository.get_team_by_slug(
                context.principal.organisation_id,
                requested_slug,
            )
            if existing is not None and existing.id != team.id:
                raise TeamAlreadyExistsError("The team slug already exists.")
        payload = team.model_dump()
        payload.update(changes)
        payload["updated_at"] = self._clock()
        updated = Team.model_validate(payload)
        result = await self._repository.update_team(updated)
        await self._audit_success(context, "TEAM_UPDATED", "TEAM", str(team.id))
        return result

    async def delete_team(self, context: AuthenticationContext, team_id: UUID) -> None:
        await self._require(context, "teams:manage")
        if not await self._repository.delete_team(context.principal.organisation_id, team_id):
            raise TeamNotFoundError("The team does not exist.")
        await self._audit_success(context, "TEAM_DELETED", "TEAM", str(team_id))

    async def add_team_member(
        self,
        context: AuthenticationContext,
        team_id: UUID,
        user_id: UUID,
        *,
        role: TeamRole = TeamRole.MEMBER,
    ) -> TeamMembership:
        await self._require(context, "teams:manage")
        await self.get_team(context, team_id)
        await self.get_user(context, user_id)
        membership = await self._repository.create_team_membership(
            context.principal.organisation_id,
            TeamMembership(team_id=team_id, user_id=user_id, role=role, created_at=self._clock()),
        )
        await self._audit_success(context, "TEAM_MEMBER_ADDED", "TEAM", str(team_id))
        return membership

    async def remove_team_member(
        self,
        context: AuthenticationContext,
        team_id: UUID,
        user_id: UUID,
    ) -> None:
        await self._require(context, "teams:manage")
        removed = await self._repository.delete_team_membership(
            context.principal.organisation_id,
            team_id,
            user_id,
        )
        if not removed:
            raise TeamMembershipNotFoundError("The team membership does not exist.")
        await self._audit_success(context, "TEAM_MEMBER_REMOVED", "TEAM", str(team_id))

    async def create_service_account(
        self,
        context: AuthenticationContext,
        *,
        name: str,
        description: str | None = None,
    ) -> ServiceAccount:
        await self._require(context, "service_accounts:manage")
        now = self._clock()
        account = ServiceAccount(
            organisation_id=context.principal.organisation_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        result = await self._repository.create_service_account(account)
        await self._audit_success(
            context,
            "SERVICE_ACCOUNT_CREATED",
            "SERVICE_ACCOUNT",
            str(result.id),
        )
        return result

    async def list_service_accounts(
        self,
        context: AuthenticationContext,
    ) -> list[ServiceAccount]:
        await self._require(context, "service_accounts:read")
        return await self._repository.list_service_accounts(context.principal.organisation_id)

    async def get_service_account(
        self,
        context: AuthenticationContext,
        account_id: UUID,
    ) -> ServiceAccount:
        await self._require(context, "service_accounts:read")
        account = await self._repository.get_service_account(
            context.principal.organisation_id,
            account_id,
        )
        if account is None:
            raise ServiceAccountNotFoundError("The service account does not exist.")
        return account

    async def set_service_account_status(
        self,
        context: AuthenticationContext,
        account_id: UUID,
        status: ServiceAccountStatus,
    ) -> ServiceAccount:
        await self._require(context, "service_accounts:manage")
        account = await self.get_service_account(context, account_id)
        if account.status is ServiceAccountStatus.REVOKED:
            if status is ServiceAccountStatus.REVOKED:
                return account
            raise RoleNotAllowedError("A revoked service account cannot be reactivated.")
        now = self._clock()
        updated = account.model_copy(update={"status": status, "updated_at": now})
        revoked_credentials: list[ApiCredential] = []
        if status is ServiceAccountStatus.REVOKED:
            (
                result,
                revoked_credentials,
            ) = await self._repository.revoke_service_account_and_credentials(updated, now)
        else:
            result = await self._repository.update_service_account(updated)
        action = (
            "SERVICE_ACCOUNT_DISABLED"
            if status is ServiceAccountStatus.DISABLED
            else "SERVICE_ACCOUNT_DELETED"
            if status is ServiceAccountStatus.REVOKED
            else "SERVICE_ACCOUNT_UPDATED"
        )
        await self._audit_success(context, action, "SERVICE_ACCOUNT", str(account_id))
        for credential in revoked_credentials:
            await self._audit_success(
                context,
                "CREDENTIAL_REVOKED",
                "API_CREDENTIAL",
                str(credential.id),
                metadata={"service_account_id": str(account_id)},
            )
        return result

    async def update_service_account_description(
        self,
        context: AuthenticationContext,
        account_id: UUID,
        description: str | None,
    ) -> ServiceAccount:
        await self._require(context, "service_accounts:manage")
        account = await self.get_service_account(context, account_id)
        if account.status is ServiceAccountStatus.REVOKED:
            raise RoleNotAllowedError("A revoked service account cannot be modified.")
        updated = account.model_copy(
            update={"description": description, "updated_at": self._clock()}
        )
        result = await self._repository.update_service_account(updated)
        await self._audit_success(
            context,
            "SERVICE_ACCOUNT_UPDATED",
            "SERVICE_ACCOUNT",
            str(account_id),
        )
        return result

    async def create_service_account_credential(
        self,
        context: AuthenticationContext,
        account_id: UUID,
        *,
        name: str,
        expires_at: datetime | None = None,
        allowed_ip_ranges: Sequence[str] = (),
        permission_restrictions: set[str] | None = None,
    ) -> IssuedApiCredential:
        await self._require(context, "credentials:create")
        account = await self.get_service_account(context, account_id)
        return await self._authentication.issue_api_credential(
            service_account=account,
            name=name,
            expires_at=expires_at,
            allowed_ip_ranges=allowed_ip_ranges,
            permission_restrictions=permission_restrictions,
            actor=context.principal,
        )

    async def list_service_account_credentials(
        self,
        context: AuthenticationContext,
        account_id: UUID,
    ) -> list[ApiCredential]:
        await self._require(context, "service_accounts:read")
        await self.get_service_account(context, account_id)
        return await self._repository.list_api_credentials(
            context.principal.organisation_id,
            PrincipalType.SERVICE_ACCOUNT,
            account_id,
        )

    async def revoke_credential(
        self,
        context: AuthenticationContext,
        credential_id: UUID,
    ) -> ApiCredential:
        await self._require(context, "credentials:revoke")
        credential = await self._repository.get_api_credential(
            context.principal.organisation_id,
            credential_id,
        )
        if credential is None:
            raise ServiceAccountNotFoundError("The API credential does not exist.")
        return await self._authentication.revoke_api_credential(
            credential,
            actor=context.principal,
        )

    async def assign_role(
        self,
        context: AuthenticationContext,
        *,
        subject_type: RoleSubjectType,
        subject_id: UUID,
        role: RoleName,
        resource_type: ResourceType,
        resource_id: str,
        expires_at: datetime | None = None,
    ) -> RoleAssignment:
        await self._require(context, "roles:manage")
        await self._validate_subject(context, subject_type, subject_id)
        if role is RoleName.ORGANISATION_OWNER:
            raise RoleNotAllowedError(
                "Organisation ownership is managed through organisation membership."
            )
        if resource_type is ResourceType.ORGANISATION:
            resource_id = str(context.principal.organisation_id)
        existing_assignments = await self._repository.list_role_assignments(
            context.principal.organisation_id,
            {(subject_type, subject_id)},
        )
        if any(
            existing.role is role
            and existing.resource_type is resource_type
            and existing.resource_id == resource_id
            for existing in existing_assignments
        ):
            raise RoleAssignmentConflictError("The role assignment already exists.")
        assignment = RoleAssignment(
            organisation_id=context.principal.organisation_id,
            subject_type=subject_type,
            subject_id=subject_id,
            role=role,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=context.principal.id,
            created_at=self._clock(),
            expires_at=expires_at,
        )
        result = await self._repository.create_role_assignment(assignment)
        await self._audit_success(context, "ROLE_ASSIGNED", "ROLE_ASSIGNMENT", str(result.id))
        return result

    async def revoke_role(self, context: AuthenticationContext, assignment_id: UUID) -> None:
        await self._require(context, "roles:manage")
        if not await self._repository.delete_role_assignment(
            context.principal.organisation_id,
            assignment_id,
        ):
            raise RoleAssignmentNotFoundError("The role assignment does not exist.")
        await self._audit_success(context, "ROLE_REVOKED", "ROLE_ASSIGNMENT", str(assignment_id))

    async def list_role_assignments(
        self,
        context: AuthenticationContext,
    ) -> Sequence[RoleAssignment]:
        await self._require(context, "roles:read")
        return await self._repository.list_role_assignments(
            context.principal.organisation_id,
            None,
        )

    async def list_roles(self, context: AuthenticationContext) -> tuple[RoleName, ...]:
        await self._require(context, "roles:read")
        return tuple(RoleName)

    async def get_role_permissions(
        self,
        context: AuthenticationContext,
        role: RoleName,
    ) -> frozenset[str]:
        await self._require(context, "roles:read")
        return ROLE_PERMISSIONS[role]

    async def effective_permissions(
        self,
        context: AuthenticationContext,
        resource: AuthorisationResource,
        permission: str = "organisation:read",
        *,
        subject_type: PrincipalType | None = None,
        subject_id: UUID | None = None,
    ) -> AuthorisationDecision:
        await self._require(context, "roles:read")
        principal = context.principal
        credential_restrictions = context.permission_restrictions
        if subject_type is not None and subject_id is not None:
            if subject_type is PrincipalType.USER:
                user = await self._repository.get_user(
                    context.principal.organisation_id,
                    subject_id,
                )
                if user is None:
                    raise UserNotFoundError("The user does not exist.")
                principal = _principal(user)
            else:
                account = await self._repository.get_service_account(
                    context.principal.organisation_id,
                    subject_id,
                )
                if account is None:
                    raise ServiceAccountNotFoundError("The service account does not exist.")
                principal = Principal(
                    id=account.id,
                    type=PrincipalType.SERVICE_ACCOUNT,
                    organisation_id=account.organisation_id,
                    display_name=account.name,
                )
            credential_restrictions = None
        return await self._authorisation.evaluate(
            principal,
            permission,
            resource,
            credential_restrictions=credential_restrictions,
        )

    async def get_bench_access_policy(
        self,
        context: AuthenticationContext,
        bench_id: str,
    ) -> BenchAccessPolicyResult:
        bench, resource = await self._bench_policy_resource(context, bench_id)
        await self._authorisation.require(
            context.principal,
            "benches:manage",
            resource,
            credential_restrictions=context.permission_restrictions,
        )
        configured = await self._repository.get_bench_access_policy(
            context.principal.organisation_id,
            bench.id,
        )
        if configured is not None:
            return BenchAccessPolicyResult(policy=configured, configured=True)
        return BenchAccessPolicyResult(
            policy=BenchAccessPolicy(
                bench_id=bench.id,
                visibility=self._default_bench_visibility,
            ),
            configured=False,
        )

    async def set_bench_access_policy(
        self,
        context: AuthenticationContext,
        bench_id: str,
        *,
        visibility: BenchVisibility,
        reservation_role: RoleName | None = None,
        operation_role: RoleName | None = None,
        allowed_team_ids: Collection[UUID] = (),
    ) -> BenchAccessPolicyResult:
        bench, resource = await self._bench_policy_resource(context, bench_id)
        await self._authorisation.require(
            context.principal,
            "benches:manage",
            resource,
            credential_restrictions=context.permission_restrictions,
        )
        organisation_id = context.principal.organisation_id
        team_ids = set(allowed_team_ids)
        for team_id in sorted(team_ids, key=str):
            if await self._repository.get_team(organisation_id, team_id) is None:
                raise TeamNotFoundError(
                    "An allowed team does not exist in the caller's organisation."
                )
        policy = BenchAccessPolicy(
            bench_id=bench.id,
            visibility=visibility,
            reservation_role=reservation_role,
            operation_role=operation_role,
            allowed_team_ids=team_ids,
        )
        persisted = await self._repository.set_bench_access_policy(organisation_id, policy)
        await self._audit_success(
            context,
            "BENCH_ACCESS_POLICY_UPDATED",
            ResourceType.BENCH.value,
            bench.id,
            metadata={
                "visibility": persisted.visibility.value,
                "reservation_role": (
                    persisted.reservation_role.value
                    if persisted.reservation_role is not None
                    else None
                ),
                "operation_role": (
                    persisted.operation_role.value if persisted.operation_role is not None else None
                ),
                "allowed_team_ids": sorted(str(team_id) for team_id in team_ids),
            },
        )
        return BenchAccessPolicyResult(policy=persisted, configured=True)

    async def get_workflow_access_policy(
        self,
        context: AuthenticationContext,
        workflow_id: str,
    ) -> WorkflowAccessPolicyResult:
        workflow, resource = await self._workflow_policy_resource(context, workflow_id)
        await self._authorisation.require(
            context.principal,
            "workflows:manage",
            resource,
            credential_restrictions=context.permission_restrictions,
        )
        configured = await self._repository.get_workflow_access_policy(
            context.principal.organisation_id,
            workflow.name,
        )
        if configured is not None:
            return WorkflowAccessPolicyResult(policy=configured, configured=True)
        return WorkflowAccessPolicyResult(
            policy=WorkflowAccessPolicy(
                workflow_id=workflow.name,
                visibility=self._default_workflow_visibility,
            ),
            configured=False,
        )

    async def set_workflow_access_policy(
        self,
        context: AuthenticationContext,
        workflow_id: str,
        *,
        visibility: WorkflowVisibility,
    ) -> WorkflowAccessPolicyResult:
        workflow, resource = await self._workflow_policy_resource(context, workflow_id)
        await self._authorisation.require(
            context.principal,
            "workflows:manage",
            resource,
            credential_restrictions=context.permission_restrictions,
        )
        policy = WorkflowAccessPolicy(
            workflow_id=workflow.name,
            visibility=visibility,
        )
        persisted = await self._repository.set_workflow_access_policy(
            context.principal.organisation_id,
            policy,
        )
        await self._audit_success(
            context,
            "WORKFLOW_ACCESS_POLICY_UPDATED",
            ResourceType.WORKFLOW.value,
            workflow.name,
            metadata={"visibility": persisted.visibility.value},
        )
        return WorkflowAccessPolicyResult(policy=persisted, configured=True)

    async def list_audit_events(
        self,
        context: AuthenticationContext,
        *,
        action: str | None = None,
        outcome: AuditOutcome | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        await self._require(context, "audit:read")
        events = await self._repository.list_audit_events(
            context.principal.organisation_id,
            action=action,
            outcome=outcome,
            after=after,
            before=before,
            limit=limit,
        )
        await self._audit_success(
            context,
            "AUDIT_LOG_VIEWED",
            "AUDIT_LOG",
            None,
            metadata={
                "result_count": len(events),
                "action_filter_applied": action is not None,
                "outcome_filter_applied": outcome is not None,
                "after_filter_applied": after is not None,
                "before_filter_applied": before is not None,
                "requested_limit": limit,
            },
        )
        return events

    async def get_audit_event(
        self,
        context: AuthenticationContext,
        event_id: UUID,
    ) -> AuditEvent:
        await self._require(context, "audit:read")
        event = await self._repository.get_audit_event(
            context.principal.organisation_id,
            event_id,
        )
        if event is None:
            raise AuditEventNotFoundError("The audit event does not exist.")
        return event

    async def _validate_subject(
        self,
        context: AuthenticationContext,
        subject_type: RoleSubjectType,
        subject_id: UUID,
    ) -> None:
        if subject_type is RoleSubjectType.USER:
            await self.get_user(context, subject_id)
        elif subject_type is RoleSubjectType.SERVICE_ACCOUNT:
            await self.get_service_account(context, subject_id)
        elif await self._repository.get_team(context.principal.organisation_id, subject_id) is None:
            raise TeamNotFoundError("The role subject team does not exist.")

    async def _bench_policy_resource(
        self,
        context: AuthenticationContext,
        bench_id: str,
    ) -> tuple[GlobalBenchRecord, AuthorisationResource]:
        if self._bench_directory is None:
            raise RuntimeError("Bench access-policy administration is not configured.")
        organisation_id = context.principal.organisation_id
        bench = await self._bench_directory.get(
            bench_id,
            organisation_id=organisation_id,
        )
        if bench is None:
            raise BenchNotFoundError("The bench does not exist.")
        return bench, AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=organisation_id,
            parent_agent_id=bench.agent_id,
        )

    async def _workflow_policy_resource(
        self,
        context: AuthenticationContext,
        workflow_id: str,
    ) -> tuple[WorkflowDefinition, AuthorisationResource]:
        if self._workflow_catalog is None:
            raise RuntimeError("Workflow access-policy administration is not configured.")
        organisation_id = context.principal.organisation_id
        workflow = await self._workflow_catalog.get_definition(
            workflow_id,
            organisation_id=organisation_id,
        )
        if workflow is None:
            raise WorkflowNotFoundError("The workflow does not exist.")
        return workflow, AuthorisationResource(
            type=ResourceType.WORKFLOW,
            id=workflow.name,
            organisation_id=organisation_id,
        )

    async def _require(self, context: AuthenticationContext, permission: str) -> None:
        await self._authorisation.require(
            context.principal,
            permission,
            _organisation_resource(context.principal.organisation_id),
            credential_restrictions=context.permission_restrictions,
        )

    async def _revoke_user_sessions(
        self,
        organisation_id: UUID,
        user_id: UUID,
        revoked_at: datetime,
    ) -> None:
        for session in await self._repository.list_sessions(organisation_id, user_id):
            if session.revoked_at is None:
                await self._repository.update_session(
                    session.model_copy(update={"revoked_at": revoked_at})
                )

    async def _audit_success(
        self,
        context: AuthenticationContext,
        action: str,
        resource_type: str,
        resource_id: str | None,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        await self._audit(
            context.principal.organisation_id,
            actor=context.principal,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

    async def _audit(
        self,
        organisation_id: UUID,
        *,
        actor: Principal,
        action: str,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not self._audit_enabled:
            return
        await self._repository.create_audit_event(
            AuditEvent(
                organisation_id=organisation_id,
                timestamp=self._clock(),
                actor_type=actor.type,
                actor_id=actor.id,
                actor_display_name=actor.display_name,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.SUCCEEDED,
                metadata=metadata or {},
            )
        )


def _organisation_resource(organisation_id: UUID) -> AuthorisationResource:
    return AuthorisationResource(
        type=ResourceType.ORGANISATION,
        id=str(organisation_id),
        organisation_id=organisation_id,
    )


def _principal(user: User) -> Principal:
    return Principal(
        id=user.id,
        type=PrincipalType.USER,
        organisation_id=user.organisation_id,
        display_name=user.display_name,
    )
