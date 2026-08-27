from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane.operational_api import reconcile_operational_alerts
from lab_platform.control_plane.operational_state import EventBackedOperationalState
from lab_platform.core import create_alert
from lab_platform.models import (
    BENCH_MAINTENANCE_LABEL,
    AgentRecord,
    AlertStatus,
    AlertType,
    ApiTokenScope,
    BenchMaintenanceStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
)


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
                "development": {"allow_insecure_agent_transport": True},
            }
        )
    )


def _bootstrap_admin(client: TestClient) -> dict[str, str]:
    issued = client.post(
        "/api/v1/tokens",
        json={
            "name": "phase9-admin",
            "owner": "phase9-tests",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _issue_token(
    client: TestClient,
    admin_headers: dict[str, str],
    name: str,
    *scopes: ApiTokenScope,
) -> dict[str, str]:
    issued = client.post(
        "/api/v1/tokens",
        headers=admin_headers,
        json={
            "name": name,
            "owner": "phase9-tests",
            "scopes": [scope.value for scope in scopes],
        },
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _enroll_agent(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    enrollment = client.post(
        "/api/v1/agents/enrollment-tokens",
        headers=headers,
        json={"name": "phase9-agent", "expires_in_seconds": 300},
    )
    assert enrollment.status_code == 201, enrollment.text
    response = client.post(
        "/api/v1/agents/enroll",
        json={
            "enrollment_token": enrollment.json()["token"],
            "agent_version": "0.9.0-beta",
            "protocol_version": "1.0",
            "location": "phase9-lab",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_operational_api_maintenance_is_durable_and_fences_inventory(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap_admin(client)
        enrolled = _enroll_agent(client, headers)
        enrolled_agent = AgentRecord.model_validate(enrolled["agent"])
        now = datetime.now(UTC)
        bench = GlobalBenchRecord(
            id=f"{enrolled_agent.slug}/bench-01",
            organisation_id=enrolled_agent.organisation_id,
            agent_id=enrolled_agent.id,
            agent_slug=enrolled_agent.slug,
            local_bench_id="bench-01",
            name="Phase 9 bench",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            capabilities=frozenset({"reset"}),
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        asyncio.run(
            runtime.inventory_repository.reconcile_agent_snapshot(
                enrolled_agent,
                (bench,),
                observed_at=now,
            )
        )

        invalid_reason = client.post(
            f"/api/v1/operational/benches/{bench.id}/maintenance/start",
            headers=headers,
            json={"reason": "   "},
        )
        assert invalid_reason.status_code == 422, invalid_reason.text
        unchanged = asyncio.run(runtime.benches.get(bench.id))
        assert unchanged is not None
        assert unchanged.status is GlobalBenchStatus.ONLINE
        not_started = client.post(
            f"/api/v1/operational/benches/{bench.id}/maintenance/end",
            headers=headers,
        )
        assert not_started.status_code == 409, not_started.text

        started = client.post(
            f"/api/v1/operational/benches/{bench.id}/maintenance/start",
            headers=headers,
            json={"reason": "  Replace the debugger cable  "},
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "MAINTENANCE"
        assert started.json()["reason"] == "Replace the debugger cable"

        stored = asyncio.run(runtime.benches.get(bench.id))
        assert stored is not None
        assert stored.status is GlobalBenchStatus.DEGRADED
        assert stored.labels[BENCH_MAINTENANCE_LABEL] == "true"

        # A fresh Agent inventory snapshot must not accidentally reopen a bench
        # that an administrator fenced for maintenance.
        incoming = bench.model_copy(update={"updated_at": now + timedelta(seconds=1)})
        asyncio.run(
            runtime.inventory_repository.reconcile_agent_snapshot(
                enrolled_agent,
                (incoming,),
                observed_at=now + timedelta(seconds=1),
            )
        )
        preserved = asyncio.run(runtime.benches.get(bench.id))
        assert preserved is not None
        assert preserved.status is GlobalBenchStatus.DEGRADED
        assert preserved.labels[BENCH_MAINTENANCE_LABEL] == "true"

        analytics = client.get(
            "/api/v1/operational/analytics?window_hours=24",
            headers=headers,
        )
        assert analytics.status_code == 200, analytics.text
        bench_row = analytics.json()["benches"][0]
        assert bench_row["maintenance"]["status"] == "MAINTENANCE"
        assert "Agent-connected" in analytics.json()["semantics"]["availability"]

        ended = client.post(
            f"/api/v1/operational/benches/{bench.id}/maintenance/end",
            headers=headers,
        )
        assert ended.status_code == 200, ended.text
        assert ended.json()["status"] == "HEALTHY"
        restored = asyncio.run(runtime.benches.get(bench.id))
        assert restored is not None
        assert restored.status is GlobalBenchStatus.ONLINE
        assert BENCH_MAINTENANCE_LABEL not in restored.labels

        # The append-only journal survives service reconstruction.
        reconstructed = EventBackedOperationalState(runtime.audit_events)
        state = asyncio.run(
            reconstructed.maintenance_state(
                bench.id,
                organisation_id=restored.organisation_id,
            )
        )
        assert state.status is BenchMaintenanceStatus.HEALTHY


def test_monitor_generates_and_manages_basic_web_alerts(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap_admin(client)
        enrolled = _enroll_agent(client, headers)
        agent = AgentRecord.model_validate(enrolled["agent"])
        now = datetime.now(UTC)
        degraded = GlobalBenchRecord(
            id=f"{agent.slug}/bench-degraded",
            organisation_id=agent.organisation_id,
            agent_id=agent.id,
            agent_slug=agent.slug,
            local_bench_id="bench-degraded",
            name="Degraded Phase 9 bench",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.DEGRADED,
            health=HealthStatus.UNHEALTHY,
            created_at=now,
            updated_at=now,
        )
        asyncio.run(
            runtime.inventory_repository.reconcile_agent_snapshot(
                agent,
                (degraded,),
                observed_at=now,
            )
        )
        asyncio.run(reconcile_operational_alerts(runtime, generated_at=datetime.now(UTC)))

        response = client.get("/api/v1/operational/alerts", headers=headers)
        assert response.status_code == 200, response.text
        alerts = response.json()["items"]
        selected = next(item for item in alerts if item["type"] == AlertType.BENCH_DEGRADED.value)
        read_headers = _issue_token(
            client,
            headers,
            "phase9-operational-reader",
            ApiTokenScope.BENCHES_READ,
            ApiTokenScope.OPERATIONS_READ,
        )
        assert client.get("/api/v1/operational/alerts", headers=read_headers).status_code == 200
        assert (
            client.post(
                f"/api/v1/operational/alerts/{selected['id']}/acknowledge",
                headers=read_headers,
            ).status_code
            == 403
        )
        acknowledged = client.post(
            f"/api/v1/operational/alerts/{selected['id']}/acknowledge",
            headers=headers,
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["status"] == AlertStatus.ACKNOWLEDGED.value

        resolved = client.post(
            f"/api/v1/operational/alerts/{selected['id']}/resolve",
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == AlertStatus.RESOLVED.value

        orphan = create_alert(
            AlertType.AGENT_OFFLINE,
            resource_type="agent",
            resource_id=str(uuid4()),
            message="Orphaned Agent alert",
        )
        asyncio.run(
            runtime.operational_state.create_or_get_alert(
                orphan,
                organisation_id=agent.organisation_id,
            )
        )
        orphaned = client.post(
            f"/api/v1/operational/alerts/{orphan.id}/acknowledge",
            headers=headers,
        )
        assert orphaned.status_code == 404, orphaned.text


def test_operational_analytics_requires_bench_and_operation_read_scopes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    with TestClient(create_app(runtime)) as client:
        admin_headers = _bootstrap_admin(client)
        bench_headers = _issue_token(
            client,
            admin_headers,
            "phase9-bench-reader",
            ApiTokenScope.BENCHES_READ,
        )
        operation_headers = _issue_token(
            client,
            admin_headers,
            "phase9-operation-reader",
            ApiTokenScope.OPERATIONS_READ,
        )
        complete_headers = _issue_token(
            client,
            admin_headers,
            "phase9-complete-reader",
            ApiTokenScope.BENCHES_READ,
            ApiTokenScope.OPERATIONS_READ,
        )

        assert client.get("/api/v1/operational/analytics").status_code == 401
        assert client.get("/api/v1/operational/analytics", headers=bench_headers).status_code == 403
        assert (
            client.get(
                "/api/v1/operational/analytics",
                headers=operation_headers,
            ).status_code
            == 403
        )
        accepted = client.get(
            "/api/v1/operational/analytics?window_hours=1",
            headers=complete_headers,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["queue"]["queue_depth"] == 0
