from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane_core import IssuedEnrollmentToken
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.core.errors import ArtifactNotFoundError, AuthenticationRequiredError
from lab_platform.models import (
    AgentStatus,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    AuditOutcome,
    AuthenticationContext,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommandType,
    Reservation,
    ReservationSource,
    ReservationStatus,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    Team,
    TeamMembership,
    User,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)

PASSWORD = "correct horse battery staple"


def _runtime(
    tmp_path: Path,
    *,
    auto_login_user: str | None = None,
    hide_unauthorised_resources: bool = True,
) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "distributed": {"queue_commands_for_offline_agents": True},
                "artifacts": {"directory": tmp_path / "artifacts"},
                "authorisation": {
                    "hide_unauthorised_resources": hide_unauthorised_resources,
                },
                "development": {
                    "enabled": True,
                    "auto_login_user": auto_login_user,
                    "allow_insecure_agent_transport": True,
                },
            }
        )
    )


async def _create_user(
    runtime: ControlPlaneRuntime,
    username: str,
    organisation_role: OrganisationRole = OrganisationRole.MEMBER,
) -> User:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    assert organisation is not None
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username=username,
        display_name=username.replace("-", " ").title(),
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_user_with_password(
        user,
        PasswordCredential(
            user_id=user.id,
            password_hash=runtime.identity.hash_password(PASSWORD),
            created_at=now,
            updated_at=now,
        ),
        OrganisationMembership(
            organisation_id=organisation.id,
            user_id=user.id,
            role=organisation_role,
            created_at=now,
        ),
    )
    return user


