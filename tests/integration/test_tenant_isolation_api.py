from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneRuntime, create_app
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    LeaseApplicationReceipt,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.core.errors import AuthenticationRequiredError
from lab_platform.models import (
    AgentStatus,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    PrincipalType,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
    User,
)

from tests.support.control_plane import make_control_plane_runtime

PASSWORD = "correct horse battery staple"


@dataclass(frozen=True, slots=True)
class TenantResources:
    slug: str
    organisation_id: UUID
    principal_id: UUID
    headers: dict[str, str]
    agent_id: UUID
    bench_id: str
    workflow_name: str
    operation_id: UUID
    reservation_id: UUID
    reservation_lease_version: int
    ci_session_id: UUID
    artifact_id: UUID


@dataclass(frozen=True, slots=True)
class IsolatedLab:
    client: TestClient
    runtime: ControlPlaneRuntime
    first: TenantResources
    second: TenantResources


def _runtime(tmp_path: Path) -> ControlPlaneRuntime:
    return make_control_plane_runtime(
        tmp_path,
        development_enabled=True,
        overrides={
            "agent_gateway": {"monitor_interval_seconds": 3600},
            "distributed": {"queue_commands_for_offline_agents": True},
            "authorisation": {"hide_unauthorised_resources": True},
        },
    )


async def _bootstrap_owner(
    runtime: ControlPlaneRuntime,
    slug: str,
) -> tuple[UUID, UUID]:
    organisation, owner = await runtime.identity_administration.bootstrap_admin(
        organisation_slug=slug,
        organisation_name=f"{slug.title()} Lab",
        username="owner",
        display_name=f"{slug.title()} Owner",
        password=PASSWORD,
    )
    return organisation.id, owner.id


def _login(client: TestClient, slug: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "organisation_slug": slug,
            "username": "owner",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _stage_agent_and_bench(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    slug: str,
) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    issued = await runtime.enrollment.issue_token(
        name=f"{slug}-agent",
        expires_at=now + timedelta(minutes=5),
        organisation_id=organisation_id,
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
        location=f"{slug}-lab",
    )
    observed_at = datetime.now(UTC)
    with runtime.database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE agents SET status = ?, last_connected_at = ?, last_seen_at = ? "
            "WHERE id = ? AND organisation_id = ?",
            (
                AgentStatus.ONLINE.value,
                observed_at.isoformat(),
                observed_at.isoformat(),
                str(enrolled.agent.id),
                str(organisation_id),
            ),
        )
    agent = await runtime.presence.get_agent(
        enrolled.agent.id,
        organisation_id=organisation_id,
    )
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench",
        organisation_id=organisation_id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="bench",
        name=f"{slug.title()} Bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"probe", "reset"}),
        last_seen_at=observed_at,
        created_at=observed_at,
        updated_at=observed_at,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        agent,
        (bench,),
        observed_at=observed_at,
    )
    return agent.id, bench.id


async def _seed_active_reservation(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    principal_id: UUID,
    agent_id: UUID,
    bench_id: str,
    slug: str,
) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    reservation = Reservation(
        id=uuid4(),
        organisation_id=organisation_id,
        bench_id=bench_id,
        owner=f"{slug.title()} Owner",
        owner_principal_id=principal_id,
        owner_principal_type=PrincipalType.USER.value,
        created_at=now,
        requested_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        status=ReservationStatus.SCHEDULED,
        source=ReservationSource.API,
        idempotency_key=f"{slug}:seed-reservation",
    )
    pending = await runtime.reservation_repository.grant_if_eligible(
        ReservationGrantRequest(
            reservation=reservation,
            agent_id=agent_id,
            lease_valid_until=now + timedelta(minutes=30),
        ),
        mutation_key=f"{slug}:seed-reservation",
        request_fingerprint=hashlib.sha256(f"{slug}:seed".encode()).hexdigest(),
        expected_agent_status=AgentStatus.ONLINE,
        expected_bench_status=GlobalBenchStatus.ONLINE,
    )
    assert pending is not None
    active = CoordinatedReservationLease(
        reservation=reservation.model_copy(
            update={"status": ReservationStatus.ACTIVE, "activated_at": now}
        ),
        lease=pending.record.lease,
        state=ReservationLeaseState.ACTIVE,
        revision=2,
    )
    persisted = await runtime.reservation_repository.replace_if_current(
        active,
        expected_revision=1,
        mutation_key=f"{slug}:activate-reservation",
        request_fingerprint=hashlib.sha256(f"{slug}:activate".encode()).hexdigest(),
    )
    assert persisted is not None
    return active


