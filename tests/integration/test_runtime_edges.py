from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from lab_platform.agent.runtime import create_agent
from lab_platform.core import BenchOfflineError, ConfigurationError
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    BenchOperationLock,
    HealthStatus,
    ReservationStatus,
    TargetHealth,
    TargetHealthStatus,
    WorkflowRunStatus,
)
from lab_platform.simlab_adapter import SimLabBackend


def _write_config(
    root: Path,
    *,
    workflow_directory: str = "./workflows",
    workflow_paths: tuple[str, ...] = (),
    scheduler_interval: float = 60,
) -> None:
    paths = (
        "  definition_paths:\n" + "".join(f"    - {path}\n" for path in workflow_paths)
        if workflow_paths
        else "  definition_paths: []\n"
    )
    (root / "agent.yaml").write_text(
        "agent:\n"
        "  name: runtime-edge-agent\n"
        "  log_level: ERROR\n"
        "plugins: []\n"
        "simlab:\n"
        "  benches: 1\n"
        "  speed_multiplier: 100\n"
        "scheduler:\n"
        f"  poll_interval_seconds: {scheduler_interval}\n"
        "workflows:\n"
        f"  definitions_directory: {workflow_directory}\n"
        f"{paths}",
        encoding="utf-8",
    )


def test_workflow_file_discovery_deduplicates_and_validates_paths(tmp_path: Path) -> None:
    definitions = tmp_path / "definitions"
    explicit = tmp_path / "explicit"
    nested = definitions / "nested"
    definitions.mkdir()
    explicit.mkdir()
    nested.mkdir()
    first = definitions / "a.yaml"
    second = definitions / "b.yml"
    third = explicit / "c.yaml"
    for path in (first, second, third, nested / "ignored.yaml"):
        path.write_text("placeholder", encoding="utf-8")
    (definitions / "ignored.txt").write_text("placeholder", encoding="utf-8")
    _write_config(
        tmp_path,
        workflow_directory="./definitions",
        workflow_paths=("./definitions/a.yaml", "./explicit"),
    )
    agent = create_agent(tmp_path)

    assert agent._workflow_definition_files() == tuple(sorted((first, second, third), key=str))

    not_directory = tmp_path / "not-a-directory"
    not_directory.write_text("text", encoding="utf-8")
    agent._workflow_directory = not_directory
    with pytest.raises(ConfigurationError, match="must be a directory"):
        agent._workflow_definition_files()

    agent._workflow_directory = tmp_path / "optional-missing-directory"
    agent._workflow_paths = (tmp_path / "missing.yaml",)
    with pytest.raises(ConfigurationError, match="does not exist"):
        agent._workflow_definition_files()

    invalid_extension = tmp_path / "workflow.json"
    invalid_extension.write_text("{}", encoding="utf-8")
    agent._workflow_paths = (invalid_extension,)
    with pytest.raises(ConfigurationError, match="must be YAML"):
        agent._workflow_definition_files()


def test_malformed_workflow_causes_full_startup_cleanup(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "malformed.yaml").write_text("steps: [", encoding="utf-8")
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        with pytest.raises(WorkflowInvalidError):
            await agent.start()
        assert not agent.started
        agent_health = next(
            report for report in agent.health_reports() if report.component == "agent"
        )
        assert agent_health.status is HealthStatus.UNHEALTHY
        assert agent._background_tasks == set()
        with pytest.raises(RuntimeError, match="Database is not initialized"):
            await agent._catalog_repository.list_backends()
        await agent.shutdown()

    asyncio.run(scenario())


