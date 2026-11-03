from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane_core.reservations import ReservationGrantRequest
from lab_platform.models import (
    LEGACY_ORGANISATION_ID,
    ApiCredential,
    ArtifactOwnerType,
    ArtifactRecord,
    AuditEvent,
    AuditOutcome,
    AuthenticationSource,
    AuthorisationSnapshot,
    BenchAccessPolicy,
    BenchVisibility,
    CiProvider,
    CiSession,
    DistributedOperation,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    LoginAttempt,
    Organisation,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandType,
    Reservation,
    ReservationStatus,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    ServiceAccount,
    Team,
    TeamMembership,
    TeamRole,
    User,
    UserSession,
    WorkflowAccessPolicy,
    WorkflowDefinition,
    WorkflowVisibility,
)
from lab_platform.models.agents import AgentStatus
from lab_platform.persistence import (
    DEFAULT_ORGANISATION_ID,
    SQLiteAgentEnrollmentRepository,
    SQLiteCentralReservationLeaseRepository,
    SQLiteCiSessionRepository,
    SQLiteDatabase,
    SQLiteGenericArtifactRepository,
    SQLiteIdentityRepository,
    SQLiteWorkflowRepository,
)
from lab_platform.persistence.distributed import (
    SQLiteDistributedOperationRepository,
    SQLiteGlobalBenchRepository,
    SQLiteRemoteArtifactRepository,
    SQLiteRemoteCommandRepository,
)

from tests.support.persistence import open_sqlite_database

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


DEFAULT_ORG_ID = UUID(DEFAULT_ORGANISATION_ID)


SECOND_ORG_ID = UUID("00000000-0000-0000-0000-000000000002")


def _user(
    user_id: int,
    organisation_id: UUID = DEFAULT_ORG_ID,
    *,
    username: str = "alice",
    authentication_source: AuthenticationSource = AuthenticationSource.LOCAL,
) -> User:
    return User(
        id=UUID(int=user_id),
        organisation_id=organisation_id,
        username=username,
        display_name=username.title(),
        email=f"{username.casefold()}@example.test",
        authentication_source=authentication_source,
        created_at=NOW,
        updated_at=NOW,
    )