def _create_tenant_resources(
    client: TestClient,
    runtime: ControlPlaneRuntime,
    *,
    slug: str,
    organisation_id: UUID,
    principal_id: UUID,
) -> TenantResources:
    assert client.portal is not None
    headers = _login(client, slug)
    agent_id, bench_id = client.portal.call(
        _stage_agent_and_bench,
        runtime,
        organisation_id,
        slug,
    )

    workflow_name = f"{slug}-probe"
    workflow = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": workflow_name,
            "version": 1,
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        },
    )
    assert workflow.status_code == 201, workflow.text

    probe = client.post(
        f"/api/v1/benches/{bench_id}/actions/probe",
        headers=headers,
        json={},
    )
    assert probe.status_code == 202, probe.text
    operation_id = UUID(probe.json()["operation_id"])

    reservation = client.portal.call(
        _seed_active_reservation,
        runtime,
        organisation_id,
        principal_id,
        agent_id,
        bench_id,
        slug,
    )

    ci = client.post(
        "/api/v1/ci/sessions",
        headers={**headers, "Idempotency-Key": f"{slug}:ci-session"},
        json={
            "provider": "github_actions",
            "external_run_id": f"{slug}-build-1",
            "repository": f"labs/{slug}",
            "ref": "refs/heads/main",
            "commit_sha": slug[0] * 40,
            "actor": "tenant-ci",
        },
    )
    assert ci.status_code == 201, ci.text
    ci_session_id = UUID(ci.json()["id"])

    content = f"{slug}-results".encode()
    artifact = client.post(
        "/api/v1/artifacts",
        headers=headers,
        data={
            "owner_type": "ci_session",
            "owner_id": str(ci_session_id),
            "artifact_type": "test-results",
            "expected_sha256": hashlib.sha256(content).hexdigest(),
        },
        files={"file": (f"{slug}.json", content, "application/json")},
    )
    assert artifact.status_code == 201, artifact.text

    return TenantResources(
        slug=slug,
        organisation_id=organisation_id,
        principal_id=principal_id,
        headers=headers,
        agent_id=agent_id,
        bench_id=bench_id,
        workflow_name=workflow_name,
        operation_id=operation_id,
        reservation_id=reservation.reservation.id,
        reservation_lease_version=reservation.lease.lease_version,
        ci_session_id=ci_session_id,
        artifact_id=UUID(artifact.json()["id"]),
    )


@pytest.fixture
def isolated_lab(tmp_path: Path) -> Iterator[IsolatedLab]:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        first_organisation_id, first_principal_id = client.portal.call(
            _bootstrap_owner,
            runtime,
            "first",
        )
        second_organisation_id, second_principal_id = client.portal.call(
            _bootstrap_owner,
            runtime,
            "second",
        )
        first = _create_tenant_resources(
            client,
            runtime,
            slug="first",
            organisation_id=first_organisation_id,
            principal_id=first_principal_id,
        )
        second = _create_tenant_resources(
            client,
            runtime,
            slug="second",
            organisation_id=second_organisation_id,
            principal_id=second_principal_id,
        )
        yield IsolatedLab(client=client, runtime=runtime, first=first, second=second)


def _assert_ok(response: Any) -> None:
    assert response.status_code == 200, response.text


def _assert_not_found(response: Any) -> None:
    assert response.status_code == 404, response.text


