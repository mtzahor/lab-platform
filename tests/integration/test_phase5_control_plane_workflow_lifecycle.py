from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationSource,
    ReservationStatus,
)


def _config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "port": 8443,
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "agent_gateway": {"monitor_interval_seconds": 3_600},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "development": {"allow_insecure_agent_transport": True},
        }
    )


async def _seed_terminal_workflow(
    runtime: ControlPlaneRuntime,
    *,
    number: int,
    lifecycle: str,
    command_terminal: bool,
) -> CoordinatedReservationLease:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id=UUID(int=number),
        slug=f"agent-{number}",
        name=f"Agent {number}",
        status=AgentStatus.ONLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=now - timedelta(days=1),
        last_connected_at=now,
        last_seen_at=now,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )
    bench = GlobalBenchRecord(
        id=f"{agent.slug}/bench-{number}",
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=f"bench-{number}",
        name=f"Bench {number}",
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"reset"}),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    with runtime.database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, location, labels_json, "
            "registered_at, last_connected_at, last_seen_at, disconnected_at, "
            "certificate_fingerprint, enrollment_status, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?, NULL, NULL, ?, NULL)",
            (
                str(agent.id),
                agent.slug,
                agent.name,
                agent.status.value,
                agent.version,
                agent.protocol_version,
                agent.registered_at.isoformat(),
                now.isoformat(),
                now.isoformat(),
                agent.enrollment_status.value,
            ),
        )
        connection.execute(
            "INSERT INTO global_benches "
            "(id, agent_id, agent_slug, local_bench_id, name, backend_id, kind, target_type, "
            "status, health, capabilities_json, labels_json, firmware_version, last_seen_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, '{}', NULL, "
            "?, ?, ?)",
            (
                bench.id,
                str(agent.id),
                agent.slug,
                bench.local_bench_id,
                bench.name,
                bench.backend_id,
                bench.kind.value,
                bench.status.value,
                bench.health.value,
                json.dumps(sorted(bench.capabilities)),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )

    reservation = Reservation(
        id=UUID(int=10_000 + number),
        bench_id=bench.id,
        owner=f"owner-{number}",
        created_at=now,
        requested_at=now,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        status=ReservationStatus.SCHEDULED,
        source=ReservationSource.API,
        metadata={"workload": "workflow", "reservation_lifecycle": lifecycle},
        idempotency_key=f"reservation-{number}",
    )
    pending = await runtime.reservation_repository.grant_if_eligible(
        ReservationGrantRequest(
            reservation=reservation,
            agent_id=agent.id,
            lease_valid_until=now + timedelta(minutes=30),
        ),
        mutation_key=f"grant-{number}",
        request_fingerprint=f"{number:x}".rjust(64, "0"),
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
        mutation_key=f"activate-{number}",
        request_fingerprint=f"{number + 100:x}".rjust(64, "0"),
    )
    assert persisted is not None

    operation_id = UUID(int=30_000 + number)
    command_status = (
        RemoteCommandStatus.SUCCEEDED if command_terminal else RemoteCommandStatus.DISPATCHED
    )
    operation_status = (
        DistributedOperationStatus.DISPATCHED
        if command_terminal
        else DistributedOperationStatus.SUCCEEDED
    )
    completed_at = now + timedelta(seconds=4)
    command = RemoteCommand(
        id=UUID(int=20_000 + number),
        agent_id=agent.id,
        bench_id=bench.id,
        command_type=RemoteCommandType.RUN_WORKFLOW,
        payload={},
        status=command_status,
        created_at=now,
        dispatched_at=now + timedelta(seconds=1),
        completed_at=completed_at if command_terminal else None,
        expires_at=now + timedelta(hours=1),
        idempotency_key=f"command-{number}",
        operation_id=operation_id,
        reservation_id=reservation.id,
        lease_version=active.lease.lease_version,
    )
    operation = DistributedOperation(
        id=operation_id,
        remote_command_id=command.id,
        agent_id=agent.id,
        bench_id=bench.id,
        reservation_id=reservation.id,
        operation_type="RUN_WORKFLOW",
        status=operation_status,
        created_at=now,
        dispatched_at=now + timedelta(seconds=1),
        completed_at=completed_at if not command_terminal else None,
    )
    await runtime.command_repository.create_bundle(command, operation)
    return active


def test_runtime_monitor_releases_persisted_workflow_after_restart_only_when_managed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        initial = ControlPlaneRuntime(config)
        initial.database.initialize()
        managed = await _seed_terminal_workflow(
            initial,
            number=1,
            lifecycle="workflow",
            command_terminal=False,
        )
        caller_managed = await _seed_terminal_workflow(
            initial,
            number=2,
            lifecycle="caller",
            command_terminal=True,
        )
        initial.database.close()

        restarted = ControlPlaneRuntime(config)
        restarted.database.initialize()
        await restarted.monitor_once()
        released = await restarted.reservations.get(managed.reservation.id)
        retained = await restarted.reservations.get(caller_managed.reservation.id)
        assert released.state is ReservationLeaseState.RELEASED
        assert released.lease.lease_version == managed.lease.lease_version
        assert retained.state is ReservationLeaseState.ACTIVE

        await restarted.monitor_once()
        with restarted.database.transaction() as connection:
            cleanup_mutations = connection.execute(
                "SELECT COUNT(*) AS count FROM reservation_lease_mutations "
                "WHERE mutation_key LIKE 'workflow-terminal-release:%'"
            ).fetchone()
        assert cleanup_mutations is not None
        assert cleanup_mutations["count"] == 1
        restarted.database.close()

    asyncio.run(scenario())