def test_atomic_user_bootstrap_password_membership_and_tenant_scoping(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "users.db")
        repository = SQLiteIdentityRepository(database)
        default = await repository.ensure_default_organisation(
            slug="engineering",
            name="Engineering",
        )
        assert default.id == DEFAULT_ORG_ID
        assert default.slug == "engineering"
        assert (
            await repository.ensure_default_organisation(
                slug="engineering",
                name="Engineering",
            )
            == default
        )

        alice = _user(101)
        password = PasswordCredential(
            user_id=alice.id,
            password_hash="scrypt$v1$" + "a" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        membership = OrganisationMembership(
            id=UUID(int=102),
            organisation_id=DEFAULT_ORG_ID,
            user_id=alice.id,
            role=OrganisationRole.OWNER,
            created_at=NOW,
        )
        assert await repository.create_user_with_password(alice, password, membership) == alice
        assert await repository.get_user(DEFAULT_ORG_ID, alice.id) == alice
        assert await repository.get_user_by_username(DEFAULT_ORG_ID, "ALICE") == alice
        assert await repository.get_password_credential(alice.id) == password
        assert await repository.get_organisation_membership(DEFAULT_ORG_ID, alice.id) == membership
        assert await repository.count_organisation_owners(DEFAULT_ORG_ID) == 1

        oidc_user = _user(
            104,
            username="oidc-user",
            authentication_source=AuthenticationSource.OIDC,
        )
        oidc_membership = OrganisationMembership(
            id=UUID(int=105),
            organisation_id=DEFAULT_ORG_ID,
            user_id=oidc_user.id,
            role=OrganisationRole.MEMBER,
            created_at=NOW,
        )
        assert await repository.create_user_with_membership(oidc_user, oidc_membership) == oidc_user
        assert await repository.get_user(DEFAULT_ORG_ID, oidc_user.id) == oidc_user
        assert await repository.get_password_credential(oidc_user.id) is None
        assert (
            await repository.get_organisation_membership(DEFAULT_ORG_ID, oidc_user.id)
            == oidc_membership
        )
        with pytest.raises(ValueError, match="only be created for a local user"):
            await repository.create_user_with_password(
                oidc_user.model_copy(update={"id": UUID(int=106), "username": "bad-oidc"}),
                password.model_copy(update={"user_id": UUID(int=106)}),
                membership.model_copy(update={"user_id": UUID(int=106)}),
            )

        second = Organisation(
            id=SECOND_ORG_ID,
            slug="second",
            name="Second Organisation",
            created_at=NOW,
            updated_at=NOW,
        )
        await repository.create_organisation(second)
        other_alice = _user(103, SECOND_ORG_ID, username="Alice")
        await repository.create_user(other_alice)
        assert await repository.get_user(SECOND_ORG_ID, alice.id) is None
        assert await repository.get_user_by_username(SECOND_ORG_ID, "alice") == other_alice
        assert await repository.list_users(DEFAULT_ORG_ID) == [alice, oidc_user]
        assert await repository.list_users(SECOND_ORG_ID) == [other_alice]

        updated = alice.model_copy(
            update={"display_name": "Alice Operator", "updated_at": NOW + timedelta(minutes=1)}
        )
        assert await repository.update_user(updated) == updated
        assert await repository.get_user(DEFAULT_ORG_ID, alice.id) == updated
        database.close()

    asyncio.run(scenario())


def test_team_membership_and_role_assignment_adapters_are_organisation_scoped(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "authorisation.db")
        repository = SQLiteIdentityRepository(database)
        user = _user(201)
        await repository.create_user(user)
        team = Team(
            id=UUID(int=202),
            organisation_id=DEFAULT_ORG_ID,
            slug="embedded",
            name="Embedded",
            created_at=NOW,
            updated_at=NOW,
        )
        await repository.create_team(team)
        team_membership = TeamMembership(
            id=UUID(int=203),
            team_id=team.id,
            user_id=user.id,
            role=TeamRole.MANAGER,
            created_at=NOW,
        )
        await repository.create_team_membership(DEFAULT_ORG_ID, team_membership)
        assert await repository.get_team(DEFAULT_ORG_ID, team.id) == team
        assert await repository.get_team_by_slug(DEFAULT_ORG_ID, "EMBEDDED") == team
        assert await repository.list_team_memberships(DEFAULT_ORG_ID, team.id) == [team_membership]
        assert tuple(await repository.list_team_ids_for_user(DEFAULT_ORG_ID, user.id)) == (team.id,)
        assert await repository.list_team_ids_for_user(SECOND_ORG_ID, user.id) == ()

        direct = RoleAssignment(
            id=UUID(int=204),
            organisation_id=DEFAULT_ORG_ID,
            subject_type=RoleSubjectType.USER,
            subject_id=user.id,
            role=RoleName.VIEWER,
            resource_type=ResourceType.BENCH,
            resource_id="home-lab/esp32",
            created_by=user.id,
            created_at=NOW,
        )
        inherited = RoleAssignment(
            id=UUID(int=205),
            organisation_id=DEFAULT_ORG_ID,
            subject_type=RoleSubjectType.TEAM,
            subject_id=team.id,
            role=RoleName.OPERATOR,
            resource_type=ResourceType.AGENT,
            resource_id=str(UUID(int=999)),
            created_by=user.id,
            created_at=NOW,
        )
        await repository.create_role_assignment(direct)
        await repository.create_role_assignment(inherited)
        assignments = await repository.list_role_assignments(
            DEFAULT_ORG_ID,
            {
                (RoleSubjectType.USER, user.id),
                (RoleSubjectType.TEAM, team.id),
            },
        )
        assert set(assignments) == {direct, inherited}
        assert await repository.list_role_assignments(SECOND_ORG_ID, None) == []
        assert await repository.get_role_assignment(DEFAULT_ORG_ID, direct.id) == direct
        assert await repository.delete_role_assignment(DEFAULT_ORG_ID, direct.id)
        assert await repository.get_role_assignment(DEFAULT_ORG_ID, direct.id) is None

        second = Organisation(
            id=SECOND_ORG_ID,
            slug="second",
            name="Second Organisation",
            created_at=NOW,
            updated_at=NOW,
        )
        await repository.create_organisation(second)
        outsider = _user(206, SECOND_ORG_ID, username="outsider")
        await repository.create_user(outsider)
        with pytest.raises(ValueError, match="does not belong"):
            await repository.create_team_membership(
                DEFAULT_ORG_ID,
                TeamMembership(
                    id=UUID(int=207),
                    team_id=team.id,
                    user_id=outsider.id,
                    created_at=NOW,
                ),
            )
        database.close()

    asyncio.run(scenario())


def test_sessions_service_accounts_and_api_credentials_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "credentials.db")
        repository = SQLiteIdentityRepository(database)
        user = _user(301)
        await repository.create_user(user)
        session = UserSession(
            id=UUID(int=302),
            user_id=user.id,
            organisation_id=DEFAULT_ORG_ID,
            secret_hash="a" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            maximum_expires_at=NOW + timedelta(hours=12),
            last_seen_at=NOW,
            user_agent="labctl/test",
            ip_address="127.0.0.1",
        )
        await repository.create_session(session)
        assert await repository.get_session(session.id) == session
        assert await repository.get_session_for_organisation(DEFAULT_ORG_ID, session.id) == session
        assert await repository.get_session_for_organisation(SECOND_ORG_ID, session.id) is None
        assert await repository.list_sessions(DEFAULT_ORG_ID, user.id) == [session]
        revoked_at = NOW + timedelta(minutes=1)
        revoked = session.model_copy(update={"revoked_at": revoked_at})
        assert (
            await repository.revoke_session(
                session.id,
                revoked_at=revoked_at,
                expected_secret_hash=session.secret_hash,
            )
            == revoked
        )

        account = ServiceAccount(
            id=UUID(int=303),
            organisation_id=DEFAULT_ORG_ID,
            name="github-ci",
            description="Hardware CI",
            created_at=NOW,
            updated_at=NOW,
        )
        await repository.create_service_account(account)
        assert await repository.get_service_account(DEFAULT_ORG_ID, account.id) == account
        assert await repository.get_service_account(SECOND_ORG_ID, account.id) is None
        assert await repository.list_service_accounts(DEFAULT_ORG_ID) == [account]

        credential = ApiCredential(
            id=UUID(int=304),
            organisation_id=DEFAULT_ORG_ID,
            principal_id=account.id,
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            name="github-main",
            secret_hash="b" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(days=30),
            allowed_ip_ranges=["10.0.0.0/8"],
            permission_restrictions={"workflows:run", "artifacts:write"},
        )
        await repository.create_api_credential(credential)
        assert await repository.get_api_credential_by_id(credential.id) == credential
        assert await repository.lookup_api_credential(credential.id) == credential
        assert await repository.get_api_credential(DEFAULT_ORG_ID, credential.id) == credential
        assert await repository.get_api_credential(SECOND_ORG_ID, credential.id) is None
        assert await repository.list_api_credentials(
            DEFAULT_ORG_ID,
            PrincipalType.SERVICE_ACCOUNT,
            account.id,
        ) == [credential]
        used = credential.model_copy(update={"last_used_at": NOW + timedelta(minutes=2)})
        await repository.update_api_credential(used)
        assert await repository.get_api_credential_by_id(credential.id) == used
        assert not hasattr(repository, "get_api_credential_by_secret_hash")
        database.close()

    asyncio.run(scenario())