def test_background_workers_are_idempotent_and_record_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, scheduler_interval=0.01)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            assert len(agent._background_tasks) == 2
            await agent.start_background_workers()
            assert len(agent._background_tasks) == 2

            async def fail_refresh() -> object:
                raise RuntimeError("inventory refresh exploded")

            monkeypatch.setattr(agent.catalog, "refresh", fail_refresh)
            scheduler_events = []
            for _ in range(100):
                scheduler_events = await agent.event_service.list_events(
                    event_type="SCHEDULER_FAILURE"
                )
                if scheduler_events:
                    break
                await asyncio.sleep(0.005)
            assert len(scheduler_events) == 1
            assert scheduler_events[0].payload == {"error": "inventory refresh exploded"}

            await agent.stop_background_workers()
            await agent.stop_background_workers()
            assert agent._background_tasks == set()
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_blocked_health_probe_does_not_delay_reservation_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, scheduler_interval=0.01)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_probe() -> None:
            entered.set()
            await release.wait()

        try:
            await agent.stop_background_workers()
            monkeypatch.setattr(agent, "_probe_idle_benches", blocked_probe)
            agent._health_probe_poll_seconds = 0.001
            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            now = agent._clock.now()
            with agent._database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE reservations SET starts_at = ?, ends_at = ? WHERE id = ?",
                    (
                        (now - timedelta(seconds=60)).isoformat(),
                        (now - timedelta(seconds=1)).isoformat(),
                        str(reservation.id),
                    ),
                )

            await agent.start_background_workers()
            await asyncio.wait_for(entered.wait(), timeout=1)
            for _ in range(100):
                current = await agent.reservation_service.get(reservation.id)
                if current.status is ReservationStatus.EXPIRED:
                    break
                await asyncio.sleep(0.005)
            assert current.status is ReservationStatus.EXPIRED
            assert not release.is_set()
        finally:
            release.set()
            await agent.shutdown()

    asyncio.run(scenario())