def test_operational_collections_and_named_resources_are_tenant_scoped(
    isolated_lab: IsolatedLab,
) -> None:
    client = isolated_lab.client
    for tenant, foreign in (
        (isolated_lab.first, isolated_lab.second),
        (isolated_lab.second, isolated_lab.first),
    ):
        agents = client.get("/api/v1/agents", headers=tenant.headers)
        benches = client.get("/api/v1/benches", headers=tenant.headers)
        workflows = client.get("/api/v1/workflows", headers=tenant.headers)
        operations = client.get("/api/v1/operations", headers=tenant.headers)
        reservations = client.get("/api/v1/reservations", headers=tenant.headers)
        ci_sessions = client.get("/api/v1/ci/sessions", headers=tenant.headers)
        artifacts = client.get(
            "/api/v1/artifacts",
            headers=tenant.headers,
            params={"owner_type": "ci_session", "owner_id": str(tenant.ci_session_id)},
        )
        for response in (
            agents,
            benches,
            workflows,
            operations,
            reservations,
            ci_sessions,
            artifacts,
        ):
            _assert_ok(response)

        assert {UUID(item["id"]) for item in agents.json()["items"]} == {tenant.agent_id}
        assert {item["id"] for item in benches.json()["items"]} == {tenant.bench_id}
        assert {item["name"] for item in workflows.json()["items"]} == {tenant.workflow_name}
        assert {UUID(item["id"]) for item in operations.json()["items"]} == {tenant.operation_id}
        assert {UUID(item["reservation"]["id"]) for item in reservations.json()["items"]} == {
            tenant.reservation_id
        }
        assert {UUID(item["id"]) for item in ci_sessions.json()["items"]} == {tenant.ci_session_id}
        assert {UUID(item["id"]) for item in artifacts.json()["items"]} == {tenant.artifact_id}
        assert all(
            item["organisation_id"] == str(tenant.organisation_id)
            for response in (agents, benches, workflows, operations, artifacts)
            for item in response.json()["items"]
        )

        foreign_artifact_owner = client.get(
            "/api/v1/artifacts",
            headers=tenant.headers,
            params={"owner_type": "ci_session", "owner_id": str(foreign.ci_session_id)},
        )
        _assert_ok(foreign_artifact_owner)
        assert foreign_artifact_owner.json()["items"] == []

        own_paths = (
            f"/api/v1/agents/{tenant.agent_id}",
            f"/api/v1/benches/{tenant.bench_id}",
            f"/api/v1/workflows/{tenant.workflow_name}",
            f"/api/v1/operations/{tenant.operation_id}",
            f"/api/v1/reservations/{tenant.reservation_id}",
            f"/api/v1/ci/sessions/{tenant.ci_session_id}",
            f"/api/v1/artifacts/{tenant.artifact_id}",
        )
        foreign_paths = (
            f"/api/v1/agents/{foreign.agent_id}",
            f"/api/v1/benches/{foreign.bench_id}",
            f"/api/v1/workflows/{foreign.workflow_name}",
            f"/api/v1/operations/{foreign.operation_id}",
            f"/api/v1/reservations/{foreign.reservation_id}",
            f"/api/v1/ci/sessions/{foreign.ci_session_id}",
            f"/api/v1/artifacts/{foreign.artifact_id}",
        )
        for path in own_paths:
            _assert_ok(client.get(path, headers=tenant.headers))
        for path in foreign_paths:
            _assert_not_found(client.get(path, headers=tenant.headers))

        own_ci_artifacts = client.get(
            f"/api/v1/ci/sessions/{tenant.ci_session_id}/artifacts",
            headers=tenant.headers,
        )
        _assert_ok(own_ci_artifacts)
        assert {UUID(item["id"]) for item in own_ci_artifacts.json()["items"]} == {
            tenant.artifact_id
        }
        _assert_not_found(
            client.get(
                f"/api/v1/ci/sessions/{foreign.ci_session_id}/artifacts",
                headers=tenant.headers,
            )
        )


