from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_platform.models import (
    ApiCredential,
    AuditEvent,
    AuditOutcome,
    AuthenticationSource,
    AuthorisationSnapshot,
    BenchAccessPolicy,
    BenchVisibility,
    LoginAttempt,
    Organisation,
    OrganisationMembership,
    OrganisationRole,
    OrganisationStatus,
    PasswordCredential,
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
    WorkflowVisibility,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.migrations import DEFAULT_ORGANISATION_ID


class SQLiteIdentityRepository:
    """Identity, tenant authorisation and append-only audit persistence.

    All ordinary resource reads include organisation scope in SQL. Credential and
    session authentication are the two deliberate exceptions: their public token
    formats contain an unguessable record UUID, which is resolved before the
    authenticated organisation context exists. API credentials are never looked up
    globally by a secret hash.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def ensure_default_organisation(self, *, slug: str, name: str) -> Organisation:
        organisation_id = UUID(DEFAULT_ORGANISATION_ID)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM organisations WHERE id = ?",
                (DEFAULT_ORGANISATION_ID,),
            ).fetchone()
            if row is None:
                now = datetime.now(UTC)
                organisation = Organisation(
                    id=organisation_id,
                    slug=slug,
                    name=name,
                    status=OrganisationStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(
                    "INSERT INTO organisations "
                    "(id, slug, name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        DEFAULT_ORGANISATION_ID,
                        organisation.slug,
                        organisation.name,
                        organisation.status.value,
                        _datetime_value(organisation.created_at),
                        _datetime_value(organisation.updated_at),
                    ),
                )
                return organisation
            current = _organisation_from_row(row)
            if current.slug == slug and current.name == name:
                return current
            updated = Organisation(
                id=current.id,
                slug=slug,
                name=name,
                status=current.status,
                created_at=current.created_at,
                updated_at=max(datetime.now(UTC), current.created_at),
            )
            connection.execute(
                "UPDATE organisations SET slug = ?, name = ?, updated_at = ? WHERE id = ?",
                (updated.slug, updated.name, _datetime_value(updated.updated_at), str(updated.id)),
            )
            return updated

    async def create_organisation(self, organisation: Organisation) -> Organisation:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO organisations "
                "(id, slug, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(organisation.id),
                    organisation.slug,
                    organisation.name,
                    organisation.status.value,
                    _datetime_value(organisation.created_at),
                    _datetime_value(organisation.updated_at),
                ),
            )
        return organisation

    async def get_organisation(self, organisation_id: UUID) -> Organisation | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM organisations WHERE id = ?",
                (str(organisation_id),),
            ).fetchone()
        return _organisation_from_row(row) if row is not None else None

    async def get_organisation_by_slug(self, slug: str) -> Organisation | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM organisations WHERE LOWER(slug) = LOWER(?)",
                (slug,),
            ).fetchone()
        return _organisation_from_row(row) if row is not None else None

    async def update_organisation(self, organisation: Organisation) -> Organisation:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE organisations SET slug = ?, name = ?, status = ?, created_at = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    organisation.slug,
                    organisation.name,
                    organisation.status.value,
                    _datetime_value(organisation.created_at),
                    _datetime_value(organisation.updated_at),
                    str(organisation.id),
                ),
            )
        _require_updated(cursor.rowcount, "Organisation", organisation.id)
        return organisation

    async def create_user(self, user: User) -> User:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, organisation_id, username, display_name, email, status, "
                "authentication_source, created_at, updated_at, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _user_values(user),
            )
        return user

    async def create_user_with_password(
        self,
        user: User,
        password_credential: PasswordCredential,
        membership: OrganisationMembership,
    ) -> User:
        if user.authentication_source is not AuthenticationSource.LOCAL:
            raise ValueError("Password credentials may only be created for a local user")
        if password_credential.user_id != user.id:
            raise ValueError("Password credential is not bound to the bootstrap user")
        if membership.user_id != user.id or membership.organisation_id != user.organisation_id:
            raise ValueError("Organisation membership is not bound to the bootstrap user")
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, organisation_id, username, display_name, email, status, "
                "authentication_source, created_at, updated_at, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _user_values(user),
            )
            connection.execute(
                "INSERT INTO password_credentials "
                "(user_id, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                _password_values(password_credential),
            )
            connection.execute(
                "INSERT INTO organisation_memberships "
                "(id, organisation_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?)",
                _organisation_membership_values(membership),
            )
        return user

    async def create_user_with_membership(
        self,
        user: User,
        membership: OrganisationMembership,
    ) -> User:
        if user.authentication_source is not AuthenticationSource.OIDC:
            raise ValueError("A passwordless user must use OIDC authentication")
        if membership.user_id != user.id or membership.organisation_id != user.organisation_id:
            raise ValueError("Organisation membership is not bound to the user")
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, organisation_id, username, display_name, email, status, "
                "authentication_source, created_at, updated_at, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _user_values(user),
            )
            connection.execute(
                "INSERT INTO organisation_memberships "
                "(id, organisation_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?)",
                _organisation_membership_values(membership),
            )
        return user

    async def get_user(self, organisation_id: UUID, user_id: UUID) -> User | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(user_id)),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    async def get_user_by_username(
        self,
        organisation_id: UUID,
        username: str,
    ) -> User | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE organisation_id = ? AND LOWER(username) = LOWER(?)",
                (str(organisation_id), username),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    async def list_users(
        self,
        organisation_id: UUID,
        *,
        status: UserStatus | None = None,
        limit: int = 500,
    ) -> list[User]:
        _require_limit(limit)
        parameters: list[object] = [str(organisation_id)]
        status_clause = ""
        if status is not None:
            status_clause = " AND status = ?"
            parameters.append(status.value)
        parameters.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE organisation_id = ?"
                + status_clause
                + " ORDER BY username, id LIMIT ?",
                parameters,
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    async def update_user(self, user: User) -> User:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE users SET username = ?, display_name = ?, email = ?, status = ?, "
                "authentication_source = ?, created_at = ?, updated_at = ?, last_login_at = ? "
                "WHERE organisation_id = ? AND id = ?",
                (
                    user.username,
                    user.display_name,
                    user.email,
                    user.status.value,
                    user.authentication_source.value,
                    _datetime_value(user.created_at),
                    _datetime_value(user.updated_at),
                    _optional_datetime_value(user.last_login_at),
                    str(user.organisation_id),
                    str(user.id),
                ),
            )
        _require_updated(cursor.rowcount, "User", user.id)
        return user

    async def create_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO password_credentials "
                "(user_id, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                _password_values(credential),
            )
        return credential

    async def set_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO password_credentials "
                "(user_id, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "password_hash = excluded.password_hash, updated_at = excluded.updated_at",
                _password_values(credential),
            )
        return credential

    async def get_password_credential(self, user_id: UUID) -> PasswordCredential | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT password_credentials.* FROM password_credentials "
                "JOIN users ON users.id = password_credentials.user_id "
                "WHERE users.id = ?",
                (str(user_id),),
            ).fetchone()
        return _password_from_row(row) if row is not None else None

    async def create_organisation_membership(
        self,
        membership: OrganisationMembership,
    ) -> OrganisationMembership:
        with self._database.transaction(immediate=True) as connection:
            _require_user_in_organisation(
                connection,
                membership.organisation_id,
                membership.user_id,
            )
            connection.execute(
                "INSERT INTO organisation_memberships "
                "(id, organisation_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?)",
                _organisation_membership_values(membership),
            )
        return membership

    async def set_organisation_membership(
        self,
        membership: OrganisationMembership,
    ) -> OrganisationMembership:
        with self._database.transaction(immediate=True) as connection:
            _require_user_in_organisation(
                connection,
                membership.organisation_id,
                membership.user_id,
            )
            connection.execute(
                "INSERT INTO organisation_memberships "
                "(id, organisation_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(organisation_id, user_id) DO UPDATE SET role = excluded.role",
                _organisation_membership_values(membership),
            )
            row = connection.execute(
                "SELECT * FROM organisation_memberships WHERE organisation_id = ? AND user_id = ?",
                (str(membership.organisation_id), str(membership.user_id)),
            ).fetchone()
        if row is None:  # pragma: no cover - write/read transaction invariant
            raise RuntimeError("Organisation membership was not persisted")
        return _organisation_membership_from_row(row)

    async def get_organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM organisation_memberships WHERE organisation_id = ? AND user_id = ?",
                (str(organisation_id), str(user_id)),
            ).fetchone()
        return _organisation_membership_from_row(row) if row is not None else None

    async def organisation_membership(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> OrganisationMembership | None:
        return await self.get_organisation_membership(organisation_id, user_id)

    async def list_organisation_memberships(
        self,
        organisation_id: UUID,
    ) -> list[OrganisationMembership]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM organisation_memberships WHERE organisation_id = ? "
                "ORDER BY created_at, id",
                (str(organisation_id),),
            ).fetchall()
        return [_organisation_membership_from_row(row) for row in rows]

    async def count_organisation_owners(self, organisation_id: UUID) -> int:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS owner_count FROM organisation_memberships "
                "WHERE organisation_id = ? AND role = 'OWNER'",
                (str(organisation_id),),
            ).fetchone()
        return int(row["owner_count"]) if row is not None else 0

    async def delete_organisation_membership(
        self,
        organisation_id: UUID,
        membership_id: UUID,
    ) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM organisation_memberships WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(membership_id)),
            )
        return cursor.rowcount == 1

    async def create_team(self, team: Team) -> Team:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO teams "
                "(id, organisation_id, slug, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                _team_values(team),
            )
        return team

    async def get_team(self, organisation_id: UUID, team_id: UUID) -> Team | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM teams WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(team_id)),
            ).fetchone()
        return _team_from_row(row) if row is not None else None

    async def get_team_by_slug(self, organisation_id: UUID, slug: str) -> Team | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM teams WHERE organisation_id = ? AND LOWER(slug) = LOWER(?)",
                (str(organisation_id), slug),
            ).fetchone()
        return _team_from_row(row) if row is not None else None

    async def list_teams(self, organisation_id: UUID, *, limit: int = 500) -> list[Team]:
        _require_limit(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM teams WHERE organisation_id = ? ORDER BY slug, id LIMIT ?",
                (str(organisation_id), limit),
            ).fetchall()
        return [_team_from_row(row) for row in rows]

    async def update_team(self, team: Team) -> Team:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE teams SET slug = ?, name = ?, description = ?, created_at = ?, "
                "updated_at = ? WHERE organisation_id = ? AND id = ?",
                (
                    team.slug,
                    team.name,
                    team.description,
                    _datetime_value(team.created_at),
                    _datetime_value(team.updated_at),
                    str(team.organisation_id),
                    str(team.id),
                ),
            )
        _require_updated(cursor.rowcount, "Team", team.id)
        return team

    async def delete_team(self, organisation_id: UUID, team_id: UUID) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM teams WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(team_id)),
            )
        return cursor.rowcount == 1

    async def create_team_membership(
        self,
        organisation_id: UUID,
        membership: TeamMembership,
    ) -> TeamMembership:
        with self._database.transaction(immediate=True) as connection:
            _require_team_in_organisation(connection, organisation_id, membership.team_id)
            _require_user_in_organisation(connection, organisation_id, membership.user_id)
            connection.execute(
                "INSERT INTO team_memberships "
                "(id, team_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?)",
                _team_membership_values(membership),
            )
        return membership

    async def list_team_memberships(
        self,
        organisation_id: UUID,
        team_id: UUID,
    ) -> list[TeamMembership]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT team_memberships.* FROM team_memberships "
                "JOIN teams ON teams.id = team_memberships.team_id "
                "WHERE teams.organisation_id = ? AND teams.id = ? "
                "ORDER BY team_memberships.created_at, team_memberships.id",
                (str(organisation_id), str(team_id)),
            ).fetchall()
        return [_team_membership_from_row(row) for row in rows]

    async def delete_team_membership(
        self,
        organisation_id: UUID,
        team_id: UUID,
        user_id: UUID,
    ) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM team_memberships WHERE team_id = ? AND user_id = ? "
                "AND EXISTS (SELECT 1 FROM teams WHERE teams.id = team_memberships.team_id "
                "AND teams.organisation_id = ?)",
                (str(team_id), str(user_id), str(organisation_id)),
            )
        return cursor.rowcount == 1

    async def list_team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT team_memberships.team_id FROM team_memberships "
                "JOIN teams ON teams.id = team_memberships.team_id "
                "JOIN users ON users.id = team_memberships.user_id "
                "WHERE teams.organisation_id = ? AND users.organisation_id = ? "
                "AND team_memberships.user_id = ? ORDER BY team_memberships.team_id",
                (str(organisation_id), str(organisation_id), str(user_id)),
            ).fetchall()
        return tuple(UUID(str(row["team_id"])) for row in rows)

    async def team_ids_for_user(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> Collection[UUID]:
        return await self.list_team_ids_for_user(organisation_id, user_id)

    async def create_service_account(self, account: ServiceAccount) -> ServiceAccount:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO service_accounts "
                "(id, organisation_id, name, description, status, created_at, updated_at, "
                "last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _service_account_values(account),
            )
        return account

    async def get_service_account(
        self,
        organisation_id: UUID,
        service_account_id: UUID,
    ) -> ServiceAccount | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM service_accounts WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(service_account_id)),
            ).fetchone()
        return _service_account_from_row(row) if row is not None else None

    async def list_service_accounts(
        self,
        organisation_id: UUID,
        *,
        limit: int = 500,
    ) -> list[ServiceAccount]:
        _require_limit(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM service_accounts WHERE organisation_id = ? "
                "ORDER BY name, id LIMIT ?",
                (str(organisation_id), limit),
            ).fetchall()
        return [_service_account_from_row(row) for row in rows]

    async def update_service_account(self, service_account: ServiceAccount) -> ServiceAccount:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE service_accounts SET name = ?, description = ?, status = ?, "
                "created_at = ?, updated_at = ?, last_used_at = ? "
                "WHERE organisation_id = ? AND id = ?",
                (
                    service_account.name,
                    service_account.description,
                    service_account.status.value,
                    _datetime_value(service_account.created_at),
                    _datetime_value(service_account.updated_at),
                    _optional_datetime_value(service_account.last_used_at),
                    str(service_account.organisation_id),
                    str(service_account.id),
                ),
            )
        _require_updated(cursor.rowcount, "Service account", service_account.id)
        return service_account

    async def revoke_service_account_and_credentials(
        self,
        service_account: ServiceAccount,
        revoked_at: datetime,
    ) -> tuple[ServiceAccount, list[ApiCredential]]:
        """Revoke an account and every live credential as one durable transition."""

        if service_account.status is not ServiceAccountStatus.REVOKED:
            raise ValueError("service account must be revoked")
        with self._database.transaction(immediate=True) as connection:
            credential_rows = connection.execute(
                "SELECT * FROM api_credentials WHERE organisation_id = ? "
                "AND principal_type = ? AND principal_id = ? AND revoked_at IS NULL",
                (
                    str(service_account.organisation_id),
                    PrincipalType.SERVICE_ACCOUNT.value,
                    str(service_account.id),
                ),
            ).fetchall()
            cursor = connection.execute(
                "UPDATE service_accounts SET name = ?, description = ?, status = ?, "
                "created_at = ?, updated_at = ?, last_used_at = ? "
                "WHERE organisation_id = ? AND id = ?",
                (
                    service_account.name,
                    service_account.description,
                    service_account.status.value,
                    _datetime_value(service_account.created_at),
                    _datetime_value(service_account.updated_at),
                    _optional_datetime_value(service_account.last_used_at),
                    str(service_account.organisation_id),
                    str(service_account.id),
                ),
            )
            connection.execute(
                "UPDATE api_credentials SET revoked_at = ? WHERE organisation_id = ? "
                "AND principal_type = ? AND principal_id = ? AND revoked_at IS NULL",
                (
                    _datetime_value(revoked_at),
                    str(service_account.organisation_id),
                    PrincipalType.SERVICE_ACCOUNT.value,
                    str(service_account.id),
                ),
            )
        _require_updated(cursor.rowcount, "Service account", service_account.id)
        revoked_credentials = [
            _api_credential_from_row(row).model_copy(update={"revoked_at": revoked_at})
            for row in credential_rows
        ]
        return service_account, revoked_credentials

    async def create_session(self, session: UserSession) -> UserSession:
        with self._database.transaction(immediate=True) as connection:
            _require_user_in_organisation(connection, session.organisation_id, session.user_id)
            connection.execute(
                "INSERT INTO user_sessions "
                "(id, user_id, organisation_id, secret_hash, created_at, expires_at, "
                "maximum_expires_at, last_seen_at, revoked_at, user_agent, ip_address) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _session_values(session),
            )
        return session

    async def get_session(self, session_id: UUID) -> UserSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM user_sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def get_session_for_organisation(
        self,
        organisation_id: UUID,
        session_id: UUID,
    ) -> UserSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM user_sessions WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(session_id)),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def list_sessions(
        self,
        organisation_id: UUID,
        user_id: UUID,
    ) -> list[UserSession]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM user_sessions WHERE organisation_id = ? AND user_id = ? "
                "ORDER BY created_at DESC, id",
                (str(organisation_id), str(user_id)),
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    async def touch_session(
        self,
        session_id: UUID,
        *,
        expected_secret_hash: str,
        last_seen_at: datetime,
    ) -> bool:
        """Advance last-seen without ever writing a stale session snapshot."""

        last_seen_value = _datetime_value(last_seen_at)
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE id = ? "
                "AND secret_hash = ? AND revoked_at IS NULL AND last_seen_at < ?",
                (
                    last_seen_value,
                    str(session_id),
                    expected_secret_hash,
                    last_seen_value,
                ),
            )
        return cursor.rowcount == 1

    async def rotate_session(
        self,
        session_id: UUID,
        *,
        expected_secret_hash: str,
        secret_hash: str,
        expires_at: datetime,
        last_seen_at: datetime,
    ) -> UserSession | None:
        """Rotate a session secret exactly once for the currently presented secret."""

        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE user_sessions SET secret_hash = ?, expires_at = ?, last_seen_at = ? "
                "WHERE id = ? AND secret_hash = ? AND revoked_at IS NULL",
                (
                    secret_hash,
                    _datetime_value(expires_at),
                    _datetime_value(last_seen_at),
                    str(session_id),
                    expected_secret_hash,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM user_sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def revoke_session(
        self,
        session_id: UUID,
        *,
        revoked_at: datetime,
        expected_secret_hash: str | None = None,
    ) -> UserSession | None:
        """Revoke only the target row, optionally fencing on its current secret."""

        with self._database.transaction(immediate=True) as connection:
            if expected_secret_hash is None:
                connection.execute(
                    "UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (_datetime_value(revoked_at), str(session_id)),
                )
                row = connection.execute(
                    "SELECT * FROM user_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
            else:
                connection.execute(
                    "UPDATE user_sessions SET revoked_at = ? "
                    "WHERE id = ? AND secret_hash = ? AND revoked_at IS NULL",
                    (_datetime_value(revoked_at), str(session_id), expected_secret_hash),
                )
                row = connection.execute(
                    "SELECT * FROM user_sessions WHERE id = ? AND secret_hash = ?",
                    (str(session_id), expected_secret_hash),
                ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def create_api_credential(self, credential: ApiCredential) -> ApiCredential:
        with self._database.transaction(immediate=True) as connection:
            _require_principal_in_organisation(
                connection,
                credential.organisation_id,
                credential.principal_type,
                credential.principal_id,
            )
            connection.execute(
                "INSERT INTO api_credentials "
                "(id, organisation_id, principal_id, principal_type, name, secret_hash, "
                "created_at, expires_at, revoked_at, last_used_at, allowed_ip_ranges_json, "
                "permission_restrictions_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _api_credential_values(credential),
            )
        return credential

    async def get_api_credential(
        self,
        organisation_id: UUID,
        credential_id: UUID,
    ) -> ApiCredential | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM api_credentials WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(credential_id)),
            ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    async def get_api_credential_by_id(self, credential_id: UUID) -> ApiCredential | None:
        # The lp_<credential-id>_<secret> format permits indexed lookup by public ID.
        # Secret material is compared by the authentication service after this read.
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM api_credentials WHERE id = ?",
                (str(credential_id),),
            ).fetchone()
        return _api_credential_from_row(row) if row is not None else None

    async def lookup_api_credential(self, credential_id: UUID) -> ApiCredential | None:
        return await self.get_api_credential_by_id(credential_id)

    async def list_api_credentials(
        self,
        organisation_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
    ) -> list[ApiCredential]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM api_credentials WHERE organisation_id = ? "
                "AND principal_type = ? AND principal_id = ? ORDER BY created_at DESC, id",
                (str(organisation_id), principal_type.value, str(principal_id)),
            ).fetchall()
        return [_api_credential_from_row(row) for row in rows]

    async def update_api_credential(self, credential: ApiCredential) -> ApiCredential:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE api_credentials SET principal_id = ?, principal_type = ?, name = ?, "
                "secret_hash = ?, created_at = ?, expires_at = ?, revoked_at = ?, "
                "last_used_at = ?, allowed_ip_ranges_json = ?, permission_restrictions_json = ? "
                "WHERE organisation_id = ? AND id = ?",
                (
                    str(credential.principal_id),
                    credential.principal_type.value,
                    credential.name,
                    credential.secret_hash,
                    _datetime_value(credential.created_at),
                    _optional_datetime_value(credential.expires_at),
                    _optional_datetime_value(credential.revoked_at),
                    _optional_datetime_value(credential.last_used_at),
                    _json(sorted(credential.allowed_ip_ranges)),
                    _optional_json(
                        sorted(credential.permission_restrictions)
                        if credential.permission_restrictions is not None
                        else None
                    ),
                    str(credential.organisation_id),
                    str(credential.id),
                ),
            )
        _require_updated(cursor.rowcount, "API credential", credential.id)
        return credential

    async def create_role_assignment(self, assignment: RoleAssignment) -> RoleAssignment:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO role_assignments "
                "(id, organisation_id, subject_type, subject_id, role, resource_type, "
                "resource_id, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _role_assignment_values(assignment),
            )
        return assignment

    async def get_role_assignment(
        self,
        organisation_id: UUID,
        assignment_id: UUID,
    ) -> RoleAssignment | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM role_assignments WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(assignment_id)),
            ).fetchone()
        return _role_assignment_from_row(row) if row is not None else None

    async def list_role_assignments(
        self,
        organisation_id: UUID,
        subjects: Collection[tuple[RoleSubjectType, UUID]] | None = None,
    ) -> Sequence[RoleAssignment]:
        parameters: list[object] = [str(organisation_id)]
        subject_clause = ""
        if subjects is not None:
            ordered = sorted(subjects, key=lambda item: (item[0].value, str(item[1])))
            if not ordered:
                return []
            subject_clause = (
                " AND ("
                + " OR ".join("(subject_type = ? AND subject_id = ?)" for _ in ordered)
                + ")"
            )
            for subject_type, subject_id in ordered:
                parameters.extend((subject_type.value, str(subject_id)))
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM role_assignments WHERE organisation_id = ?"
                + subject_clause
                + " ORDER BY created_at, id",
                parameters,
            ).fetchall()
        return [_role_assignment_from_row(row) for row in rows]

    async def delete_role_assignment(
        self,
        organisation_id: UUID,
        assignment_id: UUID,
    ) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM role_assignments WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(assignment_id)),
            )
        return cursor.rowcount == 1

    async def create_authorisation_snapshot(
        self,
        organisation_id: UUID,
        snapshot: AuthorisationSnapshot,
    ) -> AuthorisationSnapshot:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO authorisation_snapshots "
                "(id, organisation_id, principal_id, permission, resource_type, resource_id, "
                "granted_by_assignments_json, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(snapshot.id),
                    str(organisation_id),
                    str(snapshot.principal_id),
                    snapshot.permission,
                    (
                        snapshot.resource_type.value
                        if isinstance(snapshot.resource_type, ResourceType)
                        else snapshot.resource_type
                    ),
                    snapshot.resource_id,
                    _json([str(item) for item in snapshot.granted_by_assignments]),
                    _datetime_value(snapshot.evaluated_at),
                ),
            )
        return snapshot

    async def get_authorisation_snapshot(
        self,
        organisation_id: UUID,
        snapshot_id: UUID,
    ) -> AuthorisationSnapshot | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM authorisation_snapshots WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(snapshot_id)),
            ).fetchone()
        return _authorisation_snapshot_from_row(row) if row is not None else None

    async def set_bench_access_policy(
        self,
        organisation_id: UUID,
        policy: BenchAccessPolicy,
    ) -> BenchAccessPolicy:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO bench_access_policies "
                "(organisation_id, bench_id, visibility, reservation_role, operation_role, "
                "allowed_team_ids_json) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(organisation_id, bench_id) DO UPDATE SET "
                "visibility = excluded.visibility, reservation_role = excluded.reservation_role, "
                "operation_role = excluded.operation_role, "
                "allowed_team_ids_json = excluded.allowed_team_ids_json",
                (
                    str(organisation_id),
                    policy.bench_id,
                    policy.visibility.value,
                    policy.reservation_role.value if policy.reservation_role is not None else None,
                    policy.operation_role.value if policy.operation_role is not None else None,
                    _json(sorted(str(item) for item in policy.allowed_team_ids)),
                ),
            )
        return policy

    async def get_bench_access_policy(
        self,
        organisation_id: UUID,
        bench_id: str,
    ) -> BenchAccessPolicy | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM bench_access_policies WHERE organisation_id = ? AND bench_id = ?",
                (str(organisation_id), bench_id),
            ).fetchone()
        return _bench_policy_from_row(row) if row is not None else None

    async def set_workflow_access_policy(
        self,
        organisation_id: UUID,
        policy: WorkflowAccessPolicy,
    ) -> WorkflowAccessPolicy:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO workflow_access_policies "
                "(organisation_id, workflow_id, visibility) VALUES (?, ?, ?) "
                "ON CONFLICT(organisation_id, workflow_id) DO UPDATE SET "
                "visibility = excluded.visibility",
                (str(organisation_id), policy.workflow_id, policy.visibility.value),
            )
        return policy

    async def get_workflow_access_policy(
        self,
        organisation_id: UUID,
        workflow_id: str,
    ) -> WorkflowAccessPolicy | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_access_policies "
                "WHERE organisation_id = ? AND workflow_id = ?",
                (str(organisation_id), workflow_id),
            ).fetchone()
        return _workflow_policy_from_row(row) if row is not None else None

    async def create_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO audit_events "
                "(id, organisation_id, timestamp, actor_type, actor_id, actor_display_name, "
                "action, resource_type, resource_id, outcome, request_id, source_ip, user_agent, "
                "reason, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _audit_event_values(event),
            )
        return event

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        return await self.create_audit_event(event)

    async def get_audit_event(
        self,
        organisation_id: UUID,
        event_id: UUID,
    ) -> AuditEvent | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE organisation_id = ? AND id = ?",
                (str(organisation_id), str(event_id)),
            ).fetchone()
        return _audit_event_from_row(row) if row is not None else None

    async def list_audit_events(
        self,
        organisation_id: UUID,
        *,
        actor: str | None = None,
        actor_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: AuditOutcome | None = None,
        request_id: UUID | None = None,
        command_id: UUID | None = None,
        operation_id: UUID | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        cursor: UUID | None = None,
        limit: int = 500,
    ) -> list[AuditEvent]:
        _require_limit(limit)
        conditions = ["organisation_id = ?"]
        parameters: list[object] = [str(organisation_id)]
        filters: tuple[tuple[str, object | None], ...] = (
            ("actor_id = ?", str(actor_id) if actor_id is not None else None),
            ("action = ?", action),
            ("resource_type = ?", resource_type),
            ("resource_id = ?", resource_id),
            ("outcome = ?", outcome.value if outcome is not None else None),
            ("request_id = ?", str(request_id) if request_id is not None else None),
            (
                "json_extract(metadata_json, '$.command_id') = ?",
                str(command_id) if command_id is not None else None,
            ),
            (
                "json_extract(metadata_json, '$.operation_id') = ?",
                str(operation_id) if operation_id is not None else None,
            ),
            ("timestamp > ?", _optional_datetime_value(after)),
            ("timestamp < ?", _optional_datetime_value(before)),
        )
        if actor is not None:
            conditions.append("instr(lower(coalesce(actor_display_name, '')), lower(?)) > 0")
            parameters.append(actor)
        if cursor is not None:
            with self._database.transaction() as connection:
                cursor_row = connection.execute(
                    "SELECT timestamp, id FROM audit_events WHERE organisation_id = ? AND id = ?",
                    (str(organisation_id), str(cursor)),
                ).fetchone()
            if cursor_row is None:
                return []
            conditions.append("(timestamp < ? OR (timestamp = ? AND id > ?))")
            parameters.extend((cursor_row["timestamp"], cursor_row["timestamp"], cursor_row["id"]))
        for condition, value in filters:
            if value is not None:
                conditions.append(condition)
                parameters.append(value)
        parameters.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE "
                + " AND ".join(conditions)
                + " ORDER BY timestamp DESC, id LIMIT ?",
                parameters,
            ).fetchall()
        return [_audit_event_from_row(row) for row in rows]

    async def prune_audit_events_for_retention(
        self,
        before: datetime,
        *,
        batch_size: int = 1_000,
        organisation_id: UUID | None = None,
    ) -> int:
        """Remove one bounded retention batch outside ordinary audit APIs.

        Omitting ``organisation_id`` deliberately performs control-plane-wide
        maintenance. Supplying it keeps both the candidate selection and deletion
        constrained to that organisation.
        """

        _require_limit(batch_size)
        conditions = ["timestamp < ?"]
        parameters: list[object] = [_datetime_value(before)]
        if organisation_id is not None:
            conditions.append("organisation_id = ?")
            parameters.append(str(organisation_id))
        parameters.append(batch_size)
        with self._database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM audit_events WHERE "
                + " AND ".join(conditions)
                + " ORDER BY timestamp, id LIMIT ?",
                parameters,
            ).fetchall()
            event_ids = [str(row["id"]) for row in rows]
            if not event_ids:
                return 0
            placeholders = ", ".join("?" for _ in event_ids)
            delete_parameters: list[object] = list(event_ids)
            organisation_condition = ""
            if organisation_id is not None:
                organisation_condition = "organisation_id = ? AND "
                delete_parameters.insert(0, str(organisation_id))
            cursor = connection.execute(
                "DELETE FROM audit_events WHERE "
                + organisation_condition
                + f"id IN ({placeholders})",  # noqa: S608 - placeholders are generated only
                delete_parameters,
            )
        return int(cursor.rowcount)

    async def record_login_attempt(self, attempt: LoginAttempt) -> LoginAttempt:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO login_attempts "
                "(id, organisation_slug, username, ip_address, attempted_at, succeeded) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(attempt.id),
                    attempt.organisation_slug,
                    attempt.username,
                    attempt.ip_address,
                    _datetime_value(attempt.attempted_at),
                    int(attempt.succeeded),
                ),
            )
        return attempt

    async def count_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
        since: datetime,
    ) -> int:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS attempt_count FROM login_attempts "
                "WHERE LOWER(organisation_slug) = LOWER(?) AND LOWER(username) = LOWER(?) "
                "AND ip_address IS ? AND succeeded = 0 AND attempted_at >= ?",
                (organisation_slug, username, ip_address, _datetime_value(since)),
            ).fetchone()
        return int(row["attempt_count"]) if row is not None else 0

    async def clear_failed_login_attempts(
        self,
        organisation_slug: str,
        username: str,
        ip_address: str | None,
    ) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE LOWER(organisation_slug) = LOWER(?) "
                "AND LOWER(username) = LOWER(?) AND ip_address IS ? AND succeeded = 0",
                (organisation_slug, username, ip_address),
            )

    async def prune_login_attempts_for_retention(
        self,
        before: datetime,
        *,
        batch_size: int = 1_000,
        organisation_slug: str | None = None,
    ) -> int:
        """Remove one bounded batch of attempts no longer used by rate limiting."""

        _require_limit(batch_size)
        conditions = ["attempted_at < ?"]
        parameters: list[object] = [_datetime_value(before)]
        if organisation_slug is not None:
            conditions.append("LOWER(organisation_slug) = LOWER(?)")
            parameters.append(organisation_slug)
        parameters.append(batch_size)
        with self._database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM login_attempts WHERE "
                + " AND ".join(conditions)
                + " ORDER BY attempted_at, id LIMIT ?",
                parameters,
            ).fetchall()
            attempt_ids = [str(row["id"]) for row in rows]
            if not attempt_ids:
                return 0
            placeholders = ", ".join("?" for _ in attempt_ids)
            delete_parameters: list[object] = list(attempt_ids)
            organisation_condition = ""
            if organisation_slug is not None:
                organisation_condition = "LOWER(organisation_slug) = LOWER(?) AND "
                delete_parameters.insert(0, organisation_slug)
            cursor = connection.execute(
                "DELETE FROM login_attempts WHERE "
                + organisation_condition
                + f"id IN ({placeholders})",  # noqa: S608 - placeholders are generated only
                delete_parameters,
            )
        return int(cursor.rowcount)


def _organisation_from_row(row: Any) -> Organisation:
    return Organisation(
        id=UUID(str(row["id"])),
        slug=str(row["slug"]),
        name=str(row["name"]),
        status=OrganisationStatus(str(row["status"])),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _user_values(user: User) -> tuple[object, ...]:
    return (
        str(user.id),
        str(user.organisation_id),
        user.username,
        user.display_name,
        user.email,
        user.status.value,
        user.authentication_source.value,
        _datetime_value(user.created_at),
        _datetime_value(user.updated_at),
        _optional_datetime_value(user.last_login_at),
    )


def _user_from_row(row: Any) -> User:
    return User(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        email=str(row["email"]) if row["email"] is not None else None,
        status=UserStatus(str(row["status"])),
        authentication_source=AuthenticationSource(str(row["authentication_source"])),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        last_login_at=_optional_datetime(row["last_login_at"]),
    )


def _password_values(credential: PasswordCredential) -> tuple[object, ...]:
    return (
        str(credential.user_id),
        credential.password_hash,
        _datetime_value(credential.created_at),
        _datetime_value(credential.updated_at),
    )


def _password_from_row(row: Any) -> PasswordCredential:
    return PasswordCredential(
        user_id=UUID(str(row["user_id"])),
        password_hash=str(row["password_hash"]),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _organisation_membership_values(
    membership: OrganisationMembership,
) -> tuple[object, ...]:
    return (
        str(membership.id),
        str(membership.organisation_id),
        str(membership.user_id),
        membership.role.value,
        _datetime_value(membership.created_at),
    )


def _organisation_membership_from_row(row: Any) -> OrganisationMembership:
    return OrganisationMembership(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        user_id=UUID(str(row["user_id"])),
        role=OrganisationRole(str(row["role"])),
        created_at=_datetime(row["created_at"]),
    )


def _team_values(team: Team) -> tuple[object, ...]:
    return (
        str(team.id),
        str(team.organisation_id),
        team.slug,
        team.name,
        team.description,
        _datetime_value(team.created_at),
        _datetime_value(team.updated_at),
    )


def _team_from_row(row: Any) -> Team:
    return Team(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        slug=str(row["slug"]),
        name=str(row["name"]),
        description=str(row["description"]) if row["description"] is not None else None,
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _team_membership_values(membership: TeamMembership) -> tuple[object, ...]:
    return (
        str(membership.id),
        str(membership.team_id),
        str(membership.user_id),
        membership.role.value,
        _datetime_value(membership.created_at),
    )


def _team_membership_from_row(row: Any) -> TeamMembership:
    return TeamMembership(
        id=UUID(str(row["id"])),
        team_id=UUID(str(row["team_id"])),
        user_id=UUID(str(row["user_id"])),
        role=TeamRole(str(row["role"])),
        created_at=_datetime(row["created_at"]),
    )


def _service_account_values(account: ServiceAccount) -> tuple[object, ...]:
    return (
        str(account.id),
        str(account.organisation_id),
        account.name,
        account.description,
        account.status.value,
        _datetime_value(account.created_at),
        _datetime_value(account.updated_at),
        _optional_datetime_value(account.last_used_at),
    )


def _service_account_from_row(row: Any) -> ServiceAccount:
    return ServiceAccount(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        name=str(row["name"]),
        description=str(row["description"]) if row["description"] is not None else None,
        status=ServiceAccountStatus(str(row["status"])),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        last_used_at=_optional_datetime(row["last_used_at"]),
    )


def _session_values(session: UserSession) -> tuple[object, ...]:
    return (
        str(session.id),
        str(session.user_id),
        str(session.organisation_id),
        session.secret_hash,
        _datetime_value(session.created_at),
        _datetime_value(session.expires_at),
        _datetime_value(session.maximum_expires_at),
        _datetime_value(session.last_seen_at),
        _optional_datetime_value(session.revoked_at),
        session.user_agent,
        session.ip_address,
    )


def _session_from_row(row: Any) -> UserSession:
    return UserSession(
        id=UUID(str(row["id"])),
        user_id=UUID(str(row["user_id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        secret_hash=str(row["secret_hash"]),
        created_at=_datetime(row["created_at"]),
        expires_at=_datetime(row["expires_at"]),
        maximum_expires_at=_datetime(row["maximum_expires_at"]),
        last_seen_at=_datetime(row["last_seen_at"]),
        revoked_at=_optional_datetime(row["revoked_at"]),
        user_agent=str(row["user_agent"]) if row["user_agent"] is not None else None,
        ip_address=str(row["ip_address"]) if row["ip_address"] is not None else None,
    )


def _api_credential_values(credential: ApiCredential) -> tuple[object, ...]:
    return (
        str(credential.id),
        str(credential.organisation_id),
        str(credential.principal_id),
        credential.principal_type.value,
        credential.name,
        credential.secret_hash,
        _datetime_value(credential.created_at),
        _optional_datetime_value(credential.expires_at),
        _optional_datetime_value(credential.revoked_at),
        _optional_datetime_value(credential.last_used_at),
        _json(sorted(credential.allowed_ip_ranges)),
        _optional_json(
            sorted(credential.permission_restrictions)
            if credential.permission_restrictions is not None
            else None
        ),
    )


def _api_credential_from_row(row: Any) -> ApiCredential:
    restrictions = _optional_json_array(row["permission_restrictions_json"])
    return ApiCredential(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        principal_id=UUID(str(row["principal_id"])),
        principal_type=PrincipalType(str(row["principal_type"])),
        name=str(row["name"]),
        secret_hash=str(row["secret_hash"]),
        created_at=_datetime(row["created_at"]),
        expires_at=_optional_datetime(row["expires_at"]),
        revoked_at=_optional_datetime(row["revoked_at"]),
        last_used_at=_optional_datetime(row["last_used_at"]),
        allowed_ip_ranges=[str(item) for item in _json_array(row["allowed_ip_ranges_json"])],
        permission_restrictions=(
            {str(item) for item in restrictions} if restrictions is not None else None
        ),
    )


def _role_assignment_values(assignment: RoleAssignment) -> tuple[object, ...]:
    return (
        str(assignment.id),
        str(assignment.organisation_id),
        assignment.subject_type.value,
        str(assignment.subject_id),
        assignment.role.value,
        assignment.resource_type.value,
        assignment.resource_id,
        str(assignment.created_by),
        _datetime_value(assignment.created_at),
        _optional_datetime_value(assignment.expires_at),
    )


def _role_assignment_from_row(row: Any) -> RoleAssignment:
    return RoleAssignment(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        subject_type=RoleSubjectType(str(row["subject_type"])),
        subject_id=UUID(str(row["subject_id"])),
        role=RoleName(str(row["role"])),
        resource_type=ResourceType(str(row["resource_type"])),
        resource_id=str(row["resource_id"]),
        created_by=UUID(str(row["created_by"])),
        created_at=_datetime(row["created_at"]),
        expires_at=_optional_datetime(row["expires_at"]),
    )


def _authorisation_snapshot_from_row(row: Any) -> AuthorisationSnapshot:
    resource_type = str(row["resource_type"])
    return AuthorisationSnapshot(
        id=UUID(str(row["id"])),
        principal_id=UUID(str(row["principal_id"])),
        permission=str(row["permission"]),
        resource_type=(
            "CI_SESSION" if resource_type == "CI_SESSION" else ResourceType(resource_type)
        ),
        resource_id=str(row["resource_id"]),
        granted_by_assignments=[
            UUID(str(item)) for item in _json_array(row["granted_by_assignments_json"])
        ],
        evaluated_at=_datetime(row["evaluated_at"]),
    )


def _bench_policy_from_row(row: Any) -> BenchAccessPolicy:
    return BenchAccessPolicy(
        bench_id=str(row["bench_id"]),
        visibility=BenchVisibility(str(row["visibility"])),
        reservation_role=(
            RoleName(str(row["reservation_role"])) if row["reservation_role"] is not None else None
        ),
        operation_role=(
            RoleName(str(row["operation_role"])) if row["operation_role"] is not None else None
        ),
        allowed_team_ids={UUID(str(item)) for item in _json_array(row["allowed_team_ids_json"])},
    )


def _workflow_policy_from_row(row: Any) -> WorkflowAccessPolicy:
    return WorkflowAccessPolicy(
        workflow_id=str(row["workflow_id"]),
        visibility=WorkflowVisibility(str(row["visibility"])),
    )


def _audit_event_values(event: AuditEvent) -> tuple[object, ...]:
    payload = event.model_dump(mode="json")
    return (
        str(event.id),
        str(event.organisation_id),
        _datetime_value(event.timestamp),
        event.actor_type.value if event.actor_type is not None else None,
        str(event.actor_id) if event.actor_id is not None else None,
        event.actor_display_name,
        event.action,
        event.resource_type,
        event.resource_id,
        event.outcome.value,
        str(event.request_id) if event.request_id is not None else None,
        event.source_ip,
        event.user_agent,
        event.reason,
        _json(payload["metadata"]),
    )


def _audit_event_from_row(row: Any) -> AuditEvent:
    return AuditEvent(
        id=UUID(str(row["id"])),
        organisation_id=UUID(str(row["organisation_id"])),
        timestamp=_datetime(row["timestamp"]),
        actor_type=(
            PrincipalType(str(row["actor_type"])) if row["actor_type"] is not None else None
        ),
        actor_id=UUID(str(row["actor_id"])) if row["actor_id"] is not None else None,
        actor_display_name=(
            str(row["actor_display_name"]) if row["actor_display_name"] is not None else None
        ),
        action=str(row["action"]),
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]) if row["resource_id"] is not None else None,
        outcome=AuditOutcome(str(row["outcome"])),
        request_id=UUID(str(row["request_id"])) if row["request_id"] is not None else None,
        source_ip=str(row["source_ip"]) if row["source_ip"] is not None else None,
        user_agent=str(row["user_agent"]) if row["user_agent"] is not None else None,
        reason=str(row["reason"]) if row["reason"] is not None else None,
        metadata=_json_object(row["metadata_json"]),
    )


def _require_user_in_organisation(
    connection: Any,
    organisation_id: UUID,
    user_id: UUID,
) -> None:
    row = connection.execute(
        "SELECT 1 FROM users WHERE organisation_id = ? AND id = ?",
        (str(organisation_id), str(user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("User does not belong to the organisation")


def _require_team_in_organisation(
    connection: Any,
    organisation_id: UUID,
    team_id: UUID,
) -> None:
    row = connection.execute(
        "SELECT 1 FROM teams WHERE organisation_id = ? AND id = ?",
        (str(organisation_id), str(team_id)),
    ).fetchone()
    if row is None:
        raise ValueError("Team does not belong to the organisation")


def _require_principal_in_organisation(
    connection: Any,
    organisation_id: UUID,
    principal_type: PrincipalType,
    principal_id: UUID,
) -> None:
    table = "users" if principal_type is PrincipalType.USER else "service_accounts"
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE organisation_id = ? AND id = ?",  # noqa: S608
        (str(organisation_id), str(principal_id)),
    ).fetchone()
    if row is None:
        raise ValueError("Principal does not belong to the organisation")


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_datetime(value: object | None) -> datetime | None:
    return _datetime(value) if value is not None else None


def _datetime_value(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Persistence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _optional_datetime_value(value: datetime | None) -> str | None:
    return _datetime_value(value) if value is not None else None


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _optional_json(value: object | None) -> str | None:
    return _json(value) if value is not None else None


def _json_array(value: object) -> list[object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("Persisted JSON value is not an array")
    return parsed


def _optional_json_array(value: object | None) -> list[object] | None:
    return _json_array(value) if value is not None else None


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("Persisted JSON value is not an object")
    return {str(key): item for key, item in parsed.items()}


def _require_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("limit must be positive")


def _require_updated(rowcount: int, resource: str, resource_id: UUID) -> None:
    if rowcount != 1:
        raise LookupError(f"{resource} {resource_id} does not exist in its organisation")


__all__ = ["SQLiteIdentityRepository"]
