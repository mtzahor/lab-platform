from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.core.errors import AuthenticationRequiredError
from lab_platform.models import (
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    User,
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


def _login(client: TestClient, organisation_slug: str) -> tuple[dict[str, str], Any]:
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
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        assert client.portal is not None
        default_owner, second_owner = client.portal.call(_bootstrap_tenant_owners, runtime)
        with pytest.raises(AuthenticationRequiredError):
            client.portal.call(runtime.metrics)
        default_headers, default_login = _login(client, "default")
        second_headers, second_login = _login(client, "second")

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