def test_cross_tenant_operational_mutations_are_hidden_and_rejected(
    isolated_lab: IsolatedLab,
) -> None:
    client = isolated_lab.client
    for tenant, foreign in (
        (isolated_lab.first, isolated_lab.second),
        (isolated_lab.second, isolated_lab.first),
    ):
        _assert_not_found(
            client.post(
                f"/api/v1/agents/{foreign.agent_id}/drain",
                headers=tenant.headers,
                json={"cancel_queued_work": False},
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/benches/{foreign.bench_id}/actions/probe",
                headers=tenant.headers,
                json={},
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/workflows/{foreign.workflow_name}/runs",
                headers=tenant.headers,
                json={
                    "version": 1,
                    "idempotency_key": f"{tenant.slug}:foreign-workflow",
                    "bench_id": foreign.bench_id,
                },
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/operations/{foreign.operation_id}/cancel",
                headers=tenant.headers,
                json={},
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/reservations/{foreign.reservation_id}/release",
                headers=tenant.headers,
                json={
                    "expected_lease_version": foreign.reservation_lease_version,
                    "idempotency_key": f"{tenant.slug}:foreign-release",
                },
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/reservations/{foreign.reservation_id}/revoke",
                headers=tenant.headers,
                json={
                    "expected_lease_version": foreign.reservation_lease_version,
                    "idempotency_key": f"{tenant.slug}:foreign-revoke",
                },
            )
        )
        _assert_not_found(
            client.post(
                "/api/v1/reservations",
                headers=tenant.headers,
                json={
                    "bench_id": foreign.bench_id,
                    "idempotency_key": f"{tenant.slug}:foreign-reservation",
                },
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/ci/sessions/{foreign.ci_session_id}/cancel",
                headers=tenant.headers,
                json={},
            )
        )
        _assert_not_found(
            client.post(
                f"/api/v1/artifacts/{foreign.artifact_id}/transfers",
                headers=tenant.headers,
                json={"agent_id": str(tenant.agent_id)},
            )
        )
        _assert_not_found(
            client.delete(
                f"/api/v1/artifacts/{foreign.artifact_id}",
                headers=tenant.headers,
            )
        )

        cross_owner_content = f"{tenant.slug}-cross-owner".encode()
        _assert_not_found(
            client.post(
                "/api/v1/artifacts",
                headers=tenant.headers,
                data={
                    "owner_type": "ci_session",
                    "owner_id": str(foreign.ci_session_id),
                    "artifact_type": "test-results",
                    "expected_sha256": hashlib.sha256(cross_owner_content).hexdigest(),
                },
                files={
                    "file": (
                        f"{tenant.slug}-cross.json",
                        cross_owner_content,
                        "application/json",
                    )
                },
            )
        )

        _assert_ok(client.get(f"/api/v1/agents/{foreign.agent_id}", headers=foreign.headers))
        _assert_ok(
            client.get(f"/api/v1/operations/{foreign.operation_id}", headers=foreign.headers)
        )
        _assert_ok(
            client.get(
                f"/api/v1/reservations/{foreign.reservation_id}",
                headers=foreign.headers,
            )
        )
        _assert_ok(
            client.get(f"/api/v1/ci/sessions/{foreign.ci_session_id}", headers=foreign.headers)
        )
        foreign_artifacts = client.get(
            "/api/v1/artifacts",
            headers=foreign.headers,
            params={"owner_type": "ci_session", "owner_id": str(foreign.ci_session_id)},
        )
        _assert_ok(foreign_artifacts)
        assert {UUID(item["id"]) for item in foreign_artifacts.json()["items"]} == {
            foreign.artifact_id
        }


def test_foreign_workflow_artifact_input_is_rejected_before_dispatch_side_effects(
    isolated_lab: IsolatedLab,
) -> None:
    client = isolated_lab.client
    runtime = isolated_lab.runtime
    tenant = isolated_lab.first
    foreign = isolated_lab.second
    workflow_name = "first-artifact-input"
    created = client.post(
        "/api/v1/workflows",
        headers=tenant.headers,
        json={
            "name": workflow_name,
            "version": 1,
            "inputs": {"firmware": {"type": "artifact", "required": True}},
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        },
    )
    assert created.status_code == 201, created.text

    grant = AsyncMock(wraps=runtime.reservations.grant)
    issue_download = AsyncMock(wraps=runtime.workflow_artifacts.issue_download)
    create_command = AsyncMock(wraps=runtime.commands.create)
    with (
        patch.object(runtime.reservations, "grant", new=grant),
        patch.object(runtime.workflow_artifacts, "issue_download", new=issue_download),
        patch.object(runtime.commands, "create", new=create_command),
    ):
        denied = client.post(
            f"/api/v1/workflows/{workflow_name}/runs",
            headers=tenant.headers,
            json={
                "idempotency_key": "first:foreign-artifact-input",
                "bench_id": tenant.bench_id,
                "inputs": {"firmware": {"artifact_id": str(foreign.artifact_id)}},
            },
        )
    assert denied.status_code == 404, denied.text
    grant.assert_not_awaited()
    issue_download.assert_not_awaited()
    create_command.assert_not_awaited()


