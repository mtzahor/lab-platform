from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.models import (
    AuthorisationResource,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Organisation,
    Principal,
    PrincipalType,
    ResourceType,
    Team,
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
                "artifacts": {"directory": tmp_path / "artifacts"},
                "development": {
                    "enabled": True,
                    "allow_insecure_agent_transport": True,
                },
            }
        )
    )


async def _bootstrap_owner(runtime: ControlPlaneRuntime) -> None:
    await runtime.identity_administration.bootstrap_admin(
        organisation_slug=runtime.config.identity.default_organisation_slug,
        organisation_name=runtime.config.identity.default_organisation_name,
        username="owner",
        display_name="Lab Owner",
        password=PASSWORD,
    )


async def _stage_resources(
    runtime: ControlPlaneRuntime,
) -> tuple[GlobalBenchRecord, GlobalBenchRecord, WorkflowDefinition]:
    now = datetime.now(UTC)
    issued = await runtime.enrollment.issue_token(
        name="policy-agent",
        expires_at=now + timedelta(minutes=5),
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
    )
    benches = tuple(
        GlobalBenchRecord(
            id=f"{enrolled.agent.slug}/{local_id}",
            organisation_id=enrolled.agent.organisation_id,
            agent_id=enrolled.agent.id,
            agent_slug=enrolled.agent.slug,
            local_bench_id=local_id,
            name=name,
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.OFFLINE,
            health=HealthStatus.UNHEALTHY,
            capabilities=frozenset({"probe"}),
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        for local_id, name in (("managed", "Managed"), ("other", "Other"))
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        enrolled.agent,
        benches,
        observed_at=now,
    )
    workflow = WorkflowDefinition.model_validate(
        {
            "organisation_id": enrolled.agent.organisation_id,
            "name": "policy-probe",
            "version": 1,
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        }
    )
    await runtime.workflow_repository.save_definition(workflow)
    return benches[0], benches[1], workflow


async def _stage_foreign_resources(
    runtime: ControlPlaneRuntime,
    organisation: Organisation,
    workflow_template: WorkflowDefinition,
) -> tuple[GlobalBenchRecord, WorkflowDefinition]:
    now = datetime.now(UTC)
    issued = await runtime.enrollment.issue_token(
        name="foreign-policy-agent",
        expires_at=now + timedelta(minutes=5),
        organisation_id=organisation.id,
        allow_internal_authorisation=True,
    )
    enrolled = await runtime.enrollment.enroll(
        plaintext_token=issued.plaintext.get_secret_value(),
        request_id=uuid4(),
        agent_version="0.7.0-alpha",
        protocol_version="1.0",
    )
    bench = GlobalBenchRecord(
        id=f"{enrolled.agent.slug}/foreign",
        organisation_id=organisation.id,
        agent_id=enrolled.agent.id,
        agent_slug=enrolled.agent.slug,
        local_bench_id="foreign",
        name="Foreign Bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.OFFLINE,
        health=HealthStatus.UNHEALTHY,
        capabilities=frozenset({"probe"}),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        enrolled.agent,
        (bench,),
        observed_at=now,
    )
    workflow = workflow_template.model_copy(
        update={"organisation_id": organisation.id, "name": "foreign-probe"}
    )
    await runtime.workflow_repository.save_definition(workflow)
    return bench, workflow


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_access_policy_api_is_scoped_tenant_safe_and_immediately_effective(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        client.portal.call(_bootstrap_owner, runtime)
        owner_headers = _login(client, "owner")
        bench, other_bench, workflow = client.portal.call(_stage_resources, runtime)

        default_bench = client.get(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers=owner_headers,
        )
        assert default_bench.status_code == 200, default_bench.text
        assert default_bench.json() == {
            "resource_type": "BENCH",
            "resource_id": bench.id,
            "configured": False,
            "source": "DEFAULT",
            "policy": {
                "bench_id": bench.id,
                "visibility": "ORGANISATION",
                "reservation_role": None,
                "operation_role": None,
                "allowed_team_ids": [],
            },
        }
        default_workflow = client.get(
            f"/api/v1/access-policies/workflows/{workflow.name}",
            headers=owner_headers,
        )
        assert default_workflow.status_code == 200, default_workflow.text
        assert default_workflow.json()["source"] == "DEFAULT"

        scoped_admin = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "scoped-admin",
                "display_name": "Scoped Admin",
                "password": PASSWORD,
                "organisation_role": "MEMBER",
            },
        ).json()
        for resource_type, resource_id in (
            ("AGENT", str(bench.agent_id)),
            ("WORKFLOW", workflow.name),
        ):
            granted = client.post(
                "/api/v1/role-assignments",
                headers=owner_headers,
                json={
                    "subject_type": "USER",
                    "subject_id": scoped_admin["id"],
                    "role": "LAB_ADMIN",
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
            assert granted.status_code == 201, granted.text
        scoped_headers = _login(client, "scoped-admin")
        scoped_bench = client.get(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers=scoped_headers,
        )
        assert scoped_bench.status_code == 200, scoped_bench.text
        scoped_workflow = client.get(
            f"/api/v1/access-policies/workflows/{workflow.name}",
            headers=scoped_headers,
        )
        assert scoped_workflow.status_code == 200, scoped_workflow.text

        team = client.post(
            "/api/v1/teams",
            headers=owner_headers,
            json={"slug": "policy-readers", "name": "Policy Readers"},
        ).json()
        reader = client.post(
            "/api/v1/users",
            headers=owner_headers,
            json={
                "username": "policy-reader",
                "display_name": "Policy Reader",
                "password": PASSWORD,
                "organisation_role": "MEMBER",
            },
        ).json()
        membership = client.post(
            f"/api/v1/teams/{team['id']}/members",
            headers=owner_headers,
            json={"user_id": reader["id"]},
        )
        assert membership.status_code == 201, membership.text
        reader_headers = _login(client, "policy-reader")
        denied_other = client.get(
            f"/api/v1/access-policies/benches/{other_bench.id}",
            headers=reader_headers,
        )
        assert denied_other.status_code == 403
        assert denied_other.json()["error"]["details"]["required_permission"] == ("benches:manage")
        reader_principal = Principal(
            id=UUID(reader["id"]),
            type=PrincipalType.USER,
            organisation_id=bench.organisation_id,
            display_name="Policy Reader",
        )
        bench_resource = AuthorisationResource(
            type=ResourceType.BENCH,
            id=bench.id,
            organisation_id=bench.organisation_id,
            parent_agent_id=bench.agent_id,
        )
        assert not client.portal.call(
            runtime.authorisation.is_allowed,
            reader_principal,
            "benches:read",
            bench_resource,
        )

        configured_bench = client.put(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers=owner_headers,
            json={
                "visibility": "RESTRICTED",
                "reservation_role": "RESERVER",
                "operation_role": "OPERATOR",
                "allowed_team_ids": [team["id"]],
            },
        )
        assert configured_bench.status_code == 200, configured_bench.text
        assert configured_bench.json()["source"] == "CONFIGURED"
        assert configured_bench.json()["policy"]["allowed_team_ids"] == [team["id"]]
        assert client.portal.call(
            runtime.authorisation.is_allowed,
            reader_principal,
            "benches:read",
            bench_resource,
        )

        configured_workflow = client.put(
            f"/api/v1/access-policies/workflows/{workflow.name}",
            headers=owner_headers,
            json={"visibility": "ADMIN_ONLY"},
        )
        assert configured_workflow.status_code == 200, configured_workflow.text
        assert configured_workflow.json()["policy"]["visibility"] == "ADMIN_ONLY"

        missing_team = client.put(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers=owner_headers,
            json={
                "visibility": "RESTRICTED",
                "allowed_team_ids": [str(uuid4())],
            },
        )
        assert missing_team.status_code == 404
        assert missing_team.json()["error"]["code"] == "TEAM_NOT_FOUND"

        foreign_org = client.portal.call(
            runtime.identity_repository.create_organisation,
            Organisation(slug="foreign", name="Foreign"),
        )
        foreign_team = client.portal.call(
            runtime.identity_repository.create_team,
            Team(
                organisation_id=foreign_org.id,
                slug="foreign-team",
                name="Foreign Team",
            ),
        )
        foreign_bench, foreign_workflow = client.portal.call(
            _stage_foreign_resources,
            runtime,
            foreign_org,
            workflow,
        )
        for path in (
            f"/api/v1/access-policies/benches/{foreign_bench.id}",
            f"/api/v1/access-policies/workflows/{foreign_workflow.name}",
            "/api/v1/access-policies/benches/missing-agent/missing-bench",
            "/api/v1/access-policies/workflows/missing-workflow",
        ):
            hidden = client.get(path, headers=owner_headers)
            assert hidden.status_code == 404, hidden.text
        foreign_team_rejected = client.put(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers=owner_headers,
            json={
                "visibility": "RESTRICTED",
                "allowed_team_ids": [str(foreign_team.id)],
            },
        )
        assert foreign_team_rejected.status_code == 404
        assert foreign_team_rejected.json()["error"]["code"] == "TEAM_NOT_FOUND"

        service_account = client.post(
            "/api/v1/service-accounts",
            headers=owner_headers,
            json={"name": "narrow-policy-admin"},
        ).json()
        assignment = client.post(
            "/api/v1/role-assignments",
            headers=owner_headers,
            json={
                "subject_type": "SERVICE_ACCOUNT",
                "subject_id": service_account["id"],
                "role": "LAB_ADMIN",
                "resource_type": "BENCH",
                "resource_id": bench.id,
            },
        )
        assert assignment.status_code == 201, assignment.text
        credential = client.post(
            f"/api/v1/service-accounts/{service_account['id']}/credentials",
            headers=owner_headers,
            json={
                "name": "read-only",
                "permission_restrictions": ["benches:read"],
            },
        )
        assert credential.status_code == 201, credential.text
        narrowed = client.get(
            f"/api/v1/access-policies/benches/{bench.id}",
            headers={"Authorization": f"Bearer {credential.json()['token']}"},
        )
        assert narrowed.status_code == 403
        assert narrowed.json()["error"]["details"]["required_permission"] == ("benches:manage")

        bench_audit = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={"action": "BENCH_ACCESS_POLICY_UPDATED"},
        )
        assert bench_audit.status_code == 200, bench_audit.text
        assert bench_audit.json()["items"][0]["resource_id"] == bench.id
        workflow_audit = client.get(
            "/api/v1/audit-events",
            headers=owner_headers,
            params={"action": "WORKFLOW_ACCESS_POLICY_UPDATED"},
        )
        assert workflow_audit.status_code == 200, workflow_audit.text
        assert workflow_audit.json()["items"][0]["resource_id"] == workflow.name
