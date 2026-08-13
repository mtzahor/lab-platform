from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.control_plane.runtime import _sqlite_path
from lab_platform.control_plane_core.artifacts import IssuedArtifactTransfer
from lab_platform.control_plane_core.reconciliation import ReconciliationResult
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.core.errors import ConfigurationError, PermissionDeniedError
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    ArtifactTransferStatus,
    AuthenticationContext,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    Principal,
    PrincipalType,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ResourceType,
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


def _reconciliation_result(agent_id: UUID) -> ReconciliationResult:
    return ReconciliationResult(
        report_id=uuid4(),
        report_digest="b" * 64,
        agent_id=agent_id,
        boot_id=uuid4(),
        restarted=False,
        reconciled_command_ids=frozenset(),
        reconciled_operation_ids=frozenset(),
        interrupted_operation_ids=frozenset(),
        expired_reservation_ids=frozenset(),
        stale_local_leases=frozenset(),
        inventory_reconciled=True,
    )


def test_runtime_start_failure_closes_database_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        recover = AsyncMock(side_effect=[RuntimeError("CI recovery failed"), 0])
        monkeypatch.setattr(runtime.ci, "recover_incomplete", recover)

        def is_started() -> bool:
            return runtime.started

        with pytest.raises(RuntimeError, match="CI recovery failed"):
            await runtime.start()
        assert not is_started()
        with (
            pytest.raises(RuntimeError, match="not initialized"),
            runtime.database.transaction(),
        ):
            pass

        await runtime.start()
        assert is_started()
        assert recover.await_count == 2
        await runtime.stop()

    asyncio.run(scenario())