def test_periodic_health_probe_only_runs_for_idle_unlocked_benches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            backend = agent.backend_registry.get_backend("simlab")
            assert isinstance(backend, SimLabBackend)
            original_probe = backend.probe
            calls = 0

            async def tracking_probe(bench_id: str) -> object:
                nonlocal calls
                calls += 1
                return await original_probe(bench_id)

            monkeypatch.setattr(backend, "probe", tracking_probe)
            old = agent._clock.now() - timedelta(seconds=31)
            agent._last_health_probe_at["bench-01"] = old
            await agent._probe_idle_benches()
            assert calls == 1
            assert await agent._operation_locks.list() == []

            await agent._probe_idle_benches()
            assert calls == 1

            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            agent._last_health_probe_at["bench-01"] = old
            await agent._probe_idle_benches()
            assert calls == 1
            await agent.reservation_service.release(reservation.id, "alice")

            competing = BenchOperationLock(
                bench_id="bench-01",
                operation_id=uuid4(),
                acquired_at=agent._clock.now(),
            )
            await agent._operation_locks.acquire(competing)
            agent._last_health_probe_at["bench-01"] = old
            await agent._probe_idle_benches()
            assert calls == 1
            await agent._operation_locks.release("bench-01", competing.operation_id)
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_offline_probe_gates_reservations_before_releasing_maintenance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            backend = agent.backend_registry.get_backend("simlab")
            assert isinstance(backend, SimLabBackend)

            async def offline_probe(bench_id: str) -> TargetHealth:
                return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.OFFLINE)

            monkeypatch.setattr(backend, "probe", offline_probe)
            agent._last_health_probe_at["bench-01"] = agent._clock.now() - timedelta(seconds=31)

            await agent._probe_idle_benches()

            record = agent.catalog.get("bench-01")
            assert not record.online
            assert record.health is HealthStatus.UNHEALTHY
            assert agent.catalog.requires_probe("bench-01")
            persisted = await agent._catalog_repository.get_record("bench-01")
            assert persisted is not None and not persisted.online
            assert await agent._operation_locks.list() == []
            with pytest.raises(BenchOfflineError):
                await agent.reservation_service.reserve("bench-01", "alice")
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_manual_probe_reconciles_offline_health_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            backend = agent.backend_registry.get_backend("simlab")
            assert isinstance(backend, SimLabBackend)

            async def offline_probe(bench_id: str) -> TargetHealth:
                return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.OFFLINE)

            monkeypatch.setattr(backend, "probe", offline_probe)
            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            health = await agent.bench_service.probe("bench-01", "alice")

            assert health.status is TargetHealthStatus.OFFLINE
            assert not agent.catalog.is_online("bench-01")
            assert agent.catalog.requires_probe("bench-01")
            assert await agent._operation_locks.list() == []
            await agent.reservation_service.release(reservation.id, "alice")
            with pytest.raises(BenchOfflineError):
                await agent.reservation_service.reserve("bench-01", "bob")
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_workflow_probe_reconciles_offline_health_before_releasing_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "offline-probe.yaml").write_text(
        """name: offline-probe
version: 1
requirements: {capabilities: [probe]}
steps: [{action: probe}]
""",
        encoding="utf-8",
    )
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            backend = agent.backend_registry.get_backend("simlab")
            assert isinstance(backend, SimLabBackend)

            async def offline_probe(bench_id: str) -> TargetHealth:
                return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.OFFLINE)

            monkeypatch.setattr(backend, "probe", offline_probe)
            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            run = await agent.workflow_service.start(
                "offline-probe",
                bench_id="bench-01",
                owner="alice",
            )
            completed = await agent.workflow_service.wait(run.id)

            assert completed.status is WorkflowRunStatus.FAILED
            assert not agent.catalog.is_online("bench-01")
            assert agent.catalog.requires_probe("bench-01")
            assert await agent._operation_locks.list() == []
            await agent.reservation_service.release(reservation.id, "alice")
            with pytest.raises(BenchOfflineError):
                await agent.reservation_service.reserve("bench-01", "bob")
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_expiry_grace_overrun_cancels_workflow_and_requires_probe(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "overrun.yaml").write_text(
        """name: overrun
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 120}]
""",
        encoding="utf-8",
    )
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            run = await agent.workflow_service.start("overrun", bench_id="bench-01", owner="alice")
            for _ in range(20):
                if (await agent.workflow_service.get_run(run.id)).status is (
                    WorkflowRunStatus.RUNNING
                ):
                    break
                await asyncio.sleep(0)

            now = agent._clock.now()
            with agent._database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE reservations SET status = 'expired_pending_operation', "
                    "starts_at = ?, ends_at = ?, expired_at = ?, release_pending = 1 "
                    "WHERE id = ?",
                    (
                        (now - timedelta(seconds=60)).isoformat(),
                        (now - timedelta(seconds=31)).isoformat(),
                        now.isoformat(),
                        str(reservation.id),
                    ),
                )

            await agent._handle_expiry_overruns()
            completed = await agent.workflow_service.wait(run.id)
            assert completed.status is WorkflowRunStatus.CANCELLED
            assert agent.catalog.requires_probe("bench-01")
            assert not agent.catalog.is_online("bench-01")
            assert agent.health_payload()["benches"] == {"total": 1, "online": 0}
            with pytest.raises(BenchOfflineError):
                await agent.reservation_service.create("bench-01", "bob", duration_seconds=60)

            events = await agent.event_service.list_events(
                bench_id="bench-01", event_type="OPERATION_GRACE_EXCEEDED"
            )
            assert len(events) == 1
            assert events[0].payload["cancellation_requested"] is True
            assert await agent._operation_locks.list() == []
            expired = await agent.reservation_service.get(reservation.id)
            assert expired.status is ReservationStatus.EXPIRED
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_release_after_workflow_is_deduplicated_and_handles_missing_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "wait.yaml").write_text(
        """name: release-edge
version: 1
requirements: {capabilities: []}
steps: [{action: wait, seconds: 0.02}]
""",
        encoding="utf-8",
    )
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            reservation = await agent.reservation_service.reserve("bench-01", "alice")
            run = await agent.workflow_service.start(
                "release-edge", bench_id="bench-01", owner="alice"
            )
            agent.release_reservation_after_workflow(run.id, reservation.id, "alice")
            agent.release_reservation_after_workflow(run.id, reservation.id, "alice")
            assert len(agent._release_tasks) == 1
            assert (await agent.workflow_service.wait(run.id)).status is WorkflowRunStatus.SUCCEEDED
            await agent._drain_release_tasks()
            assert (await agent.reservation_service.get(reservation.id)).status is (
                ReservationStatus.RELEASED
            )

            caplog.set_level(logging.ERROR, logger="runtime-edge-agent")
            agent.release_reservation_after_workflow(uuid4(), reservation.id, "alice")
            await agent._drain_release_tasks()
            assert "Could not release reservation after workflow" in caplog.text
            assert agent._release_tasks == {}
        finally:
            await agent.shutdown()

    asyncio.run(scenario())