def test_session_rotation_fences_stale_touch_and_token_logout(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "session-cas.db")
        repository = SQLiteIdentityRepository(database)
        user = _user(311)
        await repository.create_user(user)
        session = UserSession(
            id=UUID(int=312),
            user_id=user.id,
            organisation_id=DEFAULT_ORG_ID,
            secret_hash="a" * 64,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            maximum_expires_at=NOW + timedelta(hours=12),
            last_seen_at=NOW,
        )
        await repository.create_session(session)

        assert await repository.touch_session(
            session.id,
            expected_secret_hash="a" * 64,
            last_seen_at=NOW + timedelta(seconds=1),
        )
        rotated = await repository.rotate_session(
            session.id,
            expected_secret_hash="a" * 64,
            secret_hash="b" * 64,
            expires_at=NOW + timedelta(minutes=20),
            last_seen_at=NOW + timedelta(seconds=2),
        )
        assert rotated is not None
        assert rotated.secret_hash == "b" * 64

        assert not await repository.touch_session(
            session.id,
            expected_secret_hash="a" * 64,
            last_seen_at=NOW + timedelta(seconds=3),
        )
        assert (
            await repository.revoke_session(
                session.id,
                revoked_at=NOW + timedelta(seconds=3),
                expected_secret_hash="a" * 64,
            )
            is None
        )
        current = await repository.get_session(session.id)
        assert current is not None
        assert current.secret_hash == "b" * 64
        assert current.last_seen_at == NOW + timedelta(seconds=2)
        assert current.revoked_at is None

        revoked = await repository.revoke_session(
            session.id,
            revoked_at=NOW + timedelta(seconds=4),
        )
        assert revoked is not None
        assert revoked.secret_hash == "b" * 64
        assert revoked.revoked_at == NOW + timedelta(seconds=4)
        database.close()

    asyncio.run(scenario())