async def _stage_benches(
    runtime: ControlPlaneRuntime,
) -> tuple[GlobalBenchRecord, GlobalBenchRecord]:
    now = datetime.now(UTC)
    issued = await runtime.enrollment.issue_token(
        name="scoped-agent",
        expires_at=now + timedelta(minutes=5),
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
    )
    capabilities = frozenset({"probe", "reset", "serial", "firmware"})
    benches = tuple(
        GlobalBenchRecord(
            id=f"{enrolled.agent.slug}/{local_id}",
            agent_id=enrolled.agent.id,
            agent_slug=enrolled.agent.slug,
            local_bench_id=local_id,
            name=name,
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.OFFLINE,
            health=HealthStatus.UNHEALTHY,
            capabilities=capabilities,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        for local_id, name in (("bench-a", "Bench A"), ("bench-b", "Bench B"))
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        enrolled.agent,
        benches,
        observed_at=now,
    )
    return benches[0], benches[1]


async def _grant_scoped_roles(
    runtime: ControlPlaneRuntime,
    operator: User,
    viewer: User,
    bench_id: str,
) -> RoleAssignment:
    organisation = await runtime.identity_repository.get_organisation(operator.organisation_id)
    assert organisation is not None
    now = datetime.now(UTC)
    team = Team(
        organisation_id=organisation.id,
        slug="operators",
        name="Operators",
        created_at=now,
        updated_at=now,
    )
    await runtime.identity_repository.create_team(team)
    await runtime.identity_repository.create_team_membership(
        organisation.id,
        TeamMembership(team_id=team.id, user_id=operator.id, created_at=now),
    )
    operator_assignment = RoleAssignment(
        organisation_id=organisation.id,
        subject_type=RoleSubjectType.TEAM,
        subject_id=team.id,
        role=RoleName.OPERATOR,
        resource_type=ResourceType.BENCH,
        resource_id=bench_id,
        created_by=operator.id,
        created_at=now,
    )
    await runtime.identity_repository.create_role_assignment(operator_assignment)
    await runtime.identity_repository.create_role_assignment(
        RoleAssignment(
            organisation_id=organisation.id,
            subject_type=RoleSubjectType.USER,
            subject_id=viewer.id,
            role=RoleName.VIEWER,
            resource_type=ResourceType.BENCH,
            resource_id=bench_id,
            created_by=operator.id,
            created_at=now,
        )
    )
    return operator_assignment


async def _grant_direct_role(
    runtime: ControlPlaneRuntime,
    user: User,
    role: RoleName,
    resource_type: ResourceType,
    resource_id: str,
) -> RoleAssignment:
    assignment = RoleAssignment(
        organisation_id=user.organisation_id,
        subject_type=RoleSubjectType.USER,
        subject_id=user.id,
        role=role,
        resource_type=resource_type,
        resource_id=resource_id,
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    return await runtime.identity_repository.create_role_assignment(assignment)


async def _store_platform_artifact(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    owner_type: ArtifactOwnerType,
    owner_id: UUID,
    name: str,
    content: bytes,
) -> ArtifactRecord:
    return await runtime.platform_artifacts.store_bytes(
        content,
        owner_type=owner_type,
        owner_id=owner_id,
        name=name,
        artifact_type="test-results",
        content_type="application/octet-stream",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        organisation_id=organisation_id,
    )


async def _store_remote_artifact(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    agent_id: UUID,
    command_id: UUID,
    operation_id: UUID,
    name: str,
) -> RemoteArtifactMetadata:
    artifact = RemoteArtifactMetadata(
        organisation_id=organisation_id,
        agent_id=agent_id,
        local_artifact_id=uuid4(),
        command_id=command_id,
        operation_id=operation_id,
        name=name,
        artifact_type="serial-log",
        content_type="text/plain",
        size_bytes=3,
        sha256=hashlib.sha256(b"log").hexdigest(),
        created_at=datetime.now(UTC),
    )
    return await runtime.remote_artifacts.create(artifact)


async def _seed_remote_workflow_artifact(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    agent_id: UUID,
    bench_id: str,
    reservation: CoordinatedReservationLease,
    workflow_name: str,
    key: str,
) -> tuple[RemoteArtifactMetadata, UUID, UUID]:
    definition = await runtime.workflow_repository.get_definition(
        workflow_name,
        1,
        organisation_id=organisation_id,
    )
    assert definition is not None
    command, operation = await runtime.commands.create(
        agent_id=agent_id,
        bench_id=bench_id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={
            "definition": definition.model_dump(mode="json"),
            "inputs": {},
            "owner": "artifact-parent-test",
            "artifact_transfers": [],
            "reservation_lease": reservation.lease.model_dump(mode="json"),
        },
        expires_at=min(
            datetime.now(UTC) + timedelta(minutes=5),
            reservation.lease.valid_until,
        ),
        idempotency_key=key,
        reservation_lease=reservation.lease,
        operation_type=RemoteCommandType.RUN_WORKFLOW.value,
        dispatch=False,
        allow_internal_authorisation=True,
    )
    assert operation is not None
    artifact = await _store_remote_artifact(
        runtime,
        organisation_id,
        agent_id,
        command.id,
        operation.id,
        f"{key}.log",
    )
    return artifact, command.id, operation.id


async def _seed_reservation(
    runtime: ControlPlaneRuntime,
    user: User,
    agent_id: UUID,
    bench_id: str,
    key: str,
) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    reservation = Reservation(
        id=uuid4(),
        organisation_id=user.organisation_id,
        bench_id=bench_id,
        owner=user.display_name,
        owner_principal_id=user.id,
        owner_principal_type=PrincipalType.USER.value,
        created_at=now,
        requested_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        status=ReservationStatus.SCHEDULED,
        source=ReservationSource.API,
        idempotency_key=key,
    )
    result = await runtime.reservation_repository.grant_if_eligible(
        ReservationGrantRequest(
            reservation=reservation,
            agent_id=agent_id,
            lease_valid_until=now + timedelta(minutes=30),
        ),
        mutation_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        expected_agent_status=AgentStatus.OFFLINE,
        expected_bench_status=GlobalBenchStatus.OFFLINE,
    )
    assert result is not None
    pending = result.record
    active = CoordinatedReservationLease(
        reservation=pending.reservation.model_copy(
            update={"status": ReservationStatus.ACTIVE, "activated_at": now}
        ),
        lease=pending.lease,
        state=ReservationLeaseState.ACTIVE,
        revision=pending.revision + 1,
    )
    persisted = await runtime.reservation_repository.replace_if_current(
        active,
        expected_revision=pending.revision,
        mutation_key=f"{key}:activate",
        request_fingerprint=hashlib.sha256(f"{key}:activate".encode()).hexdigest(),
    )
    assert persisted is not None
    return active


async def _seed_workflow_run(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    bench_id: str,
    reservation_id: UUID,
) -> WorkflowRun:
    definition = WorkflowDefinition.model_validate(
        {
            "organisation_id": organisation_id,
            "name": "artifact-parent-workflow",
            "version": 1,
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        }
    )
    await runtime.workflow_repository.save_definition(definition)
    now = datetime.now(UTC)
    run = WorkflowRun(
        workflow_name=definition.name,
        workflow_version=definition.version,
        bench_id=bench_id,
        owner="artifact-parent-test",
        reservation_id=reservation_id,
        status=WorkflowRunStatus.SUCCEEDED,
        created_at=now,
        started_at=now,
        completed_at=now,
    )
    return await runtime.workflow_repository.create_run(run)


async def _attach_ci_workflow(
    runtime: ControlPlaneRuntime,
    session_id: UUID,
    command_id: UUID,
    operation_id: UUID,
    agent_id: UUID,
    bench_id: str,
    reservation_id: UUID,
    workflow_name: str,
) -> None:
    key = f"artifact-ci:{session_id}"
    attached = await runtime.ci_repository.attach_distributed_workflow(
        session_id,
        command_id,
        operation_id=operation_id,
        agent_id=agent_id,
        bench_id=bench_id,
        reservation_id=reservation_id,
        workflow_name=workflow_name,
        workflow_version=1,
        idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        started_at=datetime.now(UTC),
    )
    assert attached is not None


async def _issue_legacy_token(
    runtime: ControlPlaneRuntime,
    *scopes: ApiTokenScope,
) -> str:
    issued = await runtime.token_service.issue(
        name="phase-5 compatibility",
        owner="legacy-test",
        scopes=scopes,
    )
    return issued.plaintext


async def _issue_enrollment_token(runtime: ControlPlaneRuntime) -> IssuedEnrollmentToken:
    return await runtime.enrollment.issue_token(
        name="audited-public-enrollment",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        allow_internal_authorisation=True,
    )


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _exercise_direct_artifact_boundary_denials(
    runtime: ControlPlaneRuntime,
    context: AuthenticationContext,
    existing: ArtifactRecord,
) -> None:
    stream_consumed = False

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal stream_consumed
        stream_consumed = True
        yield b"must-not-be-consumed"

    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.upload(
            chunks(),
            owner_type=ArtifactOwnerType.WORKFLOW_STEP,
            owner_id=uuid4(),
            name="anonymous.bin",
            artifact_type="test-results",
        )
    assert not stream_consumed
    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.list()
    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.get(existing.id)
    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.content(existing.id)
    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.delete(existing.id)
    with pytest.raises(AuthenticationRequiredError):
        await runtime.artifact_access.issue_download(existing.id, agent_id=uuid4())
    with pytest.raises(ValueError, match="mutually exclusive"):
        await runtime.artifact_access.get(
            existing.id,
            allow_legacy_authorisation=True,
            allow_internal_authorisation=True,
        )

    with pytest.raises(ArtifactNotFoundError):
        await runtime.artifact_access.upload(
            chunks(),
            owner_type=ArtifactOwnerType.WORKFLOW_STEP,
            owner_id=uuid4(),
            name="denied.bin",
            artifact_type="test-results",
            authentication_context=context,
        )
    assert not stream_consumed
    with pytest.raises(ArtifactNotFoundError):
        await runtime.artifact_access.upload_for_operation_route(
            chunks(),
            operation_id=uuid4(),
            agent_id=uuid4(),
            bench_id="missing-agent/missing-bench",
            name="denied-firmware.bin",
            artifact_type="firmware",
            authentication_context=context,
        )
    assert not stream_consumed

    issue_download = AsyncMock()
    with (
        patch.object(runtime.artifacts, "issue_download", new=issue_download),
        pytest.raises(ArtifactNotFoundError),
    ):
        await runtime.artifact_access.issue_download(
            existing.id,
            agent_id=uuid4(),
            authentication_context=context,
        )
    issue_download.assert_not_awaited()

    path = Path(existing.path)
    with pytest.raises(ArtifactNotFoundError):
        await runtime.artifact_access.delete(
            existing.id,
            authentication_context=context,
        )
    assert path.is_file()
    assert (
        await runtime.platform_artifacts.get(
            existing.id,
            organisation_id=context.principal.organisation_id,
        )
        == existing
    )


def test_direct_artifact_boundary_denies_before_stream_or_storage_side_effects(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        owner = client.portal.call(
            _create_user,
            runtime,
            "artifact-boundary-owner",
            OrganisationRole.OWNER,
        )
        headers = _login(client, owner.username)
        access_token = headers["Authorization"].removeprefix("Bearer ")
        context = client.portal.call(runtime.identity.authenticate_session, access_token)
        existing = client.portal.call(
            _store_platform_artifact,
            runtime,
            owner.organisation_id,
            ArtifactOwnerType.WORKFLOW_STEP,
            uuid4(),
            "untrusted-parent.bin",
            b"existing-content",
        )

        client.portal.call(
            _exercise_direct_artifact_boundary_denials,
            runtime,
            context,
            existing,
        )
        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            owner.organisation_id,
        )
        denials = [
            event
            for event in audit_events
            if event.action == "PERMISSION_DENIED"
            and event.resource_type == "ARTIFACT"
            and event.outcome is AuditOutcome.DENIED
        ]
        assert len(denials) >= 3


def test_team_bench_operator_is_scoped_and_viewer_cannot_operate(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        operator = client.portal.call(_create_user, runtime, "bench-operator")
        viewer = client.portal.call(_create_user, runtime, "bench-viewer")
        bench, other_bench = client.portal.call(_stage_benches, runtime)
        assignment = client.portal.call(
            _grant_scoped_roles,
            runtime,
            operator,
            viewer,
            bench.id,
        )

        operator_headers = _login(client, operator.username)
        viewer_headers = _login(client, viewer.username)

        operator_collection = client.get("/api/v1/benches", headers=operator_headers)
        assert operator_collection.status_code == 200, operator_collection.text
        assert [item["id"] for item in operator_collection.json()["items"]] == [bench.id]
        viewer_collection = client.get("/api/v1/benches", headers=viewer_headers)
        assert viewer_collection.status_code == 200, viewer_collection.text
        assert [item["id"] for item in viewer_collection.json()["items"]] == [bench.id]

        visible = client.get(f"/api/v1/benches/{bench.id}", headers=operator_headers)
        assert visible.status_code == 200, visible.text
        accepted = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers=operator_headers,
            json={},
        )
        assert accepted.status_code == 202, accepted.text

        command_id = UUID(accepted.json()["command_id"])
        command = client.portal.call(runtime.command_records.get, command_id)
        assert command is not None
        assert command.actor_context is not None
        assert command.actor_context.principal_id == operator.id
        assert command.authorisation_snapshot_id is not None
        assert command.actor_context.authorisation_snapshot_id == command.authorisation_snapshot_id
        snapshot = client.portal.call(
            runtime.identity_repository.get_authorisation_snapshot,
            operator.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.permission == "benches:operate"
        assert snapshot.resource_type is ResourceType.BENCH
        assert snapshot.resource_id == bench.id
        assert snapshot.granted_by_assignments == [assignment.id]

        operation_collection = client.get("/api/v1/operations", headers=operator_headers)
        assert operation_collection.status_code == 200, operation_collection.text
        assert [item["id"] for item in operation_collection.json()["items"]] == [
            accepted.json()["operation_id"]
        ]

        viewer_denied = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers=viewer_headers,
            json={},
        )
        assert viewer_denied.status_code == 404
        assert viewer_denied.json()["error"]["code"] == "BENCH_NOT_FOUND"

        other_action = client.post(
            f"/api/v1/benches/{other_bench.id}/actions/probe",
            headers=operator_headers,
            json={},
        )
        assert other_action.status_code == 404
        other_read = client.get(
            f"/api/v1/benches/{other_bench.id}",
            headers=operator_headers,
        )
        assert other_read.status_code == 404

        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            operator.organisation_id,
        )
        successful = next(
            event for event in audit_events if event.action == "BENCH_PROBE_REQUESTED"
        )
        assert successful.actor_id == operator.id
        assert successful.resource_id == bench.id
        assert successful.outcome is AuditOutcome.SUCCEEDED
        assert successful.metadata["command_id"] == str(command_id)
        assert any(
            event.action == "PERMISSION_DENIED"
            and event.actor_id in {operator.id, viewer.id}
            and event.outcome is AuditOutcome.DENIED
            for event in audit_events
        )


def test_safe_development_auto_login_uses_normal_authorisation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime(tmp_path, auto_login_user="local-owner")
    with (
        caplog.at_level(logging.WARNING, logger="lab-platform.control-plane"),
        TestClient(create_app(runtime)) as client,
    ):
        assert client.portal is not None
        client.portal.call(
            _create_user,
            runtime,
            "local-owner",
            OrganisationRole.OWNER,
        )
        response = client.get("/api/v1/agents")
        assert response.status_code == 200, response.text
    assert "DEVELOPMENT AUTO-LOGIN IS ENABLED" in caplog.text


def test_permission_details_can_be_exposed_when_resource_hiding_is_disabled(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, hide_unauthorised_resources=False)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        operator = client.portal.call(_create_user, runtime, "policy-owner")
        viewer = client.portal.call(_create_user, runtime, "policy-viewer")
        bench, _other = client.portal.call(_stage_benches, runtime)
        client.portal.call(_grant_scoped_roles, runtime, operator, viewer, bench.id)
        denied = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers=_login(client, viewer.username),
            json={},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["required_permission"] == "benches:operate"


def test_development_auto_login_never_falls_back_to_anonymous(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, auto_login_user="missing-user")
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/v1/agents")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_missing_development_user_does_not_block_loopback_legacy_bootstrap(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, auto_login_user="not-created-yet")
    with TestClient(create_app(runtime)) as client:
        bootstrapped = client.post(
            "/api/v1/tokens",
            json={
                "name": "initial-loopback-token",
                "owner": "bootstrap",
                "scopes": [scope.value for scope in ApiTokenScope],
            },
        )
        assert bootstrapped.status_code == 201, bootstrapped.text
        assert bootstrapped.json()["token"]
        assert client.get("/api/v1/agents").status_code == 401


def test_artifacts_inherit_trusted_parents_with_same_org_disjoint_assignments(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        alpha = client.portal.call(_create_user, runtime, "artifact-alpha")
        beta = client.portal.call(_create_user, runtime, "artifact-beta")
        bench_alpha, bench_beta = client.portal.call(_stage_benches, runtime)
        client.portal.call(
            _grant_direct_role,
            runtime,
            alpha,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            bench_alpha.id,
        )
        client.portal.call(
            _grant_direct_role,
            runtime,
            beta,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            bench_beta.id,
        )
        alpha_headers = _login(client, alpha.username)
        beta_headers = _login(client, beta.username)

        alpha_probe = client.post(
            f"/api/v1/benches/{bench_alpha.id}/actions/probe",
            headers=alpha_headers,
            json={},
        )
        beta_probe = client.post(
            f"/api/v1/benches/{bench_beta.id}/actions/probe",
            headers=beta_headers,
            json={},
        )
        assert alpha_probe.status_code == 202, alpha_probe.text
        assert beta_probe.status_code == 202, beta_probe.text
        alpha_command_id = UUID(alpha_probe.json()["command_id"])
        alpha_operation_id = UUID(alpha_probe.json()["operation_id"])
        beta_command_id = UUID(beta_probe.json()["command_id"])
        beta_operation_id = UUID(beta_probe.json()["operation_id"])

        alpha_artifact = client.portal.call(
            _store_platform_artifact,
            runtime,
            alpha.organisation_id,
            ArtifactOwnerType.OPERATION,
            alpha_operation_id,
            "alpha.bin",
            b"alpha-operation-artifact",
        )
        beta_artifact = client.portal.call(
            _store_platform_artifact,
            runtime,
            beta.organisation_id,
            ArtifactOwnerType.OPERATION,
            beta_operation_id,
            "beta.bin",
            b"beta-operation-artifact",
        )
        alpha_remote = client.portal.call(
            _store_remote_artifact,
            runtime,
            alpha.organisation_id,
            bench_alpha.agent_id,
            alpha_command_id,
            alpha_operation_id,
            "alpha.log",
        )
        beta_remote = client.portal.call(
            _store_remote_artifact,
            runtime,
            beta.organisation_id,
            bench_beta.agent_id,
            beta_command_id,
            beta_operation_id,
            "beta.log",
        )

        alpha_remote_list = client.get(
            "/api/v1/artifacts",
            headers=alpha_headers,
            params={"agent_id": str(bench_alpha.agent_id)},
        )
        beta_remote_list = client.get(
            "/api/v1/artifacts",
            headers=beta_headers,
            params={"agent_id": str(bench_beta.agent_id)},
        )
        assert alpha_remote_list.status_code == 200, alpha_remote_list.text
        assert beta_remote_list.status_code == 200, beta_remote_list.text
        assert {UUID(item["id"]) for item in alpha_remote_list.json()["items"]} == {alpha_remote.id}
        assert {UUID(item["id"]) for item in beta_remote_list.json()["items"]} == {beta_remote.id}

        denied_owner_list = client.get(
            "/api/v1/artifacts",
            headers=alpha_headers,
            params={
                "owner_type": ArtifactOwnerType.OPERATION.value,
                "owner_id": str(beta_operation_id),
            },
        )
        assert denied_owner_list.status_code == 200, denied_owner_list.text
        assert beta_artifact.id not in {
            UUID(item["id"]) for item in denied_owner_list.json()["items"]
        }

        for artifact_id in (alpha_artifact.id, alpha_remote.id):
            visible = client.get(f"/api/v1/artifacts/{artifact_id}", headers=alpha_headers)
            hidden = client.get(f"/api/v1/artifacts/{artifact_id}", headers=beta_headers)
            assert visible.status_code == 200, visible.text
            assert hidden.status_code == 404, hidden.text
        for artifact_id in (beta_artifact.id, beta_remote.id):
            assert (
                client.get(f"/api/v1/artifacts/{artifact_id}", headers=alpha_headers).status_code
                == 404
            )

        content = client.get(
            f"/api/v1/artifacts/{alpha_artifact.id}/content",
            headers=alpha_headers,
        )
        assert content.status_code == 200, content.text
        assert content.content == b"alpha-operation-artifact"
        assert (
            client.get(
                f"/api/v1/artifacts/{beta_artifact.id}/content",
                headers=alpha_headers,
            ).status_code
            == 404
        )

        upload_content = b"identity-authorised-upload"
        upload = client.post(
            "/api/v1/artifacts",
            headers=alpha_headers,
            data={
                "owner_type": ArtifactOwnerType.OPERATION.value,
                "owner_id": str(alpha_operation_id),
                "artifact_type": "test-results",
                "expected_sha256": hashlib.sha256(upload_content).hexdigest(),
            },
            files={"file": ("upload.bin", upload_content, "application/octet-stream")},
        )
        assert upload.status_code == 201, upload.text
        denied_upload = client.post(
            "/api/v1/artifacts",
            headers=alpha_headers,
            data={
                "owner_type": ArtifactOwnerType.OPERATION.value,
                "owner_id": str(beta_operation_id),
                "artifact_type": "test-results",
            },
            files={"file": ("denied.bin", b"denied", "application/octet-stream")},
        )
        assert denied_upload.status_code == 404, denied_upload.text

        transfer = client.post(
            f"/api/v1/artifacts/{upload.json()['id']}/transfers",
            headers=alpha_headers,
            json={"agent_id": str(bench_alpha.agent_id)},
        )
        denied_transfer = client.post(
            f"/api/v1/artifacts/{beta_artifact.id}/transfers",
            headers=alpha_headers,
            json={"agent_id": str(bench_alpha.agent_id)},
        )
        assert transfer.status_code == 201, transfer.text
        assert denied_transfer.status_code == 404, denied_transfer.text

        reservation = client.portal.call(
            _seed_reservation,
            runtime,
            alpha,
            bench_alpha.agent_id,
            bench_alpha.id,
            "artifact-parent-reservation",
        )
        flash_content = b"identity-authorised-firmware"
        flash = client.post(
            f"/api/v1/benches/{bench_alpha.id}/actions/flash",
            headers={**alpha_headers, "Idempotency-Key": "artifact-parent-flash"},
            files={
                "firmware": (
                    "firmware.bin",
                    flash_content,
                    "application/octet-stream",
                )
            },
        )
        assert flash.status_code == 202, flash.text
        flash_payload = flash.json()
        flash_artifact = flash_payload["input_artifact"]
        flash_artifact_id = UUID(flash_artifact["id"])
        assert flash_artifact["owner_type"] == ArtifactOwnerType.OPERATION.value
        assert flash_artifact["owner_id"] == flash_payload["operation_id"]
        assert (
            client.get(f"/api/v1/artifacts/{flash_artifact_id}", headers=alpha_headers).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/artifacts/{flash_artifact_id}", headers=beta_headers).status_code
            == 404
        )
        flash_download = client.get(
            f"/api/v1/artifacts/{flash_artifact_id}/content",
            headers=alpha_headers,
        )
        assert flash_download.status_code == 200, flash_download.text
        assert flash_download.content == flash_content
        assert (
            client.delete(
                f"/api/v1/artifacts/{flash_artifact_id}", headers=beta_headers
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/v1/artifacts/{flash_artifact_id}", headers=alpha_headers
            ).status_code
            == 204
        )
        workflow_run = client.portal.call(
            _seed_workflow_run,
            runtime,
            alpha.organisation_id,
            bench_alpha.id,
            reservation.reservation.id,
        )
        workflow_artifact = client.portal.call(
            _store_platform_artifact,
            runtime,
            alpha.organisation_id,
            ArtifactOwnerType.WORKFLOW_RUN,
            workflow_run.id,
            "workflow-results.json",
            b"workflow-results",
        )
        # Workflow-run artifacts require access to both the workflow definition
        # and the actual bench, not merely one of those resources.
        assert (
            client.get(
                f"/api/v1/artifacts/{workflow_artifact.id}", headers=alpha_headers
            ).status_code
            == 404
        )
        workflow_assignment = client.portal.call(
            _grant_direct_role,
            runtime,
            alpha,
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            workflow_run.workflow_name,
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{workflow_artifact.id}", headers=alpha_headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{workflow_artifact.id}", headers=beta_headers
            ).status_code
            == 404
        )
        assert client.portal.call(
            runtime.identity_repository.delete_role_assignment,
            alpha.organisation_id,
            workflow_assignment.id,
        )

        route_only = client.portal.call(_create_user, runtime, "artifact-route-only")
        client.portal.call(
            _grant_direct_role,
            runtime,
            route_only,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            bench_alpha.id,
        )
        client.portal.call(
            _grant_direct_role,
            runtime,
            route_only,
            RoleName.WORKFLOW_RUNNER,
            ResourceType.WORKFLOW,
            workflow_run.workflow_name,
        )
        route_only_headers = _login(client, route_only.username)
        workflow_remote, _workflow_command_id, _workflow_operation_id = client.portal.call(
            _seed_remote_workflow_artifact,
            runtime,
            alpha.organisation_id,
            bench_alpha.agent_id,
            bench_alpha.id,
            reservation,
            workflow_run.workflow_name,
            "artifact-remote-workflow",
        )
        assert (
            client.get(f"/api/v1/artifacts/{workflow_remote.id}", headers=alpha_headers).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{workflow_remote.id}", headers=route_only_headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/artifacts/{workflow_remote.id}", headers=beta_headers).status_code
            == 404
        )

        ci_response = client.post(
            "/api/v1/ci/sessions",
            headers={**alpha_headers, "Idempotency-Key": "artifact-linked-ci"},
            json={"provider": "local", "external_run_id": "artifact-linked-ci"},
        )
        assert ci_response.status_code == 201, ci_response.text
        ci_session_id = UUID(ci_response.json()["id"])
        ci_remote, ci_command_id, ci_operation_id = client.portal.call(
            _seed_remote_workflow_artifact,
            runtime,
            alpha.organisation_id,
            bench_alpha.agent_id,
            bench_alpha.id,
            reservation,
            workflow_run.workflow_name,
            "artifact-remote-ci-workflow",
        )
        client.portal.call(
            _attach_ci_workflow,
            runtime,
            ci_session_id,
            ci_command_id,
            ci_operation_id,
            bench_alpha.agent_id,
            bench_alpha.id,
            reservation.reservation.id,
            workflow_run.workflow_name,
        )
        ci_artifact = client.portal.call(
            _store_platform_artifact,
            runtime,
            alpha.organisation_id,
            ArtifactOwnerType.CI_SESSION,
            ci_session_id,
            "linked-ci.json",
            b"linked-ci",
        )

        # Session ownership alone is insufficient once the session is linked:
        # the trusted workflow and actual bench both participate in the decision.
        assert (
            client.get(f"/api/v1/artifacts/{ci_artifact.id}", headers=alpha_headers).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/artifacts/{ci_remote.id}", headers=alpha_headers).status_code
            == 404
        )
        # This principal has both workflow and bench access, but not access to
        # another user's CI session, so linked CI artifacts remain hidden.
        for artifact_id in (ci_artifact.id, ci_remote.id):
            assert (
                client.get(
                    f"/api/v1/artifacts/{artifact_id}", headers=route_only_headers
                ).status_code
                == 404
            )
        for user in (alpha, beta):
            client.portal.call(
                _grant_direct_role,
                runtime,
                user,
                RoleName.WORKFLOW_RUNNER,
                ResourceType.WORKFLOW,
                workflow_run.workflow_name,
            )
        assert (
            client.get(f"/api/v1/artifacts/{ci_artifact.id}", headers=alpha_headers).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/artifacts/{ci_remote.id}", headers=alpha_headers).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/artifacts/{ci_artifact.id}", headers=beta_headers).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/artifacts/{ci_remote.id}", headers=beta_headers).status_code == 404
        )

        unlinked_response = client.post(
            "/api/v1/ci/sessions",
            headers={**alpha_headers, "Idempotency-Key": "artifact-unlinked-ci"},
            json={"provider": "local", "external_run_id": "artifact-unlinked-ci"},
        )
        assert unlinked_response.status_code == 201, unlinked_response.text
        unlinked_session_id = UUID(unlinked_response.json()["id"])
        unlinked_artifact = client.portal.call(
            _store_platform_artifact,
            runtime,
            alpha.organisation_id,
            ArtifactOwnerType.CI_SESSION,
            unlinked_session_id,
            "unlinked-ci.json",
            b"unlinked-ci",
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{unlinked_artifact.id}", headers=alpha_headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{unlinked_artifact.id}", headers=beta_headers
            ).status_code
            == 404
        )

        unsupported = client.portal.call(
            _store_platform_artifact,
            runtime,
            alpha.organisation_id,
            ArtifactOwnerType.WORKFLOW_STEP,
            uuid4(),
            "legacy-step.log",
            b"legacy-step",
        )
        assert (
            client.get(f"/api/v1/artifacts/{unsupported.id}", headers=alpha_headers).status_code
            == 404
        )
        legacy_token = client.portal.call(
            _issue_legacy_token,
            runtime,
            ApiTokenScope.ARTIFACTS_READ,
        )
        legacy_response = client.get(
            f"/api/v1/artifacts/{unsupported.id}",
            headers={"Authorization": f"Bearer {legacy_token}"},
        )
        assert legacy_response.status_code == 200, legacy_response.text

        original_path = Path(alpha_artifact.path)
        assert original_path.is_file()
        assert (
            client.delete(
                f"/api/v1/artifacts/{beta_artifact.id}", headers=alpha_headers
            ).status_code
            == 404
        )
        deleted = client.delete(f"/api/v1/artifacts/{alpha_artifact.id}", headers=alpha_headers)
        assert deleted.status_code == 204, deleted.text
        assert not original_path.exists()
        assert (
            client.get(f"/api/v1/artifacts/{alpha_artifact.id}", headers=alpha_headers).status_code
            == 404
        )
        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            alpha.organisation_id,
        )
        deletion = next(
            event
            for event in audit_events
            if event.action == "ARTIFACT_DELETED" and event.resource_id == str(alpha_artifact.id)
        )
        assert deletion.actor_id == alpha.id
        assert deletion.outcome is AuditOutcome.SUCCEEDED


