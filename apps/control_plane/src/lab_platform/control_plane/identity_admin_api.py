import ipaddress
from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lab_platform.control_plane.identity_api import (
    authenticate_identity_token,
    extract_bearer_or_cookie_token,
)
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core.authorisation import ALL_PERMISSIONS
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from lab_platform.models import (
    ApiCredential,
    AuditOutcome,
    AuthenticationContext,
    AuthenticationSource,
    AuthorisationResource,
    BenchVisibility,
    OrganisationRole,
    PrincipalType,
    ResourceType,
    RoleName,
    RoleSubjectType,
    ServiceAccountStatus,
    TeamRole,
    UserStatus,
    WorkflowVisibility,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ADMIN_BEARER = HTTPBearer(auto_error=False, scheme_name="IdentityBearerAuth")
PermissionName = Annotated[str, Field(min_length=1, max_length=200)]


class IdentityAdminApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrganisationUpdateRequest(IdentityAdminApiModel):
    name: str = Field(min_length=1, max_length=200)


class UserCreateRequest(IdentityAdminApiModel):
    username: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    display_name: str = Field(min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=12, max_length=4096, repr=False)
    email: str | None = Field(default=None, max_length=320)
    organisation_role: OrganisationRole = OrganisationRole.MEMBER
    authentication_source: AuthenticationSource = AuthenticationSource.LOCAL

    @model_validator(mode="after")
    def validate_authentication_source(self) -> Self:
        if self.authentication_source is AuthenticationSource.LOCAL and self.password is None:
            raise ValueError("password is required when authentication_source is LOCAL")
        if self.authentication_source is AuthenticationSource.OIDC and self.password is not None:
            raise ValueError("password must be omitted when authentication_source is OIDC")
        return self


class UserUpdateRequest(IdentityAdminApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one user field must be supplied.")
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be null.")
        return self


class PasswordResetRequest(IdentityAdminApiModel):
    password: str = Field(min_length=12, max_length=4096, repr=False)


class TeamCreateRequest(IdentityAdminApiModel):
    slug: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class TeamUpdateRequest(IdentityAdminApiModel):
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one team field must be supplied.")
        if "slug" in self.model_fields_set and self.slug is None:
            raise ValueError("slug cannot be null.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null.")
        return self


class TeamMemberCreateRequest(IdentityAdminApiModel):
    user_id: UUID
    role: TeamRole = TeamRole.MEMBER


class ServiceAccountCreateRequest(IdentityAdminApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ServiceAccountUpdateRequest(IdentityAdminApiModel):
    status: ServiceAccountStatus | None = None
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if not self.model_fields_set.intersection({"status", "description"}):
            raise ValueError("At least one service-account field must be supplied.")
        return self


class CredentialCreateRequest(IdentityAdminApiModel):
    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None
    allowed_ip_ranges: list[str] = Field(default_factory=list, max_length=64)
    permission_restrictions: set[PermissionName] | None = None

    @field_validator("expires_at")
    @classmethod
    def require_future_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized

    @field_validator("allowed_ip_ranges")
    @classmethod
    def normalize_ip_ranges(cls, values: list[str]) -> list[str]:
        try:
            return [str(ipaddress.ip_network(value, strict=False)) for value in values]
        except ValueError as exc:
            raise ValueError("allowed_ip_ranges must contain valid IPv4 or IPv6 CIDRs") from exc

    @field_validator("permission_restrictions")
    @classmethod
    def require_known_permissions(cls, value: set[str] | None) -> set[str] | None:
        if value is None:
            return None
        unknown = value - ALL_PERMISSIONS
        if unknown:
            raise ValueError(f"Unknown permissions: {', '.join(sorted(unknown))}")
        return value


class RoleAssignmentCreateRequest(IdentityAdminApiModel):
    subject_type: RoleSubjectType
    subject_id: UUID
    role: RoleName
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=500)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_future_aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized


class BenchAccessPolicyUpdateRequest(IdentityAdminApiModel):
    visibility: BenchVisibility
    reservation_role: RoleName | None = None
    operation_role: RoleName | None = None
    allowed_team_ids: set[UUID] = Field(default_factory=set, max_length=500)


class WorkflowAccessPolicyUpdateRequest(IdentityAdminApiModel):
    visibility: WorkflowVisibility


def create_identity_admin_router(runtime: ControlPlaneRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["identity administration"])

    async def require_identity(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_ADMIN_BEARER),
        ] = None,
    ) -> AuthenticationContext:
        credential = extract_bearer_or_cookie_token(request, credentials)
        if credential is None:
            raise AuthenticationRequiredError("A bearer token or browser session is required.")
        return await authenticate_identity_token(
            runtime,
            credential.token,
            source_ip=request.client.host if request.client is not None else None,
        )

    IdentityContext = Annotated[AuthenticationContext, Security(require_identity)]

    @router.get("/organisation")
    async def get_organisation(context: IdentityContext) -> dict[str, object]:
        organisation = await runtime.identity_administration.get_organisation(context)
        return organisation.model_dump(mode="json")

    @router.patch("/organisation")
    async def update_organisation(
        body: OrganisationUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        organisation = await runtime.identity_administration.update_organisation(
            context,
            name=body.name,
        )
        return organisation.model_dump(mode="json")

    @router.get("/users")
    async def list_users(context: IdentityContext) -> dict[str, object]:
        users = await runtime.identity_administration.list_users(context)
        return {"items": [user.model_dump(mode="json") for user in users]}

    @router.post("/users", status_code=status.HTTP_201_CREATED)
    async def create_user(
        body: UserCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        user = await runtime.identity_administration.create_user(
            context,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            email=body.email,
            organisation_role=body.organisation_role,
            authentication_source=body.authentication_source,
        )
        return user.model_dump(mode="json")

    @router.get("/users/{user_id:uuid}")
    async def get_user(user_id: UUID, context: IdentityContext) -> dict[str, object]:
        user = await runtime.identity_administration.get_user(context, user_id)
        return user.model_dump(mode="json")

    @router.get("/users/{user_id:uuid}/teams")
    async def list_user_teams(
        user_id: UUID,
        context: IdentityContext,
    ) -> dict[str, object]:
        memberships = await runtime.identity_administration.list_user_team_memberships(
            context,
            user_id,
        )
        return {
            "items": [
                {
                    "team": team.model_dump(mode="json"),
                    "membership": membership.model_dump(mode="json"),
                }
                for team, membership in memberships
            ]
        }

    @router.get("/users/{user_id:uuid}/sessions")
    async def list_user_sessions(
        user_id: UUID,
        context: IdentityContext,
    ) -> dict[str, object]:
        sessions = await runtime.identity_administration.list_user_sessions(context, user_id)
        now = datetime.now(UTC)
        return {
            "items": [
                {
                    **session.model_dump(mode="json", exclude={"secret_hash"}),
                    "active": session.revoked_at is None and session.expires_at > now,
                }
                for session in sessions
            ]
        }

    @router.patch("/users/{user_id:uuid}")
    async def update_user(
        user_id: UUID,
        body: UserUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        user = await runtime.identity_administration.update_user(
            context,
            user_id,
            {field: getattr(body, field) for field in body.model_fields_set},
        )
        return user.model_dump(mode="json")

    @router.post("/users/{user_id:uuid}/disable")
    async def disable_user(user_id: UUID, context: IdentityContext) -> dict[str, object]:
        user = await runtime.identity_administration.set_user_status(
            context,
            user_id,
            UserStatus.DISABLED,
        )
        return user.model_dump(mode="json")

    @router.post("/users/{user_id:uuid}/enable")
    async def enable_user(user_id: UUID, context: IdentityContext) -> dict[str, object]:
        user = await runtime.identity_administration.set_user_status(
            context,
            user_id,
            UserStatus.ACTIVE,
        )
        return user.model_dump(mode="json")

    @router.post("/users/{user_id:uuid}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
    async def reset_password(
        user_id: UUID,
        body: PasswordResetRequest,
        context: IdentityContext,
    ) -> Response:
        await runtime.identity_administration.reset_password(context, user_id, body.password)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/teams")
    async def list_teams(context: IdentityContext) -> dict[str, object]:
        teams = await runtime.identity_administration.list_teams(context)
        return {"items": [team.model_dump(mode="json") for team in teams]}

    @router.post("/teams", status_code=status.HTTP_201_CREATED)
    async def create_team(
        body: TeamCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        team = await runtime.identity_administration.create_team(
            context,
            slug=body.slug,
            name=body.name,
            description=body.description,
        )
        return team.model_dump(mode="json")

    @router.get("/teams/{team_id:uuid}")
    async def get_team(team_id: UUID, context: IdentityContext) -> dict[str, object]:
        team = await runtime.identity_administration.get_team(context, team_id)
        return team.model_dump(mode="json")

    @router.patch("/teams/{team_id:uuid}")
    async def update_team(
        team_id: UUID,
        body: TeamUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        team = await runtime.identity_administration.update_team(
            context,
            team_id,
            {field: getattr(body, field) for field in body.model_fields_set},
        )
        return team.model_dump(mode="json")

    @router.delete("/teams/{team_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_team(team_id: UUID, context: IdentityContext) -> Response:
        await runtime.identity_administration.delete_team(context, team_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/teams/{team_id:uuid}/members", status_code=status.HTTP_201_CREATED)
    async def add_team_member(
        team_id: UUID,
        body: TeamMemberCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        membership = await runtime.identity_administration.add_team_member(
            context,
            team_id,
            body.user_id,
            role=body.role,
        )
        return membership.model_dump(mode="json")

    @router.get("/teams/{team_id:uuid}/members")
    async def list_team_members(
        team_id: UUID,
        context: IdentityContext,
    ) -> dict[str, object]:
        members = await runtime.identity_administration.list_team_members(context, team_id)
        return {
            "items": [
                {
                    "membership": membership.model_dump(mode="json"),
                    "user": user.model_dump(mode="json"),
                }
                for membership, user in members
            ]
        }

    @router.delete(
        "/teams/{team_id:uuid}/members/{user_id:uuid}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def remove_team_member(
        team_id: UUID,
        user_id: UUID,
        context: IdentityContext,
    ) -> Response:
        await runtime.identity_administration.remove_team_member(context, team_id, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/service-accounts")
    async def list_service_accounts(context: IdentityContext) -> dict[str, object]:
        accounts = await runtime.identity_administration.list_service_accounts(context)
        return {"items": [account.model_dump(mode="json") for account in accounts]}

    @router.post("/service-accounts", status_code=status.HTTP_201_CREATED)
    async def create_service_account(
        body: ServiceAccountCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        account = await runtime.identity_administration.create_service_account(
            context,
            name=body.name,
            description=body.description,
        )
        return account.model_dump(mode="json")

    @router.get("/service-accounts/{account_id:uuid}")
    async def get_service_account(
        account_id: UUID,
        context: IdentityContext,
    ) -> dict[str, object]:
        account = await runtime.identity_administration.get_service_account(context, account_id)
        return account.model_dump(mode="json")

    @router.patch("/service-accounts/{account_id:uuid}")
    async def update_service_account(
        account_id: UUID,
        body: ServiceAccountUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        account = await runtime.identity_administration.get_service_account(context, account_id)
        if body.status is not None:
            account = await runtime.identity_administration.set_service_account_status(
                context,
                account_id,
                body.status,
            )
        if "description" in body.model_fields_set:
            account = await runtime.identity_administration.update_service_account_description(
                context,
                account_id,
                body.description,
            )
        return account.model_dump(mode="json")

    @router.delete(
        "/service-accounts/{account_id:uuid}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_service_account(account_id: UUID, context: IdentityContext) -> Response:
        await runtime.identity_administration.set_service_account_status(
            context,
            account_id,
            ServiceAccountStatus.REVOKED,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/service-accounts/{account_id:uuid}/credentials",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_service_account_credential(
        account_id: UUID,
        body: CredentialCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        issued = await runtime.identity_administration.create_service_account_credential(
            context,
            account_id,
            name=body.name,
            expires_at=body.expires_at,
            allowed_ip_ranges=body.allowed_ip_ranges,
            permission_restrictions=body.permission_restrictions,
        )
        return {
            "credential": _public_credential(issued.credential),
            "token": issued.token,
        }

    @router.get("/service-accounts/{account_id:uuid}/credentials")
    async def list_service_account_credentials(
        account_id: UUID,
        context: IdentityContext,
    ) -> dict[str, object]:
        credentials = await runtime.identity_administration.list_service_account_credentials(
            context,
            account_id,
        )
        return {"items": [_public_credential(credential) for credential in credentials]}

    @router.delete("/credentials/{credential_id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_credential(credential_id: UUID, context: IdentityContext) -> Response:
        await runtime.identity_administration.revoke_credential(context, credential_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/roles")
    async def list_roles(context: IdentityContext) -> dict[str, object]:
        roles = await runtime.identity_administration.list_roles(context)
        return {"items": [role.value for role in roles]}

    @router.get("/roles/{role}/permissions")
    async def get_role_permissions(role: RoleName, context: IdentityContext) -> dict[str, object]:
        permissions = await runtime.identity_administration.get_role_permissions(context, role)
        return {"role": role.value, "permissions": sorted(permissions)}

    @router.get("/role-assignments")
    async def list_role_assignments(
        context: IdentityContext,
        subject_type: Annotated[RoleSubjectType | None, Query()] = None,
        subject_id: Annotated[UUID | None, Query()] = None,
        resource_type: Annotated[ResourceType | None, Query()] = None,
        resource_id: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    ) -> dict[str, object]:
        assignments = await runtime.identity_administration.list_role_assignments(
            context,
            subject_type=subject_type,
            subject_id=subject_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return {"items": [assignment.model_dump(mode="json") for assignment in assignments]}

    @router.post("/role-assignments", status_code=status.HTTP_201_CREATED)
    async def create_role_assignment(
        body: RoleAssignmentCreateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        assignment = await runtime.identity_administration.assign_role(
            context,
            subject_type=body.subject_type,
            subject_id=body.subject_id,
            role=body.role,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            expires_at=body.expires_at,
        )
        return assignment.model_dump(mode="json")

    @router.delete(
        "/role-assignments/{assignment_id:uuid}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_role_assignment(assignment_id: UUID, context: IdentityContext) -> Response:
        await runtime.identity_administration.revoke_role(context, assignment_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/permissions/effective")
    async def effective_permissions(
        context: IdentityContext,
        resource_type: Annotated[ResourceType, Query()],
        resource_id: Annotated[str, Query(min_length=1, max_length=500)],
        permission: Annotated[str, Query(min_length=1, max_length=200)] = "organisation:read",
        parent_agent_id: Annotated[UUID | None, Query()] = None,
        subject_type: Annotated[PrincipalType | None, Query()] = None,
        subject_id: Annotated[UUID | None, Query()] = None,
    ) -> dict[str, object]:
        if permission not in ALL_PERMISSIONS:
            raise PermissionDeniedError(
                "The requested permission is not part of the platform permission vocabulary.",
                required_permission=permission,
            )
        decision = await runtime.identity_administration.effective_permissions(
            context,
            AuthorisationResource(
                type=resource_type,
                id=resource_id,
                organisation_id=context.principal.organisation_id,
                parent_agent_id=parent_agent_id,
            ),
            permission,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        return {
            "allowed": decision.allowed,
            "roles": sorted(role.value for role in decision.roles),
            "permissions": sorted(decision.permissions),
            "granting_assignment_ids": sorted(
                str(assignment_id) for assignment_id in decision.granting_assignment_ids
            ),
        }

    @router.get("/access-policies/benches/{bench_id:path}")
    async def get_bench_access_policy(
        bench_id: str,
        context: IdentityContext,
    ) -> dict[str, object]:
        result = await runtime.identity_administration.get_bench_access_policy(
            context,
            bench_id,
        )
        policy = result.policy.model_dump(mode="json")
        policy["allowed_team_ids"] = sorted(
            str(team_id) for team_id in result.policy.allowed_team_ids
        )
        return _access_policy_response(
            resource_type=ResourceType.BENCH,
            resource_id=result.policy.bench_id,
            configured=result.configured,
            policy=policy,
        )

    @router.put("/access-policies/benches/{bench_id:path}")
    async def set_bench_access_policy(
        bench_id: str,
        body: BenchAccessPolicyUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        result = await runtime.identity_administration.set_bench_access_policy(
            context,
            bench_id,
            visibility=body.visibility,
            reservation_role=body.reservation_role,
            operation_role=body.operation_role,
            allowed_team_ids=body.allowed_team_ids,
        )
        policy = result.policy.model_dump(mode="json")
        policy["allowed_team_ids"] = sorted(
            str(team_id) for team_id in result.policy.allowed_team_ids
        )
        return _access_policy_response(
            resource_type=ResourceType.BENCH,
            resource_id=result.policy.bench_id,
            configured=result.configured,
            policy=policy,
        )

    @router.get("/access-policies/workflows/{workflow_id:path}")
    async def get_workflow_access_policy(
        workflow_id: str,
        context: IdentityContext,
    ) -> dict[str, object]:
        result = await runtime.identity_administration.get_workflow_access_policy(
            context,
            workflow_id,
        )
        return _access_policy_response(
            resource_type=ResourceType.WORKFLOW,
            resource_id=result.policy.workflow_id,
            configured=result.configured,
            policy=result.policy.model_dump(mode="json"),
        )

    @router.put("/access-policies/workflows/{workflow_id:path}")
    async def set_workflow_access_policy(
        workflow_id: str,
        body: WorkflowAccessPolicyUpdateRequest,
        context: IdentityContext,
    ) -> dict[str, object]:
        result = await runtime.identity_administration.set_workflow_access_policy(
            context,
            workflow_id,
            visibility=body.visibility,
        )
        return _access_policy_response(
            resource_type=ResourceType.WORKFLOW,
            resource_id=result.policy.workflow_id,
            configured=result.configured,
            policy=result.policy.model_dump(mode="json"),
        )

    @router.get("/audit-events")
    async def list_audit_events(
        context: IdentityContext,
        actor: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        actor_id: Annotated[UUID | None, Query()] = None,
        action: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        resource_type: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        resource_id: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
        outcome: Annotated[AuditOutcome | None, Query()] = None,
        request_id: Annotated[UUID | None, Query()] = None,
        command_id: Annotated[UUID | None, Query()] = None,
        operation_id: Annotated[UUID | None, Query()] = None,
        after: Annotated[datetime | None, Query()] = None,
        before: Annotated[datetime | None, Query()] = None,
        cursor: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        events = await runtime.identity_administration.list_audit_events(
            context,
            actor=actor,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            request_id=request_id,
            command_id=command_id,
            operation_id=operation_id,
            after=after,
            before=before,
            cursor=cursor,
            limit=limit + 1,
        )
        visible = events[:limit]
        has_more = len(events) > limit
        return {
            "items": [event.model_dump(mode="json") for event in visible],
            "has_more": has_more,
            "next_cursor": str(visible[-1].id) if has_more and visible else None,
        }

    @router.get("/audit-events/{event_id:uuid}")
    async def get_audit_event(event_id: UUID, context: IdentityContext) -> dict[str, object]:
        event = await runtime.identity_administration.get_audit_event(context, event_id)
        return event.model_dump(mode="json")

    return router


def _public_credential(credential: ApiCredential) -> dict[str, object]:
    return credential.model_dump(mode="json", exclude={"secret_hash"})


def _access_policy_response(
    *,
    resource_type: ResourceType,
    resource_id: str,
    configured: bool,
    policy: dict[str, object],
) -> dict[str, object]:
    return {
        "resource_type": resource_type.value,
        "resource_id": resource_id,
        "configured": configured,
        "source": "CONFIGURED" if configured else "DEFAULT",
        "policy": policy,
    }


__all__ = ["create_identity_admin_router"]