def test_policies_snapshots_and_append_only_audit_are_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "audit.db")
        repository = SQLiteIdentityRepository(database)
        team_id = UUID(int=401)
        bench_policy = BenchAccessPolicy(
            bench_id="home-lab/esp32",
            visibility=BenchVisibility.RESTRICTED,
            reservation_role=RoleName.RESERVER,
            operation_role=RoleName.OPERATOR,
            allowed_team_ids={team_id},
        )
        await repository.set_bench_access_policy(DEFAULT_ORG_ID, bench_policy)
        assert (
            await repository.get_bench_access_policy(DEFAULT_ORG_ID, bench_policy.bench_id)
            == bench_policy
        )
        assert (
            await repository.get_bench_access_policy(SECOND_ORG_ID, bench_policy.bench_id) is None
        )

        workflow_policy = WorkflowAccessPolicy(
            workflow_id="esp32-smoke-test",
            visibility=WorkflowVisibility.RESTRICTED,
        )
        await repository.set_workflow_access_policy(DEFAULT_ORG_ID, workflow_policy)
        assert (
            await repository.get_workflow_access_policy(
                DEFAULT_ORG_ID,
                workflow_policy.workflow_id,
            )
            == workflow_policy
        )

        snapshot = AuthorisationSnapshot(
            id=UUID(int=402),
            principal_id=UUID(int=403),
            permission="benches:flash",
            resource_type=ResourceType.BENCH,
            resource_id=bench_policy.bench_id,
            granted_by_assignments=[UUID(int=404)],
            evaluated_at=NOW,
        )
        await repository.create_authorisation_snapshot(DEFAULT_ORG_ID, snapshot)
        assert await repository.get_authorisation_snapshot(DEFAULT_ORG_ID, snapshot.id) == snapshot
        assert await repository.get_authorisation_snapshot(SECOND_ORG_ID, snapshot.id) is None

        event = AuditEvent(
            id=UUID(int=405),
            organisation_id=DEFAULT_ORG_ID,
            timestamp=NOW,
            actor_type=PrincipalType.USER,
            actor_id=UUID(int=403),
            actor_display_name="Alice",
            action="BENCH_FLASH_REQUESTED",
            resource_type="BENCH",
            resource_id=bench_policy.bench_id,
            outcome=AuditOutcome.SUCCEEDED,
            request_id=UUID(int=406),
            source_ip="127.0.0.1",
            user_agent="labctl/test",
            metadata={"command_id": str(UUID(int=407))},
        )
        await repository.create_audit_event(event)
        assert await repository.get_audit_event(DEFAULT_ORG_ID, event.id) == event
        assert await repository.get_audit_event(SECOND_ORG_ID, event.id) is None
        assert await repository.list_audit_events(
            DEFAULT_ORG_ID,
            action="BENCH_FLASH_REQUESTED",
            outcome=AuditOutcome.SUCCEEDED,
        ) == [event]
        assert await repository.list_audit_events(DEFAULT_ORG_ID, actor="lic") == [event]
        assert await repository.list_audit_events(DEFAULT_ORG_ID, actor_id=event.actor_id) == [
            event
        ]
        assert await repository.list_audit_events(
            DEFAULT_ORG_ID,
            resource_type="BENCH",
            resource_id=bench_policy.bench_id,
            request_id=event.request_id,
            command_id=UUID(int=407),
        ) == [event]
        assert (
            await repository.list_audit_events(
                DEFAULT_ORG_ID,
                operation_id=UUID(int=408),
            )
            == []
        )
        assert not hasattr(repository, "update_audit_event")
        assert not hasattr(repository, "delete_audit_event")
        database.close()

    asyncio.run(scenario())