def test_two_tenants_can_dispatch_workflows_with_the_same_idempotency_key(
    isolated_lab: IsolatedLab,
) -> None:
    client = isolated_lab.client
    runtime = isolated_lab.runtime

    # Free both benches through the authenticated API so each workflow dispatch
    # reaches the durable reservation mutation table rather than a permissive fake.
    for tenant in (isolated_lab.first, isolated_lab.second):
        released = client.post(
            f"/api/v1/reservations/{tenant.reservation_id}/release",
            headers=tenant.headers,
            json={
                "expected_lease_version": tenant.reservation_lease_version,
                "idempotency_key": f"{tenant.slug}:release-for-shared-workflow-key",
            },
        )
        assert released.status_code == 200, released.text

    async def confirm_lease(lease: ReservationLease) -> LeaseApplicationReceipt:
        return LeaseApplicationReceipt(
            reservation_id=lease.reservation_id,
            agent_id=lease.agent_id,
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            confirmed_at=datetime.now(UTC),
        )

    apply_lease = AsyncMock(side_effect=confirm_lease)
    shared_key = "shared-workflow-idempotency-key"
    with patch.object(runtime.lease_synchronizer, "apply_lease", new=apply_lease):
        first = client.post(
            f"/api/v1/workflows/{isolated_lab.first.workflow_name}/runs",
            headers=isolated_lab.first.headers,
            json={
                "idempotency_key": shared_key,
                "bench_id": isolated_lab.first.bench_id,
            },
        )
        second = client.post(
            f"/api/v1/workflows/{isolated_lab.second.workflow_name}/runs",
            headers=isolated_lab.second.headers,
            json={
                "idempotency_key": shared_key,
                "bench_id": isolated_lab.second.bench_id,
            },
        )

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert apply_lease.await_count == 2
    assert UUID(first.json()["operation"]["id"]) != UUID(second.json()["operation"]["id"])

    with runtime.database.transaction() as connection:
        rows = connection.execute(
            "SELECT organisation_id, mutation_key FROM reservation_lease_mutations "
            "WHERE mutation_key LIKE 'workflow-reservation:%' ORDER BY organisation_id"
        ).fetchall()
    assert {UUID(row["organisation_id"]) for row in rows} == {
        isolated_lab.first.organisation_id,
        isolated_lab.second.organisation_id,
    }
    assert len({row["mutation_key"] for row in rows}) == 2


def _runtime_tenant_isolation(tmp_path: Path) -> ControlPlaneRuntime:
    return make_control_plane_runtime(tmp_path, development_enabled=True)


async def _bootstrap_tenant_owners(
    runtime: ControlPlaneRuntime,
) -> tuple[User, User]:
    _default_organisation, default_owner = await runtime.identity_administration.bootstrap_admin(
        organisation_slug="default",
        organisation_name="Default Lab",
        username="owner",
        display_name="Default Owner",
        password=PASSWORD,
    )
    _second_organisation, second_owner = await runtime.identity_administration.bootstrap_admin(
        organisation_slug="second",
        organisation_name="Second Lab",
        username="owner",
        display_name="Second Owner",
        password=PASSWORD,
    )
    return default_owner, second_owner


async def _enroll_tenant_agents(
    runtime: ControlPlaneRuntime,
    organisation_id: UUID,
    *names: str,
) -> list[UUID]:
    agent_ids: list[UUID] = []
    for name in names:
        issued = await runtime.enrollment.issue_token(
            name=name,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            organisation_id=organisation_id,
            allow_internal_authorisation=True,
        )
        enrolled = await runtime.enrollment.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=uuid4(),
            agent_version="0.7.0-alpha",
            protocol_version="1.0",
        )
        agent_ids.append(enrolled.agent.id)
    return agent_ids


async def _stage_transfer_metric(
    runtime: ControlPlaneRuntime,
    agent_id: UUID,
    marker: int,
    bytes_transferred: int,
) -> None:
    now = datetime.now(UTC)
    transfer = ArtifactTransferRecord(
        agent_id=agent_id,
        artifact_id=UUID(int=10_000 + marker),
        direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
        status=ArtifactTransferStatus.FAILED,
        token_hash=f"{marker:064x}",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        expected_sha256=f"{marker + 100:064x}",
        expected_size_bytes=bytes_transferred,
    )
    await runtime.artifact_transfers.create(transfer)
    attempt = ArtifactTransferAttempt(
        transfer_id=transfer.id,
        attempt_number=1,
        started_at=now,
        completed_at=now,
        bytes_transferred=bytes_transferred,
        error_code="TEST_FAILURE",
    )
    assert (
        await runtime.artifact_transfers.add_attempt(attempt, expected_attempt_count=0) == attempt
    )