def test_reservation_auditor_reads_but_only_lab_admin_revokes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        owner = client.portal.call(_create_user, runtime, "reservation-owner")
        auditor = client.portal.call(_create_user, runtime, "reservation-auditor")
        administrator = client.portal.call(_create_user, runtime, "reservation-admin")
        bench, other_bench = client.portal.call(_stage_benches, runtime)
        client.portal.call(
            _grant_direct_role,
            runtime,
            auditor,
            RoleName.AUDITOR,
            ResourceType.BENCH,
            bench.id,
        )
        client.portal.call(
            _grant_direct_role,
            runtime,
            administrator,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            bench.id,
        )
        reservation = client.portal.call(
            _seed_reservation,
            runtime,
            owner,
            bench.agent_id,
            bench.id,
            "auditor-readable-reservation",
        )
        auditor_headers = _login(client, auditor.username)
        administrator_headers = _login(client, administrator.username)
        reservation_id = reservation.reservation.id
        lease_version = reservation.lease.lease_version

        readable = client.get(f"/api/v1/reservations/{reservation_id}", headers=auditor_headers)
        assert readable.status_code == 200, readable.text
        listed = client.get("/api/v1/reservations", headers=auditor_headers)
        assert listed.status_code == 200, listed.text
        assert {UUID(item["reservation"]["id"]) for item in listed.json()["items"]} == {
            reservation_id
        }

        for action in ("renew", "release", "revoke"):
            payload = {
                "expected_lease_version": lease_version,
                "idempotency_key": f"auditor-denied-{action}",
            }
            denied = client.post(
                f"/api/v1/reservations/{reservation_id}/{action}",
                headers=auditor_headers,
                json=payload,
            )
            assert denied.status_code == 404, denied.text

        revoked = client.post(
            f"/api/v1/reservations/{reservation_id}/revoke",
            headers=administrator_headers,
            json={
                "expected_lease_version": lease_version,
                "idempotency_key": "administrator-revoke",
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["state"] == "REVOKED"
        audit_events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            administrator.organisation_id,
        )
        event = next(event for event in audit_events if event.action == "BENCH_RESERVATION_REVOKED")
        assert event.actor_id == administrator.id
        assert event.resource_id == bench.id
        assert event.metadata["reservation_id"] == str(reservation_id)

        legacy_reservation = client.portal.call(
            _seed_reservation,
            runtime,
            owner,
            other_bench.agent_id,
            other_bench.id,
            "legacy-revocable-reservation",
        )
        legacy_token = client.portal.call(
            _issue_legacy_token,
            runtime,
            ApiTokenScope.RESERVATIONS_WRITE,
        )
        legacy_revoke = client.post(
            f"/api/v1/reservations/{legacy_reservation.reservation.id}/revoke",
            headers={"Authorization": f"Bearer {legacy_token}"},
            json={
                "expected_lease_version": legacy_reservation.lease.lease_version,
                "idempotency_key": "legacy-revoke",
            },
        )
        assert legacy_revoke.status_code == 200, legacy_revoke.text
        assert legacy_revoke.json()["state"] == "REVOKED"


def test_reservation_and_bench_action_owner_denials_are_audited(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        reservation_owner = client.portal.call(
            _create_user,
            runtime,
            "durable-reservation-owner",
            OrganisationRole.OWNER,
        )
        non_owner = client.portal.call(
            _create_user,
            runtime,
            "reservation-non-owner",
            OrganisationRole.OWNER,
        )
        bench, _other_bench = client.portal.call(_stage_benches, runtime)
        reservation = client.portal.call(
            _seed_reservation,
            runtime,
            reservation_owner,
            bench.agent_id,
            bench.id,
            "owner-denial-audit-reservation",
        )
        headers = _login(client, non_owner.username)

        for action in ("renew", "release"):
            denied = client.post(
                f"/api/v1/reservations/{reservation.reservation.id}/{action}",
                headers=headers,
                json={
                    "expected_lease_version": reservation.lease.lease_version,
                    "idempotency_key": f"owner-denial-{action}",
                },
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "RESERVATION_OWNER_MISMATCH"

        denied_reset = client.post(
            f"/api/v1/benches/{bench.id}/actions/reset",
            headers=headers,
            json={},
        )
        assert denied_reset.status_code == 403, denied_reset.text
        assert denied_reset.json()["error"]["code"] == "RESERVATION_OWNER_MISMATCH"

        events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            non_owner.organisation_id,
        )
        denials = [
            event
            for event in events
            if event.action == "PERMISSION_DENIED"
            and event.actor_id == non_owner.id
            and event.resource_type == ResourceType.BENCH.value
            and event.resource_id == bench.id
        ]
        assert sorted(event.metadata["required_permission"] for event in denials) == [
            "benches:reserve",
            "benches:reserve",
            "benches:reset",
        ]
        assert all("another principal" in (event.reason or "") for event in denials)


def test_operation_owner_denial_branches_are_audited(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        operation_owner = client.portal.call(
            _create_user,
            runtime,
            "durable-operation-owner",
            OrganisationRole.OWNER,
        )
        non_owner = client.portal.call(
            _create_user,
            runtime,
            "operation-non-owner",
            OrganisationRole.OWNER,
        )
        bench, _other_bench = client.portal.call(_stage_benches, runtime)
        owner_headers = _login(client, operation_owner.username)
        non_owner_headers = _login(client, non_owner.username)

        identity_operation = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers=owner_headers,
            json={},
        )
        assert identity_operation.status_code == 202, identity_operation.text
        identity_denial = client.post(
            f"/api/v1/operations/{identity_operation.json()['operation_id']}/cancel",
            headers=non_owner_headers,
            json={},
        )
        assert identity_denial.status_code == 403, identity_denial.text

        legacy_token = client.portal.call(
            _issue_legacy_token,
            runtime,
            ApiTokenScope.WORKFLOWS_RUN,
        )
        legacy_operation = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers={"Authorization": f"Bearer {legacy_token}"},
            json={"owner": "legacy-operation-owner"},
        )
        assert legacy_operation.status_code == 202, legacy_operation.text
        legacy_denial = client.post(
            f"/api/v1/operations/{legacy_operation.json()['operation_id']}/cancel",
            headers=non_owner_headers,
            json={},
        )
        assert legacy_denial.status_code == 403, legacy_denial.text

        events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            non_owner.organisation_id,
        )
        denials = [
            event
            for event in events
            if event.action == "PERMISSION_DENIED"
            and event.actor_id == non_owner.id
            and event.resource_type == ResourceType.BENCH.value
            and event.resource_id == bench.id
            and event.metadata == {"required_permission": "operations:cancel"}
        ]
        assert len(denials) == 2
        assert {event.reason for event in denials} == {
            "The operation belongs to another authenticated principal.",
            "The legacy operation owner does not match the cancellation request.",
        }