def test_login_attempt_rate_limit_queries_and_clear_are_subject_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = open_sqlite_database(tmp_path / "login-attempts.db")
        repository = SQLiteIdentityRepository(database)
        attempts = [
            LoginAttempt(
                id=UUID(int=501),
                organisation_slug="default",
                username="alice",
                ip_address="127.0.0.1",
                attempted_at=NOW,
                succeeded=False,
            ),
            LoginAttempt(
                id=UUID(int=502),
                organisation_slug="default",
                username="ALICE",
                ip_address="127.0.0.1",
                attempted_at=NOW + timedelta(seconds=1),
                succeeded=False,
            ),
            LoginAttempt(
                id=UUID(int=503),
                organisation_slug="default",
                username="alice",
                ip_address="127.0.0.1",
                attempted_at=NOW + timedelta(seconds=2),
                succeeded=True,
            ),
        ]
        for attempt in attempts:
            await repository.record_login_attempt(attempt)
        assert (
            await repository.count_failed_login_attempts(
                "DEFAULT",
                "Alice",
                "127.0.0.1",
                NOW - timedelta(minutes=1),
            )
            == 2
        )
        assert (
            await repository.count_failed_login_attempts(
                "default",
                "alice",
                "10.0.0.1",
                NOW - timedelta(minutes=1),
            )
            == 0
        )
        await repository.clear_failed_login_attempts(
            "default",
            "alice",
            "127.0.0.1",
        )
        assert (
            await repository.count_failed_login_attempts(
                "default",
                "alice",
                "127.0.0.1",
                NOW - timedelta(minutes=1),
            )
            == 0
        )
        database.close()

    asyncio.run(scenario())


NOW_OPERATIONAL_TENANT_PERSISTENCE = datetime(2026, 8, 9, 12, tzinfo=UTC)


SECOND_ORGANISATION_ID = UUID("10000000-0000-0000-0000-000000000002")


def _database_operational_tenant_persistence(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO organisations (id, slug, name, status, created_at, updated_at) "
            "VALUES (?, 'second', 'Second Lab', 'ACTIVE', ?, ?)",
            (
                str(SECOND_ORGANISATION_ID),
                NOW_OPERATIONAL_TENANT_PERSISTENCE.isoformat(),
                NOW_OPERATIONAL_TENANT_PERSISTENCE.isoformat(),
            ),
        )
    return database


def _seed_agent(database: SQLiteDatabase) -> UUID:
    agent_id = uuid4()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, organisation_id, slug, name, status, version, protocol_version, "
            "labels_json, registered_at, enrollment_status) "
            "VALUES (?, ?, 'second-agent', 'Second Agent', 'ONLINE', '0.7.0-alpha', "
            "'1.0', '{}', ?, 'ENROLLED')",
            (
                str(agent_id),
                str(SECOND_ORGANISATION_ID),
                NOW_OPERATIONAL_TENANT_PERSISTENCE.isoformat(),
            ),
        )
    return agent_id


def test_distributed_resources_are_hydrated_and_filtered_by_organisation(
    tmp_path: Path,
) -> None:
    asyncio.run(_distributed_resources_are_hydrated_and_filtered(tmp_path))


