from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lab_platform.core.errors import PermissionDeniedError
from lab_platform.models import (
    AuditEvent,
    AuditOutcome,
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

ALL_PERMISSIONS = frozenset(
    {
        "organisation:read",
        "organisation:manage",
        "users:read",
        "users:manage",
        "teams:read",
        "teams:manage",
        "roles:read",
        "roles:manage",
        "service_accounts:read",
        "service_accounts:manage",
        "credentials:create",
        "credentials:revoke",
        "agents:read",
        "agents:manage",
        "agents:drain",
        "benches:read",
        "benches:manage",
        "benches:reserve",
        "benches:operate",
        "benches:flash",
        "benches:reset",
        "benches:serial",
        "workflows:read",
        "workflows:run",
        "workflows:manage",
        "operations:read",
        "operations:cancel",
        "artifacts:read",
        "artifacts:write",
        "artifacts:delete",
        "ci:sessions:create",
        "ci:sessions:read",
        "ci:sessions:cancel",
        "audit:read",
    }
)

ROLE_PERMISSIONS: Mapping[RoleName, frozenset[str]] = {
    RoleName.ORGANISATION_OWNER: ALL_PERMISSIONS,
    RoleName.ORGANISATION_ADMIN: ALL_PERMISSIONS - {"organisation:manage"},
    RoleName.LAB_ADMIN: frozenset(
        {
            "organisation:read",
            "agents:read",
            "agents:manage",
            "agents:drain",
            "benches:read",
            "benches:manage",
            "benches:reserve",
            "benches:operate",
            "benches:flash",
            "benches:reset",
            "benches:serial",
            "workflows:read",
            "workflows:run",
            "workflows:manage",
            "operations:read",
            "operations:cancel",
            "artifacts:read",
            "artifacts:write",
            "artifacts:delete",
            "ci:sessions:create",
            "ci:sessions:read",
            "ci:sessions:cancel",
            "audit:read",
        }
    ),
    RoleName.OPERATOR: frozenset(
        {
            "agents:read",
            "benches:read",
            "benches:reserve",
            "benches:operate",
            "benches:flash",
            "benches:reset",
            "benches:serial",
            "workflows:read",
            "workflows:run",
            "operations:read",
            "artifacts:read",
            "artifacts:write",
        }
    ),
    RoleName.WORKFLOW_RUNNER: frozenset(
        {
            "benches:read",
            "benches:operate",
            "workflows:read",
            "workflows:run",
            "operations:read",
            "artifacts:read",
            "artifacts:write",
            "ci:sessions:create",
            "ci:sessions:read",
            "ci:sessions:cancel",
        }
    ),
    RoleName.RESERVER: frozenset({"benches:read", "benches:reserve"}),
    RoleName.VIEWER: frozenset({"benches:read", "operations:read"}),
    RoleName.AUDITOR: frozenset(
        {
            "organisation:read",
            "agents:read",
            "benches:read",
            "workflows:read",
            "operations:read",
            "audit:read",
        }
    ),
}

ORGANISATION_ROLE_PERMISSIONS: Mapping[OrganisationRole, frozenset[str]] = {
    OrganisationRole.OWNER: ALL_PERMISSIONS,
    OrganisationRole.ADMIN: ROLE_PERMISSIONS[RoleName.ORGANISATION_ADMIN],
    OrganisationRole.MEMBER: frozenset({"organisation:read"}),
    OrganisationRole.VIEWER: frozenset(
        {
            "organisation:read",
            "benches:read",
            "operations:read",
        }
    ),
}

_ORGANISATION_ROLE_NAMES: Mapping[OrganisationRole, RoleName | None] = {
    OrganisationRole.OWNER: RoleName.ORGANISATION_OWNER,
    OrganisationRole.ADMIN: RoleName.ORGANISATION_ADMIN,
    OrganisationRole.MEMBER: None,
    OrganisationRole.VIEWER: RoleName.VIEWER,
}


class AuthorisationRepository(Protocol):
    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None: ...

    async def list_team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]: ...

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]],
    ) -> Sequence[RoleAssignment]: ...


class AuthorisationAuditRepository(Protocol):
    async def create_audit_event(self, event: AuditEvent) -> AuditEvent: ...


class AuthorisationPolicyRepository(Protocol):
    async def get_bench_access_policy(
        self,
        organisation_id: UUID,
        bench_id: str,
    ) -> BenchAccessPolicy | None: ...

    async def get_workflow_access_policy(
        self,
        organisation_id: UUID,
        workflow_id: str,
    ) -> WorkflowAccessPolicy | None: ...