def test_public_enrollment_emits_secret_safe_phase6_audit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        issued = client.portal.call(
            _issue_enrollment_token,
            runtime,
        )
        enrollment_token = issued.plaintext.get_secret_value()
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": enrollment_token,
                "request_id": str(uuid4()),
                "agent_version": "0.7.0-alpha",
                "protocol_version": "1.0",
                "location": "audit-lab",
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        credential = enrolled.json()["credential"]
        organisation_id = UUID(enrolled.json()["agent"]["organisation_id"])
        events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            organisation_id,
        )
        event = next(item for item in events if item.action == "AGENT_ENROLLED")
        assert event.actor_type is None
        assert event.actor_id is None
        assert event.actor_display_name is None
        assert event.resource_type == "AGENT"
        assert event.resource_id == enrolled.json()["agent"]["id"]
        assert event.outcome is AuditOutcome.SUCCEEDED
        assert event.metadata == {
            "agent_slug": enrolled.json()["agent"]["slug"],
            "agent_version": "0.7.0-alpha",
            "protocol_version": "1.0",
        }
        serialized = event.model_dump_json()
        assert enrollment_token not in serialized
        assert credential not in serialized
        assert "credential" not in serialized.casefold()
        assert "token" not in serialized.casefold()


def test_remaining_protected_mutations_emit_bounded_phase6_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        owner = client.portal.call(
            _create_user,
            runtime,
            "audit-owner",
            OrganisationRole.OWNER,
        )
        bench, _other_bench = client.portal.call(_stage_benches, runtime)
        headers = _login(client, owner.username)

        enrollment = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "bounded-audit-token", "expires_in_seconds": 300},
        )
        assert enrollment.status_code == 201, enrollment.text
        enrollment_secret = enrollment.json()["token"]
        enrollment_token_id = UUID(enrollment.json()["id"])

        workflow = client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "bounded-audit-workflow",
                "version": 1,
                "requirements": {"capabilities": ["probe"]},
                "steps": [{"action": "probe"}],
            },
        )
        assert workflow.status_code == 201, workflow.text

        probe = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers=headers,
            json={},
        )
        assert probe.status_code == 202, probe.text
        operation_id = UUID(probe.json()["operation_id"])

        reservation = client.portal.call(
            _seed_reservation,
            runtime,
            owner,
            bench.agent_id,
            bench.id,
            "bounded-audit-reservation",
        )
        now = datetime.now(UTC)
        active_reservation = CoordinatedReservationLease(
            reservation=reservation.reservation.model_copy(
                update={"status": ReservationStatus.ACTIVE, "activated_at": now}
            ),
            lease=reservation.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=reservation.revision + 1,
        )
        monkeypatch.setattr(
            runtime.reservations,
            "renew",
            AsyncMock(return_value=active_reservation),
        )
        renewed = client.post(
            f"/api/v1/reservations/{reservation.reservation.id}/renew",
            headers=headers,
            json={
                "expected_lease_version": reservation.lease.lease_version,
                "idempotency_key": "bounded-audit-renew",
            },
        )
        assert renewed.status_code == 200, renewed.text

        refresh_request_id = uuid4()
        reconciliation_request_id = uuid4()
        monkeypatch.setattr(runtime.hub, "is_connected", AsyncMock(return_value=True))
        monkeypatch.setattr(
            runtime,
            "refresh_inventory",
            AsyncMock(return_value=refresh_request_id),
        )
        monkeypatch.setattr(
            runtime,
            "request_reconciliation",
            AsyncMock(return_value=reconciliation_request_id),
        )
        refresh = client.post(
            f"/api/v1/agents/{bench.agent_id}/actions/refresh-inventory",
            headers=headers,
        )
        reconcile = client.post(
            f"/api/v1/operations/{operation_id}/reconcile",
            headers=headers,
        )
        assert refresh.status_code == 202, refresh.text
        assert reconcile.status_code == 202, reconcile.text

        ci = client.post(
            "/api/v1/ci/sessions",
            headers={**headers, "Idempotency-Key": "bounded-audit-ci"},
            json={"provider": "local", "external_run_id": "bounded-audit-ci"},
        )
        assert ci.status_code == 201, ci.text
        ci_session_id = UUID(ci.json()["id"])
        artifact_content = b"bounded-audit-artifact"
        artifact = client.post(
            "/api/v1/artifacts",
            headers=headers,
            data={
                "owner_type": ArtifactOwnerType.CI_SESSION.value,
                "owner_id": str(ci_session_id),
                "artifact_type": "test-results",
                "expected_sha256": hashlib.sha256(artifact_content).hexdigest(),
            },
            files={"file": ("audit.bin", artifact_content, "application/octet-stream")},
        )
        assert artifact.status_code == 201, artifact.text
        artifact_id = UUID(artifact.json()["id"])
        transfer = client.post(
            f"/api/v1/artifacts/{artifact_id}/transfers",
            headers=headers,
            json={"agent_id": str(bench.agent_id)},
        )
        assert transfer.status_code == 201, transfer.text
        transfer_capability = transfer.json()["token"]
        transfer_id = UUID(transfer.json()["transfer"]["id"])

        heartbeat = client.post(
            f"/api/v1/ci/sessions/{ci_session_id}/heartbeat",
            headers=headers,
        )
        cancelled = client.post(
            f"/api/v1/ci/sessions/{ci_session_id}/cancel",
            headers=headers,
        )
        finalized = client.post(
            f"/api/v1/ci/sessions/{ci_session_id}/finalize",
            headers=headers,
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert cancelled.status_code == 200, cancelled.text
        assert finalized.status_code == 200, finalized.text

        start_ci = client.post(
            "/api/v1/ci/sessions",
            headers={**headers, "Idempotency-Key": "bounded-audit-ci-start"},
            json={"provider": "local", "external_run_id": "bounded-audit-ci-start"},
        )
        assert start_ci.status_code == 201, start_ci.text
        start_session_id = UUID(start_ci.json()["id"])
        start_session = client.portal.call(runtime.ci_repository.get, start_session_id)
        assert start_session is not None
        monkeypatch.setattr(runtime.ci, "start", AsyncMock(return_value=start_session))
        started = client.post(
            f"/api/v1/ci/sessions/{start_session_id}/run",
            headers=headers,
            json={"workflow_name": "bounded-audit-workflow"},
        )
        assert started.status_code == 202, started.text

        revoked = client.delete(
            f"/api/v1/agents/enrollment-tokens/{enrollment_token_id}",
            headers=headers,
        )
        assert revoked.status_code == 204, revoked.text

        legacy_token = client.portal.call(
            _issue_legacy_token,
            runtime,
            ApiTokenScope.AGENTS_ADMIN,
        )
        legacy_enrollment = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers={"Authorization": f"Bearer {legacy_token}"},
            json={"name": "legacy-no-phase6-audit", "expires_in_seconds": 300},
        )
        assert legacy_enrollment.status_code == 201, legacy_enrollment.text
        legacy_enrollment_id = legacy_enrollment.json()["id"]
        assert (
            client.delete(
                f"/api/v1/agents/enrollment-tokens/{legacy_enrollment_id}",
                headers={"Authorization": f"Bearer {legacy_token}"},
            ).status_code
            == 204
        )

        events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            owner.organisation_id,
        )
        by_action = {event.action: event for event in events if event.actor_id == owner.id}
        expected_actions = {
            "AGENT_ENROLLMENT_TOKEN_CREATED",
            "AGENT_ENROLLMENT_TOKEN_REVOKED",
            "INVENTORY_REFRESH_REQUESTED",
            "BENCH_RESERVATION_RENEWED",
            "WORKFLOW_REGISTERED",
            "CI_SESSION_CREATED",
            "CI_SESSION_STARTED",
            "CI_SESSION_HEARTBEAT_RECORDED",
            "CI_SESSION_CANCELLED",
            "CI_SESSION_FINALIZED",
            "OPERATION_RECONCILIATION_REQUESTED",
            "ARTIFACT_TRANSFER_ISSUED",
        }
        assert expected_actions <= by_action.keys()
        assert not any(event.resource_id == legacy_enrollment_id for event in events)
        assert by_action["INVENTORY_REFRESH_REQUESTED"].metadata == {
            "request_id": str(refresh_request_id)
        }
        assert by_action["OPERATION_RECONCILIATION_REQUESTED"].resource_id == str(operation_id)
        assert by_action["OPERATION_RECONCILIATION_REQUESTED"].metadata == {
            "agent_id": str(bench.agent_id),
            "request_id": str(reconciliation_request_id),
        }
        assert by_action["ARTIFACT_TRANSFER_ISSUED"].resource_id == str(artifact_id)
        assert by_action["ARTIFACT_TRANSFER_ISSUED"].metadata == {
            "transfer_id": str(transfer_id),
            "agent_id": str(bench.agent_id),
        }
        assert by_action["CI_SESSION_STARTED"].resource_id == str(start_session_id)
        serialized = "\n".join(event.model_dump_json() for event in events)
        assert enrollment_secret not in serialized
        assert transfer_capability not in serialized
        forbidden_metadata_fragments = {
            "credential",
            "download_url",
            "payload",
            "inputs",
            "allowed_labels",
            "capability",
        }
        for event in events:
            assert forbidden_metadata_fragments.isdisjoint(event.metadata)