def test_runtime_reissues_unrecoverable_artifact_capabilities_and_reuses_live_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            now = datetime.now(UTC)
            token = await runtime.enrollment.issue_token(
                name="artifact-agent",
                expires_at=now + timedelta(minutes=10),
                allow_internal_authorisation=True,
            )
            enrolled = await runtime.enrollment.enroll(
                plaintext_token=token.plaintext.get_secret_value(),
                request_id=uuid4(),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )
            agent = enrolled.agent
            bench = GlobalBenchRecord(
                id=f"{agent.slug}/bench-01",
                agent_id=agent.id,
                agent_slug=agent.slug,
                local_bench_id="bench-01",
                name="Artifact bench",
                backend_id="simlab",
                kind=GlobalBenchKind.SIMULATED,
                status=GlobalBenchStatus.ONLINE,
                health=HealthStatus.HEALTHY,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
            await runtime.inventory_repository.reconcile_agent_snapshot(
                agent,
                (bench,),
                observed_at=now,
            )
            command = RemoteCommand(
                agent_id=agent.id,
                bench_id=bench.id,
                command_type=RemoteCommandType.RUN_WORKFLOW,
                created_at=now,
                expires_at=now + timedelta(minutes=10),
                idempotency_key="artifact-command",
            )
            await runtime.command_repository.create_bundle(command, None)
            content = b"distributed-test-results"
            artifact = await runtime.artifacts.register_remote_artifact(
                RemoteArtifactMetadata(
                    agent_id=agent.id,
                    local_artifact_id=uuid4(),
                    command_id=command.id,
                    name="results.json",
                    artifact_type="test-results",
                    content_type="application/json",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    created_at=now,
                )
            )
            send = AsyncMock()
            monkeypatch.setattr(runtime.hub, "send", send)

            first = cast(
                IssuedArtifactTransfer,
                await runtime.request_artifact_upload(agent.id, artifact.id),
            )
            replay = cast(
                IssuedArtifactTransfer,
                await runtime.request_artifact_upload(agent.id, artifact.id),
            )
            assert replay.transfer.id == first.transfer.id
            assert replay.plaintext_token.get_secret_value() == (
                first.plaintext_token.get_secret_value()
            )
            assert send.await_count == 1
            sent_payload = send.await_args_list[0].args[2]
            assert sent_payload.upload_url.endswith(
                f"/api/v1/artifact-transfers/{first.transfer.id}/content"
            )
            assert sent_payload.local_artifact_id == artifact.local_artifact_id

            runtime._artifact_upload_capabilities.clear()
            replacement = cast(
                IssuedArtifactTransfer,
                await runtime.request_artifact_upload(agent.id, artifact.id),
            )
            assert replacement.transfer.id != first.transfer.id
            transfers = await runtime.artifact_transfers.list(agent_id=agent.id, limit=10)
            assert {item.status for item in transfers} == {
                ArtifactTransferStatus.EXPIRED,
                ArtifactTransferStatus.PENDING,
            }
            expired = next(
                item for item in transfers if item.status is ArtifactTransferStatus.EXPIRED
            )
            assert expired.error_code == "TRANSFER_SUPERSEDED"

            uploaded = await runtime.remote_artifacts.update_uploaded(
                artifact.id,
                now + timedelta(seconds=1),
            )
            assert uploaded is not None
            assert await runtime.request_artifact_upload(agent.id, artifact.id) == uploaded
            assert send.await_count == 2

            with pytest.raises(ValueError, match="does not belong"):
                await runtime.request_artifact_upload(UUID(int=999), artifact.id)
            with pytest.raises(ValueError, match="does not belong"):
                await runtime.request_artifact_upload(agent.id, uuid4())
        finally:
            await runtime.stop()
            await runtime.stop()
        assert runtime.started is False

    asyncio.run(scenario())


def _remote_command(
    agent_id: UUID,
    number: int,
    *,
    reservation_id: UUID | None = None,
    lease_version: int | None = None,
) -> RemoteCommand:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    return RemoteCommand(
        id=UUID(int=number),
        agent_id=agent_id,
        bench_id="replay-agent/bench-01",
        command_type=RemoteCommandType.RESET,
        status=RemoteCommandStatus.UNKNOWN,
        created_at=now,
        dispatched_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        idempotency_key=f"command-{number}",
        reservation_id=reservation_id,
        lease_version=lease_version,
    )


def _active_reservation(
    agent_id: UUID,
    reservation_id: UUID,
    lease_version: int,
) -> CoordinatedReservationLease:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    reservation = Reservation(
        id=reservation_id,
        bench_id="replay-agent/bench-01",
        owner="ci/build",
        created_at=now,
        ends_at=now + timedelta(minutes=5),
        idempotency_key=f"reservation-{reservation_id}",
    )
    lease = ReservationLease(
        reservation_id=reservation_id,
        agent_id=agent_id,
        bench_id=reservation.bench_id,
        owner=reservation.owner,
        valid_from=now,
        valid_until=now + timedelta(minutes=5),
        lease_version=lease_version,
    )
    return CoordinatedReservationLease(
        reservation=reservation,
        lease=lease,
        state=ReservationLeaseState.ACTIVE,
        revision=1,
    )


def test_runtime_replay_only_dispatches_safe_commands_and_records_deferrals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        agent_id = UUID(int=500)
        matching_reservation = UUID(int=501)
        stale_reservation = UUID(int=502)
        unreserved = _remote_command(agent_id, 510)
        failing = _remote_command(agent_id, 511)
        matching = _remote_command(
            agent_id,
            512,
            reservation_id=matching_reservation,
            lease_version=7,
        )
        stale = _remote_command(
            agent_id,
            513,
            reservation_id=stale_reservation,
            lease_version=2,
        )
        list_commands = AsyncMock(return_value=[unreserved, failing, matching, stale])
        monkeypatch.setattr(runtime.command_repository, "list_commands", list_commands)

        active = _active_reservation(agent_id, matching_reservation, 7)

        async def lookup(reservation_id: UUID) -> CoordinatedReservationLease:
            if reservation_id == matching_reservation:
                return active
            return _active_reservation(agent_id, stale_reservation, 99)

        monkeypatch.setattr(runtime.reservations, "get", lookup)

        dispatch = AsyncMock()

        async def dispatch_side_effect(command_id: UUID, **_kwargs: object) -> object:
            if command_id == failing.id:
                raise ConnectionError("offline again")
            return command_id

        dispatch.side_effect = dispatch_side_effect
        monkeypatch.setattr(runtime.commands, "dispatch", dispatch)
        timeline = AsyncMock()
        monkeypatch.setattr(runtime, "record_timeline", timeline)

        await runtime._replay_after_reconciliation(_reconciliation_result(agent_id))

        list_commands.assert_awaited_once_with(
            agent_id=agent_id,
            statuses={RemoteCommandStatus.UNKNOWN, RemoteCommandStatus.QUEUED},
            limit=10_000,
        )
        assert [entry.args[0] for entry in dispatch.await_args_list] == [
            unreserved.id,
            failing.id,
            matching.id,
        ]
        assert dispatch.await_args_list[0].kwargs == {"reservation_lease": None}
        assert dispatch.await_args_list[2].kwargs == {"reservation_lease": active.lease}
        timeline.assert_awaited_once()
        timeline_call = timeline.await_args
        assert timeline_call is not None
        assert timeline_call.args[1:3] == (
            "REMOTE_COMMAND_REPLAY_DEFERRED",
            "A reconciled command could not yet be replayed safely.",
        )
        assert timeline_call.kwargs["correlation_id"] == failing.id
        assert timeline_call.kwargs["metadata"] == {"error_type": "ConnectionError"}

    asyncio.run(scenario())


def test_runtime_monitor_drives_timeout_cleanup_and_suppresses_drain_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        agent_id = UUID(int=600)
        connection_id = UUID(int=601)
        transition = SimpleNamespace(
            agent=SimpleNamespace(id=agent_id, disconnected_at=datetime.now(UTC)),
            connection=SimpleNamespace(id=connection_id),
        )
        check_timeouts = AsyncMock(return_value=[transition])
        list_agents = AsyncMock(
            return_value=[SimpleNamespace(id=UUID(int=602)), SimpleNamespace(id=UUID(int=603))]
        )
        mark_unknown = AsyncMock(return_value=1)
        mark_disconnected = AsyncMock()
        command_expiry = AsyncMock()
        reservation_expiry = AsyncMock()
        reconciliation_timeout = AsyncMock()
        workflow_cleanup = AsyncMock()
        ci_maintenance = AsyncMock()
        refresh = AsyncMock(side_effect=[None, RuntimeError("refresh race")])
        monkeypatch.setattr(runtime.presence, "check_timeouts", check_timeouts)
        monkeypatch.setattr(runtime.presence, "list_agents", list_agents)
        monkeypatch.setattr(runtime.commands, "mark_unknown", mark_unknown)
        monkeypatch.setattr(runtime.commands, "expire_due", command_expiry)
        monkeypatch.setattr(
            runtime.reservations,
            "mark_agent_disconnected",
            mark_disconnected,
        )
        monkeypatch.setattr(runtime.reservations, "expire_due", reservation_expiry)
        monkeypatch.setattr(
            runtime.reconciliation_service,
            "timeout_unreconciled_operations",
            reconciliation_timeout,
        )
        monkeypatch.setattr(runtime.workflow_reservations, "release_terminal", workflow_cleanup)
        monkeypatch.setattr(runtime.ci, "process_maintenance", ci_maintenance)
        monkeypatch.setattr(runtime.drain, "refresh", refresh)

        await runtime.monitor_once()

        check_timeouts.assert_awaited_once()
        timeout_call = check_timeouts.await_args
        assert timeout_call is not None
        mark_unknown.assert_awaited_once_with(
            agent_id,
            observed_at=timeout_call.kwargs["observed_at"],
        )
        mark_disconnected.assert_awaited_once_with(
            agent_id,
            disconnect_id=connection_id,
        )
        command_expiry.assert_awaited_once_with()
        reservation_expiry.assert_awaited_once_with()
        reconciliation_timeout.assert_awaited_once_with()
        workflow_cleanup.assert_awaited_once_with()
        ci_maintenance.assert_awaited_once_with()
        list_agents.assert_awaited_once_with(status=AgentStatus.DRAINING)
        assert refresh.await_args_list == [
            call(UUID(int=602), allow_internal_authorisation=True),
            call(UUID(int=603), allow_internal_authorisation=True),
        ]

    asyncio.run(scenario())


def test_sqlite_path_validation_and_disconnected_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _sqlite_path("sqlite:///relative/control-plane.db") == Path("relative/control-plane.db")
    with pytest.raises(ConfigurationError, match="requires a sqlite:///"):
        _sqlite_path("postgresql://localhost/lab")
    with pytest.raises(ConfigurationError, match="include a path"):
        _sqlite_path("sqlite:///")

    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        get_agent = AsyncMock()
        monkeypatch.setattr(runtime.presence, "active_connection", AsyncMock(return_value=None))
        monkeypatch.setattr(runtime.presence, "get_agent", get_agent)
        with pytest.raises(RuntimeError, match="not connected"):
            await runtime.request_reconciliation(
                UUID(int=700),
                allow_legacy_authorisation=True,
            )
        get_agent.assert_awaited_once_with(UUID(int=700))

    asyncio.run(scenario())


def test_phase6_runtime_agent_wrappers_authorise_exact_tenant_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        organisation_id = UUID(int=90_001)
        agent = AgentRecord(
            id=UUID(int=90_002),
            organisation_id=organisation_id,
            slug="phase6-agent",
            name="Phase 6 Agent",
            status=AgentStatus.ONLINE,
            version="0.6.0-alpha",
            protocol_version="1.0",
            registered_at=datetime.now(UTC) - timedelta(days=1),
            enrollment_status=EnrollmentStatus.ENROLLED,
        )
        context = AuthenticationContext(
            principal=Principal(
                id=UUID(int=90_003),
                type=PrincipalType.USER,
                organisation_id=organisation_id,
                display_name="Denied administrator",
            )
        )
        get_agent = AsyncMock(return_value=agent)
        evaluate = AsyncMock(
            return_value=SimpleNamespace(allowed=False, granting_assignment_ids=frozenset())
        )
        require = AsyncMock(side_effect=PermissionDeniedError("denied"))
        active_connection = AsyncMock()
        hub_send = AsyncMock()
        hub_close = AsyncMock()
        drain = AsyncMock()
        undrain = AsyncMock()
        revoke = AsyncMock()
        timeline = AsyncMock()
        monkeypatch.setattr(runtime.presence, "get_agent", get_agent)
        monkeypatch.setattr(runtime.presence, "active_connection", active_connection)
        monkeypatch.setattr(runtime.authorisation, "evaluate", evaluate)
        monkeypatch.setattr(runtime.authorisation, "require", require)
        monkeypatch.setattr(runtime.hub, "send", hub_send)
        monkeypatch.setattr(runtime.hub, "close_agent", hub_close)
        monkeypatch.setattr(runtime.drain, "drain", drain)
        monkeypatch.setattr(runtime.drain, "undrain", undrain)
        monkeypatch.setattr(runtime.enrollment, "revoke_agent", revoke)
        monkeypatch.setattr(runtime, "record_timeline", timeline)

        with pytest.raises(PermissionDeniedError):
            await runtime.refresh_inventory(
                agent.id,
                authentication_context=context,
            )
        with pytest.raises(PermissionDeniedError):
            await runtime.request_reconciliation(
                agent.id,
                authentication_context=context,
            )
        with pytest.raises(PermissionDeniedError):
            await runtime.drain_agent(
                agent.id,
                authentication_context=context,
            )
        with pytest.raises(PermissionDeniedError):
            await runtime.undrain_agent(
                agent.id,
                authentication_context=context,
            )
        with pytest.raises(PermissionDeniedError):
            await runtime.revoke_agent(
                agent.id,
                authentication_context=context,
            )

        assert (
            get_agent.await_args_list
            == [
                call(agent.id, organisation_id=organisation_id),
            ]
            * 5
        )
        assert [item.args[1] for item in require.await_args_list] == [
            "agents:manage",
            "agents:manage",
            "agents:drain",
            "agents:drain",
            "agents:manage",
        ]
        assert all(
            item.args[2].type is ResourceType.AGENT
            and item.args[2].id == str(agent.id)
            and item.args[2].organisation_id == organisation_id
            for item in require.await_args_list
        )
        active_connection.assert_not_awaited()
        hub_send.assert_not_awaited()
        hub_close.assert_not_awaited()
        drain.assert_not_awaited()
        undrain.assert_not_awaited()
        revoke.assert_not_awaited()
        timeline.assert_not_awaited()

    asyncio.run(scenario())
