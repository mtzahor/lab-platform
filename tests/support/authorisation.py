from __future__ import annotations

from collections.abc import Collection, Sequence
from uuid import UUID

from lab_platform.models import OrganisationMembership, RoleAssignment, RoleSubjectType


class ScopedAuthorisationRepository:
    def __init__(self, assignments: Sequence[RoleAssignment]) -> None:
        self.assignments = tuple(assignments)

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None:
        del organisation_id, user_id
        return None

    async def list_team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]:
        del organisation_id, user_id
        return ()

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]],
    ) -> Sequence[RoleAssignment]:
        return tuple(
            assignment
            for assignment in self.assignments
            if assignment.organisation_id == organisation_id
            and (assignment.subject_type, assignment.subject_id) in subjects
        )