@dataclass(frozen=True, slots=True)
class AuthorisationDecision:
    allowed: bool
    roles: frozenset[RoleName]
    permissions: frozenset[str]
    granting_assignment_ids: frozenset[UUID]


class AuthorisationService:
    """Resolve the effective additive permissions for one trusted resource."""

    def __init__(
        self,
        repository: AuthorisationRepository,
        *,
        audit_repository: AuthorisationAuditRepository | None = None,
        policy_repository: AuthorisationPolicyRepository | None = None,
        default_bench_visibility: BenchVisibility = BenchVisibility.ORGANISATION,
        audit_enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._audit_repository = audit_repository
        self._policy_repository = policy_repository
        self._default_bench_visibility = default_bench_visibility
        self._audit_enabled = audit_enabled
        self._clock = clock

    async def evaluate(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> AuthorisationDecision:
        if principal.organisation_id != resource.organisation_id:
            return _denied_decision()

        now = _as_utc(self._clock())
        subjects: set[tuple[RoleSubjectType, UUID]] = {
            (_principal_subject_type(principal.type), principal.id)
        }
        organisation_role: OrganisationRole | None = None
        team_ids: Collection[UUID] = ()
        if principal.type is PrincipalType.USER:
            membership = await self._repository.get_organisation_membership(
                principal.organisation_id,
                principal.id,
            )
            if (
                membership is not None
                and membership.organisation_id == principal.organisation_id
                and membership.user_id == principal.id
            ):
                organisation_role = membership.role
            team_ids = await self._repository.list_team_ids_for_user(
                principal.organisation_id,
                principal.id,
            )
            subjects.update((RoleSubjectType.TEAM, team_id) for team_id in team_ids)

        assignments = await self._repository.list_role_assignments(
            principal.organisation_id,
            frozenset(subjects),
        )
        applicable = tuple(
            assignment
            for assignment in assignments
            if assignment.organisation_id == principal.organisation_id
            and (assignment.subject_type, assignment.subject_id) in subjects
            and _assignment_is_active(assignment, now)
            and _assignment_applies_to(assignment, resource)
        )

        policy_permissions: set[str] = set()
        if self._policy_repository is not None:
            applicable, organisation_role, policy_permissions = await self._apply_access_policy(
                principal,
                resource,
                permission,
                organisation_role,
                frozenset(team_ids) if principal.type is PrincipalType.USER else frozenset(),
                applicable,
            )

        roles = {assignment.role for assignment in applicable}
        permissions: set[str] = set(policy_permissions)
        if organisation_role is not None:
            permissions.update(ORGANISATION_ROLE_PERMISSIONS[organisation_role])
            role_name = _ORGANISATION_ROLE_NAMES[organisation_role]
            if role_name is not None:
                roles.add(role_name)
        for assignment in applicable:
            permissions.update(ROLE_PERMISSIONS[assignment.role])

        if credential_restrictions is not None:
            permissions.intersection_update(credential_restrictions)

        frozen_permissions = frozenset(permissions)
        allowed = permission in frozen_permissions
        granting_assignment_ids = (
            frozenset(
                assignment.id
                for assignment in applicable
                if permission in ROLE_PERMISSIONS[assignment.role]
            )
            if allowed
            else frozenset()
        )
        return AuthorisationDecision(
            allowed=allowed,
            roles=frozenset(roles),
            permissions=frozen_permissions,
            granting_assignment_ids=granting_assignment_ids,
        )

    async def _apply_access_policy(
        self,
        principal: Principal,
        resource: AuthorisationResource,
        permission: str,
        organisation_role: OrganisationRole | None,
        team_ids: frozenset[UUID],
        assignments: tuple[RoleAssignment, ...],
    ) -> tuple[tuple[RoleAssignment, ...], OrganisationRole | None, set[str]]:
        repository = self._policy_repository
        if repository is None:  # pragma: no cover - guarded by evaluate
            return assignments, organisation_role, set()
        if resource.type is ResourceType.BENCH:
            bench_policy = await repository.get_bench_access_policy(
                principal.organisation_id,
                resource.id,
            )
            visibility = (
                bench_policy.visibility
                if bench_policy is not None
                else self._default_bench_visibility
            )
            filtered, effective_role = _filter_bench_policy_assignments(
                principal,
                organisation_role,
                assignments,
                visibility,
            )
            permissions: set[str] = set()
            if (
                bench_policy is not None
                and permission == "benches:read"
                and bench_policy.allowed_team_ids.intersection(team_ids)
            ):
                permissions.add(permission)
            required_role = _bench_policy_required_role(bench_policy, permission)
            if required_role is not None and not _policy_role_is_present(
                required_role,
                effective_role,
                filtered,
            ):
                return (), None, set()
            return filtered, effective_role, permissions

        if resource.type is ResourceType.WORKFLOW:
            workflow_policy = await repository.get_workflow_access_policy(
                principal.organisation_id,
                resource.id,
            )
            if (
                workflow_policy is None
                or workflow_policy.visibility is WorkflowVisibility.ORGANISATION
            ):
                return assignments, organisation_role, set()
            if _is_organisation_administrator(organisation_role):
                return assignments, organisation_role, set()
            if workflow_policy.visibility is WorkflowVisibility.ADMIN_ONLY:
                return (), None, set()
            return (
                tuple(
                    assignment
                    for assignment in assignments
                    if assignment.resource_type is ResourceType.WORKFLOW
                    and assignment.resource_id == resource.id
                ),
                None,
                set(),
            )
        return assignments, organisation_role, set()

    async def is_allowed(
        self,
        principal: Principal,
        permission: str,
        resource: AuthorisationResource,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool:
        decision = await self.evaluate(
            principal,
            permission,
            resource,
            credential_restrictions=credential_restrictions,
        )
        return decision.allowed

    async def is_allowed_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> bool:
        """Return whether one effective assignment grants a permission somewhere.

        This is intentionally limited to operations that allocate an inert,
        organisation-owned container before its concrete resource is known (a CI
        session is the Phase 6 use case). Protected work must still call
        ``require`` against the selected workflow and bench.
        """

        return (
            await self.evaluate_anywhere(
                principal,
                permission,
                credential_restrictions=credential_restrictions,
            )
        ).allowed

    async def evaluate_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> AuthorisationDecision:
        """Resolve the grants behind an inert organisation-owned container action."""

        if credential_restrictions is not None and permission not in credential_restrictions:
            return _denied_decision()
        now = _as_utc(self._clock())
        subjects: set[tuple[RoleSubjectType, UUID]] = {
            (_principal_subject_type(principal.type), principal.id)
        }
        organisation_role: OrganisationRole | None = None
        if principal.type is PrincipalType.USER:
            membership = await self._repository.get_organisation_membership(
                principal.organisation_id,
                principal.id,
            )
            if (
                membership is not None
                and membership.organisation_id == principal.organisation_id
                and membership.user_id == principal.id
            ):
                organisation_role = membership.role
            team_ids = await self._repository.list_team_ids_for_user(
                principal.organisation_id,
                principal.id,
            )
            subjects.update((RoleSubjectType.TEAM, team_id) for team_id in team_ids)
        assignments = await self._repository.list_role_assignments(
            principal.organisation_id,
            frozenset(subjects),
        )
        granting = frozenset(
            assignment.id
            for assignment in assignments
            if assignment.organisation_id == principal.organisation_id
            and (assignment.subject_type, assignment.subject_id) in subjects
            and _assignment_is_active(assignment, now)
            and permission in ROLE_PERMISSIONS[assignment.role]
        )
        organisation_allowed = (
            organisation_role is not None
            and permission in ORGANISATION_ROLE_PERMISSIONS[organisation_role]
        )
        return AuthorisationDecision(
            allowed=organisation_allowed or bool(granting),
            roles=frozenset(),
            permissions=frozenset({permission})
            if organisation_allowed or granting
            else frozenset(),
            granting_assignment_ids=granting,
        )

    async def require_anywhere(
        self,
        principal: Principal,
        permission: str,
        *,
        credential_restrictions: Collection[str] | None = None,
    ) -> None:
        if (
            await self.evaluate_anywhere(
                principal,
                permission,
                credential_restrictions=credential_restrictions,
            )
        ).allowed:
            return
        await self.require(
            principal,
            permission,
            AuthorisationResource(
                type=ResourceType.ORGANISATION,
                id=str(principal.organisation_id),
                organisation_id=principal.organisation_id,
            ),
            credential_restrictions=credential_restrictions,
        )

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
        raise PermissionDeniedError(
            "The principal is not allowed to perform this action.",
            required_permission=permission,
            resource_type=resource.type.value,
            resource_id=resource.id,
        )

    async def audit_permission_denied(
        self,
        principal: Principal,
        permission: str,
        *,
        resource_type: str,
        resource_id: str | None,
        reason: str = "Required permission was not granted.",
    ) -> AuditEvent | None:
        """Record a trusted denial whose policy check cannot use ``require``."""

        if not self._audit_enabled or self._audit_repository is None:
            return None
        return await self._audit_repository.create_audit_event(
            AuditEvent(
                organisation_id=principal.organisation_id,
                timestamp=_as_utc(self._clock()),
                actor_type=principal.type,
                actor_id=principal.id,
                actor_display_name=principal.display_name,
                action="PERMISSION_DENIED",
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.DENIED,
                reason=reason,
                metadata={"required_permission": permission},
            )
        )

    async def audit_success(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
        request_id: UUID | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuditEvent | None:
        """Append one bounded success event for an authorised protected action."""

        if not self._audit_enabled or self._audit_repository is None:
            return None
        return await self._audit_repository.create_audit_event(
            AuditEvent(
                organisation_id=principal.organisation_id,
                timestamp=_as_utc(self._clock()),
                actor_type=principal.type,
                actor_id=principal.id,
                actor_display_name=principal.display_name,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=request_id,
                source_ip=source_ip,
                user_agent=user_agent,
                metadata=metadata or {},
            )
        )

    async def audit_system_success(
        self,
        organisation_id: UUID,
        action: str,
        *,
        resource_type: str,
        resource_id: str | None,
        metadata: dict[str, object] | None = None,
        request_id: UUID | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuditEvent | None:
        """Append a success event for a trusted non-principal system action."""

        if not self._audit_enabled or self._audit_repository is None:
            return None
        return await self._audit_repository.create_audit_event(
            AuditEvent(
                organisation_id=organisation_id,
                timestamp=_as_utc(self._clock()),
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=request_id,
                source_ip=source_ip,
                user_agent=user_agent,
                metadata=metadata or {},
            )
        )


def _principal_subject_type(principal_type: PrincipalType) -> RoleSubjectType:
    if principal_type is PrincipalType.USER:
        return RoleSubjectType.USER
    return RoleSubjectType.SERVICE_ACCOUNT


def _filter_bench_policy_assignments(
    principal: Principal,
    organisation_role: OrganisationRole | None,
    assignments: tuple[RoleAssignment, ...],
    visibility: BenchVisibility,
) -> tuple[tuple[RoleAssignment, ...], OrganisationRole | None]:
    if visibility is BenchVisibility.ORGANISATION or _is_organisation_administrator(
        organisation_role
    ):
        return assignments, organisation_role
    if visibility is BenchVisibility.RESTRICTED:
        return (
            tuple(
                assignment
                for assignment in assignments
                if assignment.resource_type in {ResourceType.AGENT, ResourceType.BENCH}
            ),
            None,
        )
    direct_subject = (_principal_subject_type(principal.type), principal.id)
    return (
        tuple(
            assignment
            for assignment in assignments
            if (assignment.subject_type, assignment.subject_id) == direct_subject
            and assignment.resource_type is ResourceType.BENCH
        ),
        None,
    )


def _bench_policy_required_role(
    policy: BenchAccessPolicy | None,
    permission: str,
) -> RoleName | None:
    if policy is None:
        return None
    if permission == "benches:reserve":
        return policy.reservation_role
    if permission in {"benches:operate", "benches:flash", "benches:reset", "benches:serial"}:
        return policy.operation_role
    return None


def _policy_role_is_present(
    required_role: RoleName,
    organisation_role: OrganisationRole | None,
    assignments: tuple[RoleAssignment, ...],
) -> bool:
    if _is_organisation_administrator(organisation_role):
        return True
    return any(assignment.role is required_role for assignment in assignments)


def _is_organisation_administrator(role: OrganisationRole | None) -> bool:
    return role in {OrganisationRole.OWNER, OrganisationRole.ADMIN}


def _assignment_is_active(assignment: RoleAssignment, now: datetime) -> bool:
    created_at = _optional_utc(assignment.created_at)
    expires_at = _optional_utc(assignment.expires_at)
    return (
        created_at is not None
        and created_at <= now
        and (assignment.expires_at is None or (expires_at is not None and expires_at > now))
    )


def _assignment_applies_to(
    assignment: RoleAssignment,
    resource: AuthorisationResource,
) -> bool:
    if assignment.resource_type is ResourceType.ORGANISATION:
        organisation_ids = {str(resource.organisation_id)}
        if resource.type is ResourceType.ORGANISATION:
            organisation_ids.add(resource.id)
        return assignment.resource_id in organisation_ids
    if assignment.resource_type is resource.type:
        return assignment.resource_id == resource.id
    return (
        assignment.resource_type is ResourceType.AGENT
        and resource.type is ResourceType.BENCH
        and resource.parent_agent_id is not None
        and assignment.resource_id == str(resource.parent_agent_id)
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Authorisation clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _denied_decision() -> AuthorisationDecision:
    return AuthorisationDecision(
        allowed=False,
        roles=frozenset(),
        permissions=frozenset(),
        granting_assignment_ids=frozenset(),
    )


__all__ = [
    "ALL_PERMISSIONS",
    "ORGANISATION_ROLE_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "AuthorisationDecision",
    "AuthorisationRepository",
    "AuthorisationService",
]