def _prometheus_values(response_text: str) -> dict[str, float]:
    return {
        name: float(value)
        for line in response_text.splitlines()
        for name, value in [line.split(maxsplit=1)]
    }


def _login_tenant_isolation(
    client: TestClient, organisation_slug: str
) -> tuple[dict[str, str], Any]:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "organisation_slug": organisation_slug,
            "username": "owner",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload


def test_identity_admin_and_audit_apis_do_not_cross_tenant_boundaries(tmp_path: Path) -> None:
    runtime = _runtime_tenant_isolation(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        default_owner, second_owner = client.portal.call(_bootstrap_tenant_owners, runtime)
        with pytest.raises(AuthenticationRequiredError):
            client.portal.call(runtime.metrics)
        default_headers, default_login = _login_tenant_isolation(client, "default")
        second_headers, second_login = _login_tenant_isolation(client, "second")

        assert default_login["principal"]["id"] == str(default_owner.id)
        assert second_login["principal"]["id"] == str(second_owner.id)
        assert default_login["organisation"]["id"] != second_login["organisation"]["id"]

        default_users = client.get("/api/v1/users", headers=default_headers)
        second_users = client.get("/api/v1/users", headers=second_headers)
        assert [item["id"] for item in default_users.json()["items"]] == [str(default_owner.id)]
        assert [item["id"] for item in second_users.json()["items"]] == [str(second_owner.id)]
        assert (
            client.get(
                f"/api/v1/users/{second_owner.id}",
                headers=default_headers,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/users/{default_owner.id}",
                headers=second_headers,
            ).status_code
            == 404
        )

        default_audit = client.get("/api/v1/audit-events", headers=default_headers)
        second_audit = client.get("/api/v1/audit-events", headers=second_headers)
        assert default_audit.status_code == 200, default_audit.text
        assert second_audit.status_code == 200, second_audit.text
        default_events = default_audit.json()["items"]
        second_events = second_audit.json()["items"]
        default_organisation_id = default_login["organisation"]["id"]
        second_organisation_id = second_login["organisation"]["id"]
        assert default_events and all(
            event["organisation_id"] == default_organisation_id for event in default_events
        )
        assert second_events and all(
            event["organisation_id"] == second_organisation_id for event in second_events
        )
        assert {event["id"] for event in default_events}.isdisjoint(
            event["id"] for event in second_events
        )
        assert (
            client.get(
                f"/api/v1/audit-events/{default_events[0]['id']}",
                headers=second_headers,
            ).status_code
            == 404
        )

        default_agent_ids = client.portal.call(
            _enroll_tenant_agents,
            runtime,
            UUID(default_organisation_id),
            "default-agent",
        )
        second_agent_ids = client.portal.call(
            _enroll_tenant_agents,
            runtime,
            UUID(second_organisation_id),
            "second-agent-one",
            "second-agent-two",
        )
        client.portal.call(
            _stage_transfer_metric,
            runtime,
            default_agent_ids[0],
            1,
            11,
        )
        client.portal.call(
            _stage_transfer_metric,
            runtime,
            second_agent_ids[0],
            2,
            22,
        )
        client.portal.call(
            _stage_transfer_metric,
            runtime,
            second_agent_ids[1],
            3,
            33,
        )
        default_metrics = client.get("/metrics", headers=default_headers)
        second_metrics = client.get("/metrics", headers=second_headers)
        assert default_metrics.status_code == 200, default_metrics.text
        assert second_metrics.status_code == 200, second_metrics.text
        assert _prometheus_values(default_metrics.text)["agents_offline"] == 1
        assert _prometheus_values(second_metrics.text)["agents_offline"] == 2
        assert _prometheus_values(default_metrics.text)["artifact_transfer_bytes"] == 11
        assert _prometheus_values(second_metrics.text)["artifact_transfer_bytes"] == 55
        assert _prometheus_values(default_metrics.text)["artifact_transfer_failures"] == 1
        assert _prometheus_values(second_metrics.text)["artifact_transfer_failures"] == 2

        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["components"]["agent_gateway"] == "healthy"