def test_ci_hidden_and_owner_only_denials_are_audited(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        session_owner = client.portal.call(_create_user, runtime, "ci-session-owner")
        scoped_non_owner = client.portal.call(_create_user, runtime, "ci-scoped-non-owner")
        organisation_admin = client.portal.call(
            _create_user,
            runtime,
            "ci-organisation-admin",
            OrganisationRole.ADMIN,
        )
        owner_bench, non_owner_bench = client.portal.call(_stage_benches, runtime)
        client.portal.call(
            _grant_direct_role,
            runtime,
            session_owner,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            owner_bench.id,
        )
        client.portal.call(
            _grant_direct_role,
            runtime,
            scoped_non_owner,
            RoleName.LAB_ADMIN,
            ResourceType.BENCH,
            non_owner_bench.id,
        )
        owner_headers = _login(client, session_owner.username)
        scoped_headers = _login(client, scoped_non_owner.username)
        admin_headers = _login(client, organisation_admin.username)

        created = client.post(
            "/api/v1/ci/sessions",
            headers={**owner_headers, "Idempotency-Key": "ci-denial-audit"},
            json={"provider": "local", "external_run_id": "ci-denial-audit"},
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])

        hidden = client.get(
            f"/api/v1/ci/sessions/{session_id}",
            headers=scoped_headers,
        )
        assert hidden.status_code == 404, hidden.text

        owner_only = client.post(
            f"/api/v1/ci/sessions/{session_id}/run",
            headers=admin_headers,
            json={"workflow_name": "never-reached"},
        )
        assert owner_only.status_code == 403, owner_only.text

        events = client.portal.call(
            runtime.identity_repository.list_audit_events,
            session_owner.organisation_id,
        )
        hidden_denial = next(
            event
            for event in events
            if event.action == "PERMISSION_DENIED" and event.actor_id == scoped_non_owner.id
        )
        assert hidden_denial.outcome is AuditOutcome.DENIED
        assert hidden_denial.resource_type == ResourceType.ORGANISATION.value
        assert hidden_denial.resource_id == str(session_owner.organisation_id)
        assert hidden_denial.metadata == {"required_permission": "ci:sessions:read"}

        owner_denial = next(
            event
            for event in events
            if event.action == "PERMISSION_DENIED" and event.actor_id == organisation_admin.id
        )
        assert owner_denial.outcome is AuditOutcome.DENIED
        assert owner_denial.resource_type == "CI_SESSION"
        assert owner_denial.resource_id == str(session_id)
        assert owner_denial.metadata == {"required_permission": "ci:sessions:create"}
        assert "durable CI session principal" in (owner_denial.reason or "")
        assert not any(
            event.action == "CI_SESSION_STARTED" and event.actor_id == organisation_admin.id
            for event in events
        )
