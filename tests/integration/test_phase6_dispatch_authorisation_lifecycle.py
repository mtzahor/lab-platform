from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent_protocol import CommandCancelPayload
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane_core import DistributedWorkflowRequest
from lab_platform.control_plane_core.reservations import LeaseApplicationReceipt
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    ActorContext,
    ArtifactOwnerType,
    AuthenticationContext,
    AuthorisationResource,
    DistributedOperationStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    OrganisationMembership,
    OrganisationRole,
    PasswordCredential,
    Principal,
    PrincipalType,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
    ResourceType,
    RoleAssignment,
    RoleName,
    RoleSubjectType,
    User,
    WorkflowDefinition,
)

PASSWORD = "correct horse battery staple"


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {
                    "host": "127.0.0.1",
                    "port": 8443,
                    "public_url": "http://127.0.0.1:8443",
                },
                "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                "agent_gateway": {"monitor_interval_seconds": 3_600},
                "distributed": {"queue_commands_for_offline_agents": True},
                "artifacts": {"directory": tmp_path / "artifacts"},
                "authorisation": {"hide_unauthorised_resources": True},
                "development": {
                    "enabled": True,
                    "allow_insecure_agent_transport": True,
                },
            }
        )
    )


async def _create_member(runtime: ControlPlaneRuntime) -> User:
    organisation = await runtime.identity_repository.get_organisation_by_slug(
        runtime.config.identity.default_organisation_slug
    )
    assert organisation is not None
    now = datetime.now(UTC)
    user = User(
        organisation_id=organisation.id,
        username="dispatch-operator",
        display_name="Dispatch Operator",
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
            role=OrganisationRole.MEMBER,
            created_at=now,
        ),
    )
    return user


async def _stage_online_route(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
) -> GlobalBenchRecord:
    now = datetime.now(UTC)
    issued = await runtime.enrollment.issue_token(
        name="dispatch-lifecycle-agent",
        expires_at=now + timedelta(minutes=5),
        organisation_id=organisation_id,
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
    )
    registered = await runtime.presence.register_authenticated_connection(
        enrolled.agent,
        connection_id=uuid4(),
        boot_id=uuid4(),
        protocol_version="1.0",
        agent_version="0.7.0-alpha",
        observed_at=now,
        observed_monotonic=time.monotonic(),
    )
    bench = GlobalBenchRecord(
        id=f"{registered.agent.slug}/dispatch-bench",
        organisation_id=organisation_id,
        agent_id=registered.agent.id,
        agent_slug=registered.agent.slug,
        local_bench_id="dispatch-bench",
        name="Dispatch lifecycle bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe"}),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        registered.agent,
        (bench,),
        observed_at=now,
    )
    return bench


async def _save_artifact_workflow(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
) -> WorkflowDefinition:
    definition = WorkflowDefinition.model_validate(
        {
            "organisation_id": organisation_id,
            "name": "snapshot-trace-workflow",
            "version": 1,
            "inputs": {"evidence": {"type": "artifact", "required": True}},
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        }
    )
    return await runtime.workflow_repository.save_definition(definition)


async def _grant_exact_dispatch_roles(
    runtime: ControlPlaneRuntime,
    user: User,
    definition: WorkflowDefinition,
    bench: GlobalBenchRecord,
) -> tuple[RoleAssignment, RoleAssignment]:
    now = datetime.now(UTC)
    workflow = RoleAssignment(
        organisation_id=user.organisation_id,
        subject_type=RoleSubjectType.USER,
        subject_id=user.id,
        role=RoleName.WORKFLOW_RUNNER,
        resource_type=ResourceType.WORKFLOW,
        resource_id=definition.name,
        created_by=user.id,
        created_at=now,
    )
    bench_access = RoleAssignment(
        organisation_id=user.organisation_id,
        subject_type=RoleSubjectType.USER,
        subject_id=user.id,
        role=RoleName.WORKFLOW_RUNNER,
        resource_type=ResourceType.BENCH,
        resource_id=bench.id,
        created_by=user.id,
        created_at=now,
    )
    return (
        await runtime.identity_repository.create_role_assignment(workflow),
        await runtime.identity_repository.create_role_assignment(bench_access),
    )