async def _distributed_resources_are_hydrated_and_filtered(
    tmp_path: Path,
) -> None:
    database = _database_operational_tenant_persistence(tmp_path / "tenant-distributed.db")
    agent_id = _seed_agent(database)
    agents = SQLiteAgentEnrollmentRepository(database)
    assert await agents.get_agent(agent_id, organisation_id=LEGACY_ORGANISATION_ID) is None
    agent = await agents.get_agent(agent_id, organisation_id=SECOND_ORGANISATION_ID)
    assert agent is not None and agent.organisation_id == SECOND_ORGANISATION_ID

    bench = GlobalBenchRecord(
        id="second-agent/bench-1",
        organisation_id=SECOND_ORGANISATION_ID,
        agent_id=agent_id,
        agent_slug="second-agent",
        local_bench_id="bench-1",
        name="Second Bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
        updated_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    benches = SQLiteGlobalBenchRepository(database)
    assert await benches.upsert(bench) == bench
    assert await benches.get(bench.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await benches.list(organisation_id=SECOND_ORGANISATION_ID) == [bench]

    reservations = SQLiteCentralReservationLeaseRepository(database)
    result = await reservations.grant_if_eligible(
        ReservationGrantRequest(
            reservation=Reservation(
                id=uuid4(),
                bench_id=bench.id,
                owner="second-owner",
                status=ReservationStatus.SCHEDULED,
                created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
                starts_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
                ends_at=NOW_OPERATIONAL_TENANT_PERSISTENCE + timedelta(hours=1),
            ),
            agent_id=agent_id,
            lease_valid_until=NOW_OPERATIONAL_TENANT_PERSISTENCE + timedelta(minutes=10),
        ),
        mutation_key="tenant-reservation",
        request_fingerprint="d" * 64,
        expected_agent_status=AgentStatus.ONLINE,
        expected_bench_status=GlobalBenchStatus.ONLINE,
    )
    assert result is not None
    reservation_id = result.record.reservation.id
    assert result.record.reservation.organisation_id == SECOND_ORGANISATION_ID
    assert await reservations.get(reservation_id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert len(await reservations.list(organisation_id=SECOND_ORGANISATION_ID)) == 1

    command = RemoteCommand(
        organisation_id=SECOND_ORGANISATION_ID,
        agent_id=agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RESET,
        expires_at=NOW_OPERATIONAL_TENANT_PERSISTENCE + timedelta(hours=1),
        idempotency_key="tenant-command",
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    command_repository = SQLiteRemoteCommandRepository(database)
    await command_repository.create(command)
    operation = DistributedOperation(
        organisation_id=SECOND_ORGANISATION_ID,
        remote_command_id=command.id,
        agent_id=agent_id,
        bench_id=bench.id,
        operation_type="reset",
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    operation_repository = SQLiteDistributedOperationRepository(database)
    await operation_repository.create(operation)
    assert (
        await operation_repository.get(
            operation.id,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    assert await operation_repository.list(organisation_id=SECOND_ORGANISATION_ID) == [operation]

    reported = RemoteArtifactMetadata(
        agent_id=agent_id,
        local_artifact_id=uuid4(),
        command_id=command.id,
        name="serial.log",
        artifact_type="serial_log",
        content_type="text/plain",
        size_bytes=3,
        sha256="a" * 64,
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    remote_artifacts = SQLiteRemoteArtifactRepository(database)
    stored = await remote_artifacts.create(reported)
    assert stored.organisation_id == SECOND_ORGANISATION_ID
    assert await remote_artifacts.list(organisation_id=SECOND_ORGANISATION_ID) == [stored]
    assert (
        await remote_artifacts.get(
            stored.id,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    database.close()


def test_workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path: Path) -> None:
    asyncio.run(_workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path))


async def _workflow_ci_and_artifact_queries_are_tenant_scoped(tmp_path: Path) -> None:
    database = _database_operational_tenant_persistence(tmp_path / "tenant-platform.db")
    workflow = WorkflowDefinition.model_validate(
        {
            "organisation_id": SECOND_ORGANISATION_ID,
            "name": "tenant-probe",
            "version": 1,
            "requirements": {"capabilities": ["probe"], "labels": {}},
            "steps": [{"action": "probe"}],
        }
    )
    workflows = SQLiteWorkflowRepository(database, initialize_schema=False)
    await workflows.save_definition(workflow)
    assert (
        await workflows.get_definition(
            workflow.name,
            organisation_id=LEGACY_ORGANISATION_ID,
        )
        is None
    )
    assert await workflows.list_definitions(organisation_id=SECOND_ORGANISATION_ID) == [workflow]

    artifact = ArtifactRecord(
        organisation_id=SECOND_ORGANISATION_ID,
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=uuid4(),
        name="result.json",
        artifact_type="test_result",
        content_type="application/json",
        path="objects/result.json",
        size_bytes=2,
        sha256="b" * 64,
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    artifacts = SQLiteGenericArtifactRepository(database)
    await artifacts.save(artifact, idempotency_key="result")
    assert await artifacts.get(artifact.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await artifacts.list_for_owner(
        artifact.owner_type,
        artifact.owner_id,
        organisation_id=SECOND_ORGANISATION_ID,
    ) == [artifact]
    assert await artifacts.list_all(organisation_id=SECOND_ORGANISATION_ID) == [artifact]
    assert await artifacts.list_all(organisation_id=LEGACY_ORGANISATION_ID) == []

    session = CiSession(
        provider=CiProvider.LOCAL,
        external_run_id="second-run",
        requested_by="second-service",
        organisation_id=SECOND_ORGANISATION_ID,
        requested_by_principal_id=uuid4(),
        requested_by_principal_type=PrincipalType.SERVICE_ACCOUNT,
        created_at=NOW_OPERATIONAL_TENANT_PERSISTENCE,
    )
    sessions = SQLiteCiSessionRepository(database)
    await sessions.create(session, idempotency_key="second-session")
    assert await sessions.get(session.id, organisation_id=LEGACY_ORGANISATION_ID) is None
    assert await sessions.list(organisation_id=SECOND_ORGANISATION_ID) == [session]
    database.close()
