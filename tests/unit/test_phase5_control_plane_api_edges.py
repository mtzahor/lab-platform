from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime, create_app
from lab_platform.control_plane import api as control_plane_api
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import (
    CapabilityNotSupportedError,
    ReservationNotActiveError,
    ReservationOwnerMismatchError,
)
from lab_platform.models import (
    AgentRecord,
    ApiTokenScope,
    DistributedOperation,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    Reservation,
    ReservationLease,
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


def _bootstrap(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/tokens",
        json={
            "name": "action-admin",
            "owner": "alice",
            "scopes": [scope.value for scope in ApiTokenScope],
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def _seed_bench(runtime: ControlPlaneRuntime, agent: AgentRecord) -> GlobalBenchRecord:
    now = datetime.now(UTC)
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-01",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id="bench-01",
        name="Action bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"firmware", "probe", "reset", "serial"}),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    await runtime.inventory_repository.reconcile_agent_snapshot(
        agent,
        (bench,),
        observed_at=now,
    )
    return bench


def _reservation(agent_id: UUID, bench_id: str) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    reservation = Reservation(
        id=uuid4(),
        bench_id=bench_id,
        owner="alice",
        created_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        idempotency_key="action-reservation",
    )
    return CoordinatedReservationLease(
        reservation=reservation,
        lease=ReservationLease(
            reservation_id=reservation.id,
            agent_id=agent_id,
            bench_id=bench_id,
            owner="alice",
            valid_from=now,
            valid_until=now + timedelta(hours=1),
            lease_version=1,
        ),
        state=ReservationLeaseState.ACTIVE,
        revision=1,
    )


def test_direct_bench_action_routes_stage_only_durable_artifact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    captured: list[dict[str, Any]] = []

    with TestClient(create_app(runtime)) as client:
        headers = _bootstrap(client)
        enrollment_token = client.post(
            "/api/v1/agents/enrollment-tokens",
            headers=headers,
            json={"name": "action-agent", "expires_in_seconds": 300},
        ).json()["token"]
        enrolled = client.post(
            "/api/v1/agents/enroll",
            json={
                "enrollment_token": enrollment_token,
                "agent_version": "0.6.0-alpha",
                "protocol_version": "1.0",
            },
        ).json()
        assert client.portal is not None
        agent = client.portal.call(runtime.presence.get_agent, UUID(enrolled["agent"]["id"]))
        bench = client.portal.call(_seed_bench, runtime, agent)
        reservation = _reservation(agent.id, bench.id)

        async def create_command(**kwargs: Any) -> tuple[RemoteCommand, DistributedOperation]:
            captured.append(kwargs)
            lease = kwargs.get("reservation_lease")
            command = RemoteCommand(
                agent_id=kwargs["agent_id"],
                bench_id=kwargs["bench_id"],
                command_type=kwargs["command_type"],
                payload=dict(kwargs["payload"]),
                expires_at=kwargs["expires_at"],
                idempotency_key=kwargs["idempotency_key"],
                reservation_id=lease.reservation_id if lease is not None else None,
                lease_version=lease.lease_version if lease is not None else None,
            )
            operation = DistributedOperation(
                remote_command_id=command.id,
                agent_id=command.agent_id,
                bench_id=command.bench_id,
                reservation_id=command.reservation_id,
                operation_type=str(kwargs["operation_type"]),
            )
            return command.model_copy(update={"operation_id": operation.id}), operation

        monkeypatch.setattr(runtime.commands, "create", create_command)
        monkeypatch.setattr(
            control_plane_api,
            "_action_reservation",
            AsyncMock(return_value=reservation),
        )

        probe = client.post(
            f"/api/v1/benches/{bench.id}/actions/probe",
            headers={**headers, "Idempotency-Key": "probe-once"},
            json={"owner": "alice"},
        )
        assert probe.status_code == 202
        assert probe.json()["status"] == "CREATED"

        reset = client.post(
            f"/api/v1/benches/{bench.id}/actions/reset",
            headers=headers,
            json={"owner": "alice"},
        )
        assert reset.status_code == 202

        serial = client.post(
            f"/api/v1/benches/{bench.id}/actions/read-serial",
            headers=headers,
            json={
                "owner": "alice",
                "timeout_seconds": 2.5,
                "until_pattern": "READY",
                "max_lines": 12,
            },
        )
        assert serial.status_code == 202
        assert captured[-1]["payload"]["request"] == {
            "timeout_seconds": 2.5,
            "until_pattern": "READY",
            "max_lines": 12,
            "include_timestamps": True,
        }

        flashed = client.post(
            f"/api/v1/benches/{bench.id}/actions/flash",
            headers={**headers, "Idempotency-Key": "flash-once"},
            data={"owner": "alice", "version": "2.0.0"},
            files={"firmware": ("firmware.bin", b"firmware-v2", "application/octet-stream")},
        )
        assert flashed.status_code == 202, flashed.text
        assert flashed.json()["input_artifact"]["sha256"]
        durable = captured[-1]["payload"]["artifact"]
        assert set(durable) == {
            "input_name",
            "agent_id",
            "artifact_id",
            "sha256",
            "size_bytes",
            "target_path",
        }
        assert "transfer_token" not in repr(captured)
        assert "download_url" not in repr(captured)

        missing = client.post(
            "/api/v1/benches/missing/bench/actions/probe",
            headers=headers,
            json={"owner": "alice"},
        )
        assert missing.status_code == 404

        with pytest.raises(CapabilityNotSupportedError):
            client.portal.call(
                partial(
                    control_plane_api._action_bench,
                    runtime,
                    bench.id,
                    capability="power",
                )
            )


def test_direct_action_reservation_helper_fences_absent_wrong_and_inactive_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime: Any = SimpleNamespace(
        reservation_repository=SimpleNamespace(get_current_for_bench=AsyncMock(return_value=None))
    )

    async def scenario() -> None:
        with pytest.raises(ReservationNotActiveError, match="require an active"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "alice")

        active = _reservation(UUID(int=1), "agent/bench")
        runtime.reservation_repository.get_current_for_bench = AsyncMock(return_value=active)
        with pytest.raises(ReservationOwnerMismatchError, match="another owner"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "bob")

        inactive = CoordinatedReservationLease(
            reservation=active.reservation,
            lease=active.lease,
            state=ReservationLeaseState.UNKNOWN,
            revision=2,
            unknown_since=datetime.now(UTC),
            reconciliation_deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        runtime.reservation_repository.get_current_for_bench = AsyncMock(return_value=inactive)
        with pytest.raises(ReservationNotActiveError, match="lease is not active"):
            await control_plane_api._action_reservation(runtime, "agent/bench", "alice")

    import asyncio

    asyncio.run(scenario())