async def _seed_operation_artifact(
    runtime: ControlPlaneRuntime,
    bench: GlobalBenchRecord,
) -> UUID:
    command, operation = await runtime.commands.create(
        agent_id=bench.agent_id,
        bench_id=bench.id,
        command_type=RemoteCommandType.PROBE,
        payload={"owner": "trusted-test-fixture"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        idempotency_key="dispatch-lifecycle-artifact-parent",
        operation_type=RemoteCommandType.PROBE.value,
        dispatch=False,
        allow_internal_authorisation=True,
    )
    assert command.operation_id is not None
    assert operation is not None
    assert command.operation_id == operation.id
    content = b"authorised workflow input"
    artifact = await runtime.platform_artifacts.store_bytes(
        content,
        owner_type=ArtifactOwnerType.OPERATION,
        owner_id=operation.id,
        name="evidence.bin",
        artifact_type="test-input",
        content_type="application/octet-stream",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        organisation_id=bench.organisation_id,
    )
    return artifact.id


async def _authorisation_snapshot_count(runtime: ControlPlaneRuntime) -> int:
    with runtime.database.transaction() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM authorisation_snapshots").fetchone()
    assert row is not None
    return int(row["count"])


async def _mark_ci_work_dispatched(
    runtime: ControlPlaneRuntime,
    session_id: UUID,
) -> UUID:
    binding = await runtime.ci_repository.get_distributed_workflow(session_id)
    assert binding is not None
    command = await runtime.command_repository.get_command(binding.remote_command_id)
    operation = await runtime.command_repository.get_operation_for_command(
        binding.remote_command_id
    )
    assert command is not None
    assert operation is not None
    dispatched_at = max(datetime.now(UTC), command.created_at)
    persisted_command = await runtime.command_repository.update_command(
        command.model_copy(
            update={"status": RemoteCommandStatus.DISPATCHED, "dispatched_at": dispatched_at}
        ),
        expected_statuses={RemoteCommandStatus.CREATED, RemoteCommandStatus.QUEUED},
    )
    persisted_operation = await runtime.command_repository.update_operation(
        operation.model_copy(
            update={
                "status": DistributedOperationStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
            }
        ),
        expected_statuses={DistributedOperationStatus.CREATED},
    )
    assert persisted_command is not None
    assert persisted_operation is not None
    return command.id


def _login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "dispatch-operator", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_runtime_coordinator_rejects_forged_persisted_workflow_before_side_effects(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(_create_member, runtime)
        trusted = client.portal.call(
            _save_artifact_workflow,
            runtime,
            user.organisation_id,
        )
        forged = WorkflowDefinition.model_validate(
            {
                **trusted.model_dump(mode="python"),
                "inputs": {},
                "requirements": {"capabilities": []},
                "steps": [{"action": "wait", "seconds": 1}],
            }
        )
        principal = Principal(
            id=user.id,
            type=PrincipalType.USER,
            organisation_id=user.organisation_id,
            display_name=user.display_name,
        )
        context = AuthenticationContext(principal=principal)
        actor = ActorContext(
            principal_id=principal.id,
            principal_type=principal.type,
            display_name=principal.display_name,
            organisation_id=principal.organisation_id,
        )
        request = DistributedWorkflowRequest(
            definition=forged,
            owner=principal.display_name,
            idempotency_key="forged-runtime-definition",
            actor_context=actor,
            authentication_context=context,
            organisation_id=principal.organisation_id,
        )

        catalog_get = AsyncMock(wraps=runtime.workflow_repository.get_definition)
        require_artifact = AsyncMock(wraps=runtime.workflow_artifacts.require_access)
        grant_reservation = AsyncMock(wraps=runtime.reservations.grant)
        issue_transfer = AsyncMock(wraps=runtime.workflow_artifacts.issue_download)
        create_command = AsyncMock(wraps=runtime.commands.create)
        dispatch_command = AsyncMock(wraps=runtime.commands.dispatch)
        with (
            patch.object(runtime.workflow_repository, "get_definition", new=catalog_get),
            patch.object(
                runtime.workflow_artifacts,
                "require_access",
                new=require_artifact,
            ),
            patch.object(runtime.reservations, "grant", new=grant_reservation),
            patch.object(
                runtime.workflow_artifacts,
                "issue_download",
                new=issue_transfer,
            ),
            patch.object(runtime.commands, "create", new=create_command),
            patch.object(runtime.commands, "dispatch", new=dispatch_command),
            pytest.raises(WorkflowInvalidError, match="trusted catalog"),
        ):
            client.portal.call(runtime.workflows.run, request)

        catalog_get.assert_awaited_once_with(
            trusted.name,
            trusted.version,
            organisation_id=user.organisation_id,
        )
        require_artifact.assert_not_awaited()
        grant_reservation.assert_not_awaited()
        issue_transfer.assert_not_awaited()
        create_command.assert_not_awaited()
        dispatch_command.assert_not_awaited()


def test_new_dispatch_uses_current_grants_while_prior_dispatch_keeps_its_actor_trace(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(_create_member, runtime)
        bench = client.portal.call(_stage_online_route, runtime, user.organisation_id)
        definition = client.portal.call(
            _save_artifact_workflow,
            runtime,
            user.organisation_id,
        )
        workflow_assignment, _bench_assignment = client.portal.call(
            _grant_exact_dispatch_roles,
            runtime,
            user,
            definition,
            bench,
        )
        artifact_id = client.portal.call(_seed_operation_artifact, runtime, bench)
        headers = _login(client)

        async def confirm_lease(lease: object) -> LeaseApplicationReceipt:
            assert isinstance(lease, ReservationLease)
            return LeaseApplicationReceipt(
                reservation_id=lease.reservation_id,
                agent_id=lease.agent_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                confirmed_at=datetime.now(UTC),
            )

        apply_lease = AsyncMock(side_effect=confirm_lease)
        with patch.object(runtime.lease_synchronizer, "apply_lease", new=apply_lease):
            first = client.post(
                f"/api/v1/workflows/{definition.name}/runs",
                headers=headers,
                json={
                    "idempotency_key": "dispatch-before-role-revocation",
                    "bench_id": bench.id,
                    "inputs": {"evidence": {"artifact_id": str(artifact_id)}},
                },
            )
        assert first.status_code == 202, first.text
        apply_lease.assert_awaited_once()
        assert client.portal.call(_authorisation_snapshot_count, runtime) == 1

        replay = client.post(
            f"/api/v1/workflows/{definition.name}/runs",
            headers=headers,
            json={
                "idempotency_key": "dispatch-before-role-revocation",
                "bench_id": bench.id,
                "inputs": {"evidence": {"artifact_id": str(artifact_id)}},
            },
        )
        assert replay.status_code == 202, replay.text
        assert replay.json() == first.json()
        # The command service is the sole decision-evidence minter. HTTP replay
        # therefore does not leak an unused route-level snapshot.
        assert client.portal.call(_authorisation_snapshot_count, runtime) == 1

        payload = first.json()
        assert [item["artifact_id"] for item in payload["artifact_transfers"]] == [str(artifact_id)]
        command_id = UUID(payload["command"]["id"])
        operation_id = UUID(payload["operation"]["id"])
        command = client.portal.call(
            partial(
                runtime.command_repository.get_command,
                command_id,
                organisation_id=user.organisation_id,
            )
        )
        operation = client.portal.call(
            partial(
                runtime.operation_records.get,
                operation_id,
                organisation_id=user.organisation_id,
            )
        )
        assert command is not None
        assert operation is not None
        assert operation.remote_command_id == command.id
        assert command.actor_context is not None
        assert command.actor_context.principal_id == user.id
        assert command.actor_context.principal_type is PrincipalType.USER
        assert command.actor_context.organisation_id == user.organisation_id
        assert command.authorisation_snapshot_id is not None
        assert command.actor_context.authorisation_snapshot_id == command.authorisation_snapshot_id
        snapshot = client.portal.call(
            runtime.identity_repository.get_authorisation_snapshot,
            user.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.principal_id == user.id
        assert snapshot.permission == "workflows:run"
        assert snapshot.resource_type is ResourceType.WORKFLOW
        assert snapshot.resource_id == definition.name
        assert snapshot.granted_by_assignments == [workflow_assignment.id]

        assert client.portal.call(
            runtime.identity_repository.delete_role_assignment,
            user.organisation_id,
            workflow_assignment.id,
        )
        assert (
            client.portal.call(
                runtime.identity_repository.get_role_assignment,
                user.organisation_id,
                workflow_assignment.id,
            )
            is None
        )
        require_artifact = AsyncMock(wraps=runtime.workflow_artifacts.require_access)
        grant_reservation = AsyncMock(wraps=runtime.reservations.grant)
        issue_transfer = AsyncMock(wraps=runtime.workflow_artifacts.issue_download)
        create_command = AsyncMock(wraps=runtime.commands.create)
        dispatch_command = AsyncMock(wraps=runtime.commands.dispatch)
        with (
            patch.object(
                runtime.workflow_artifacts,
                "require_access",
                new=require_artifact,
            ),
            patch.object(runtime.reservations, "grant", new=grant_reservation),
            patch.object(
                runtime.workflow_artifacts,
                "issue_download",
                new=issue_transfer,
            ),
            patch.object(runtime.commands, "create", new=create_command),
            patch.object(runtime.commands, "dispatch", new=dispatch_command),
        ):
            denied = client.post(
                f"/api/v1/workflows/{definition.name}/runs",
                headers=headers,
                json={
                    "idempotency_key": "dispatch-after-role-revocation",
                    "bench_id": bench.id,
                    "inputs": {"evidence": {"artifact_id": str(artifact_id)}},
                },
            )
        assert denied.status_code == 404, denied.text
        assert denied.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"
        require_artifact.assert_not_awaited()
        grant_reservation.assert_not_awaited()
        issue_transfer.assert_not_awaited()
        create_command.assert_not_awaited()
        dispatch_command.assert_not_awaited()

        traced_command = client.portal.call(
            partial(
                runtime.command_repository.get_command,
                command_id,
                organisation_id=user.organisation_id,
            )
        )
        traced_operation = client.portal.call(
            partial(
                runtime.operation_records.get,
                operation_id,
                organisation_id=user.organisation_id,
            )
        )
        retained_snapshot = client.portal.call(
            runtime.identity_repository.get_authorisation_snapshot,
            user.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert traced_command == command
        assert traced_operation == operation
        assert retained_snapshot == snapshot

    reopened = _runtime(tmp_path)
    with TestClient(create_app(reopened)) as restarted_client:
        assert restarted_client.portal is not None
        restarted_command = restarted_client.portal.call(
            partial(
                reopened.command_repository.get_command,
                command_id,
                organisation_id=user.organisation_id,
            )
        )
        restarted_snapshot = restarted_client.portal.call(
            reopened.identity_repository.get_authorisation_snapshot,
            user.organisation_id,
            command.authorisation_snapshot_id,
        )
        assert restarted_command == command
        assert restarted_snapshot == snapshot


def test_workflow_runner_ci_cancel_carries_actor_and_survives_restart_retry(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    first_attempts: list[CommandCancelPayload] = []

    async def send_cancel(
        _agent_id: UUID,
        payload: CommandCancelPayload,
        **_kwargs: object,
    ) -> object:
        first_attempts.append(payload)
        return object()

    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        user = client.portal.call(_create_member, runtime)
        bench = client.portal.call(_stage_online_route, runtime, user.organisation_id)
        definition = client.portal.call(
            _save_artifact_workflow,
            runtime,
            user.organisation_id,
        )
        cancellation_assignments = client.portal.call(
            _grant_exact_dispatch_roles,
            runtime,
            user,
            definition,
            bench,
        )
        artifact_id = client.portal.call(_seed_operation_artifact, runtime, bench)
        headers = _login(client)
        principal = Principal(
            id=user.id,
            type=PrincipalType.USER,
            organisation_id=user.organisation_id,
            display_name=user.display_name,
        )
        allowed = client.portal.call(
            runtime.authorisation.is_allowed,
            principal,
            "operations:cancel",
            AuthorisationResource(
                type=ResourceType.BENCH,
                id=bench.id,
                organisation_id=user.organisation_id,
                parent_agent_id=bench.agent_id,
            ),
        )
        assert not allowed

        created = client.post(
            "/api/v1/ci/sessions",
            headers={**headers, "Idempotency-Key": "ci-cancel-actor"},
            json={
                "provider": "local",
                "external_run_id": "ci-cancel-actor",
                "bench_request": {"explicit_bench_id": bench.id},
            },
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])

        async def confirm_lease(lease: object) -> LeaseApplicationReceipt:
            assert isinstance(lease, ReservationLease)
            return LeaseApplicationReceipt(
                reservation_id=lease.reservation_id,
                agent_id=lease.agent_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                confirmed_at=datetime.now(UTC),
            )

        with patch.object(
            runtime.lease_synchronizer,
            "apply_lease",
            new=AsyncMock(side_effect=confirm_lease),
        ):
            started = client.post(
                f"/api/v1/ci/sessions/{session_id}/run",
                headers=headers,
                json={
                    "workflow_name": definition.name,
                    "workflow_version": definition.version,
                    "inputs": {"evidence": {"artifact_id": str(artifact_id)}},
                },
            )
        assert started.status_code == 202, started.text
        command_id = client.portal.call(_mark_ci_work_dispatched, runtime, session_id)

        with patch.object(runtime.hub, "send_cancel", new=AsyncMock(side_effect=send_cancel)):
            cancelled = client.post(
                f"/api/v1/ci/sessions/{session_id}/cancel",
                headers=headers,
            )
        assert cancelled.status_code == 200, cancelled.text
        assert len(first_attempts) == 1
        initial_payload = first_attempts[0]
        assert initial_payload.command_id == command_id
        assert initial_payload.actor_context is not None
        assert initial_payload.actor_context.principal_id == user.id
        snapshot_id = initial_payload.authorisation_snapshot_id
        assert snapshot_id is not None
        snapshot = client.portal.call(
            runtime.identity_repository.get_authorisation_snapshot,
            user.organisation_id,
            snapshot_id,
        )
        assert snapshot is not None
        assert snapshot.permission == "ci:sessions:cancel"
        assert snapshot.resource_type == "CI_SESSION"
        assert snapshot.resource_id == str(session_id)
        assert snapshot.granted_by_assignments == [
            assignment.id
            for assignment in sorted(cancellation_assignments, key=lambda item: str(item.id))
        ]
        persisted = client.portal.call(
            partial(
                runtime.ci.get,
                session_id,
                synchronize=False,
                organisation_id=user.organisation_id,
            )
        )
        assert persisted.cancel_actor_context == initial_payload.actor_context
        assert persisted.cancel_authorisation_snapshot_id == snapshot_id
        errors = client.portal.call(
            partial(
                runtime.ci_repository.errors,
                session_id,
                organisation_id=user.organisation_id,
            )
        )
        assert errors == []

        failed_attempts: list[CommandCancelPayload] = []

        async def fail_cancel(
            _agent_id: UUID,
            payload: CommandCancelPayload,
            **_kwargs: object,
        ) -> object:
            failed_attempts.append(payload)
            raise RuntimeError("simulated enqueue failure")

        with patch.object(runtime.hub, "send_cancel", new=AsyncMock(side_effect=fail_cancel)):
            replayed = client.post(
                f"/api/v1/ci/sessions/{session_id}/cancel",
                headers=headers,
            )
        assert replayed.status_code == 200, replayed.text
        assert len(failed_attempts) == 1
        assert failed_attempts[0].actor_context == initial_payload.actor_context
        assert failed_attempts[0].authorisation_snapshot_id == snapshot_id
        errors = client.portal.call(
            partial(
                runtime.ci_repository.errors,
                session_id,
                organisation_id=user.organisation_id,
            )
        )
        assert errors == ["remote cancellation delivery failed: RuntimeError"]

    retried: list[CommandCancelPayload] = []

    async def retry_cancel(
        _agent_id: UUID,
        payload: CommandCancelPayload,
        **_kwargs: object,
    ) -> object:
        retried.append(payload)
        return object()

    restarted = _runtime(tmp_path)
    with (
        patch.object(restarted.hub, "send_cancel", new=AsyncMock(side_effect=retry_cancel)),
        TestClient(create_app(restarted)) as restarted_client,
    ):
        assert restarted_client.portal is not None
        result = restarted_client.portal.call(restarted.ci.process_maintenance)
        assert result.examined >= 1
    assert retried
    assert {payload.command_id for payload in retried} == {command_id}
    assert {payload.actor_context for payload in retried} == {initial_payload.actor_context}
    assert {payload.authorisation_snapshot_id for payload in retried} == {snapshot_id}
