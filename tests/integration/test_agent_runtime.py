from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent.api import create_app
from lab_platform.agent.cli import main as agent_main
from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.core import BenchOfflineError
from lab_platform.core.errors import ConfigurationError
from lab_platform.core.workflows import WorkflowInvalidError
from lab_platform.models import (
    BackendProgress,
    BenchOperationLock,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    HealthStatus,
    Operation,
    OperationStatus,
    OperationType,
    ReservationStatus,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
    TimelineCategory,
    WorkflowRunStatus,
)
from lab_platform.persistence import SQLiteDatabase, SQLiteOperationRepository
from lab_platform.real_backend import RealLabBackend
from lab_platform.simlab_adapter import SimLabBackend


class FakePhysicalBackend:
    def __init__(self) -> None:
        self.fail_inventory = False
        self.actions: list[tuple[str, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_benches(self) -> list[BenchSnapshot]:
        if self.fail_inventory:
            raise RuntimeError("physical inventory unavailable")
        return [await self.get_bench("fake-physical-01")]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return BenchSnapshot(
            id=bench_id,
            name="Fake physical ESP32",
            status=BenchStatus.AVAILABLE,
            online=True,
            powered=True,
            capabilities=["reset", "probe", "serial", "firmware"],
        )

    async def power_on(self, bench_id: str) -> None:
        self.actions.append(("power_on", bench_id))

    async def power_off(self, bench_id: str) -> None:
        self.actions.append(("power_off", bench_id))

    async def power_cycle(self, bench_id: str) -> None:
        self.actions.append(("power_cycle", bench_id))

    async def reset(self, bench_id: str) -> None:
        self.actions.append(("reset", bench_id))

    async def probe(self, bench_id: str) -> TargetHealth:
        return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.ONLINE)

    async def flash_firmware(
        self, bench_id: str, firmware: FirmwareInput
    ) -> AsyncIterator[BackendProgress]:
        self.actions.append(("flash", bench_id))
        yield BackendProgress(percent=100, message=firmware.filename)

    async def read_serial(
        self, bench_id: str, request: SerialReadRequest
    ) -> AsyncIterator[SerialLine]:
        self.actions.append(("serial", bench_id))
        yield SerialLine(text=f"timeout={request.timeout_seconds}")


def _write_config(root: Path, *, benches: int = 2, enabled: bool = True) -> None:
    (root / "agent.yaml").write_text(
        "agent:\n  name: integration-agent\n  log_level: ERROR\nplugins:\n  - power\n",
        encoding="utf-8",
    )
    (root / "simlab.yaml").write_text(
        f"simlab:\n  enabled: {str(enabled).lower()}\n  benches: {benches}\n"
        "  speed_multiplier: 100\n",
        encoding="utf-8",
    )


def test_agent_starts_backend_database_plugins_and_is_idempotent(tmp_path: Path) -> None:
    _write_config(tmp_path)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        await agent.start()

        assert [bench.name for bench in agent.benches()] == ["bench-01", "bench-02"]
        assert [plugin.name for plugin in agent.plugins()] == ["power"]
        assert agent.health_payload() == {
            "status": "healthy",
            "version": "0.9.0-beta",
            "backend": "simlab",
            "database": "healthy",
            "benches": {"total": 2, "online": 2},
        }
        assert all(report.status is HealthStatus.HEALTHY for report in agent.health_reports())
        registered = await agent.event_service.list_events(event_type="BACKEND_REGISTERED")
        discovered = await agent.event_service.list_events(event_type="BENCH_DISCOVERED")
        assert [event.payload["backend_id"] for event in registered] == ["simlab"]
        assert {event.bench_id for event in discovered} == {"bench-01", "bench-02"}

        await agent.shutdown()
        await agent.shutdown()
        assert agent.benches() == []
        assert agent.plugins() == []
        assert agent.health_payload()["status"] == "warning"

    asyncio.run(scenario())


def test_http_lifespan_owns_full_unstarted_agent_lifecycle(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)
    agent = create_agent(tmp_path)

    def is_started() -> bool:
        return agent.started

    assert not is_started()
    with TestClient(create_app(agent)) as client:
        assert is_started()
        assert client.get("/api/v1/health").status_code == 200
        assert any(not task.done() for task in agent._background_tasks)

    assert not is_started()
    assert agent._background_tasks == set()


def test_managed_http_application_rejects_a_prestarted_agent(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)
    agent = create_agent(tmp_path)
    asyncio.run(agent.start())
    try:
        with pytest.raises(ConfigurationError, match="requires an unstarted Agent"):
            create_app(agent)
    finally:
        asyncio.run(agent.shutdown())


def test_failed_distributed_start_rolls_back_and_can_retry(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)

    class DistributedRuntime:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.started = False

        async def start(self) -> None:
            if self.fail:
                raise RuntimeError("distributed startup failed")
            self.started = True

        async def stop(self) -> None:
            self.started = False

        async def publish_inventory(self, *, force: bool = False) -> UUID:
            del force
            return UUID(int=1)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        attempts = 0

        def factory(_agent: LabAgent) -> DistributedRuntime:
            nonlocal attempts
            attempts += 1
            return DistributedRuntime(fail=attempts == 1)

        def is_started() -> bool:
            return agent.started

        agent._distributed_runtime_factory = cast(Any, factory)
        with pytest.raises(RuntimeError, match="distributed startup failed"):
            await agent.start()
        assert not is_started()

        await agent.start()
        assert is_started()
        assert attempts == 2
        await agent.shutdown()

    asyncio.run(scenario())


def test_catalog_refresh_publishes_changed_inventory_to_distributed_runtime(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, benches=1)

    class DistributedRuntime:
        started = True
        publishes = 0

        async def publish_inventory(self, *, force: bool = False) -> UUID:
            assert not force
            self.publishes += 1
            return UUID(int=self.publishes)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        distributed = DistributedRuntime()
        agent.distributed_runtime = cast(Any, distributed)
        await agent.refresh_catalog()
        assert distributed.publishes == 1
        agent.distributed_runtime = None
        await agent.shutdown()

    asyncio.run(scenario())


def test_probe_health_success_survives_inventory_publish_backpressure(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)

    class FailingPublisher:
        started = True

        async def publish_inventory(self, *, force: bool = False) -> UUID:
            del force
            raise RuntimeError("outgoing inventory queue is full")

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        agent.distributed_runtime = cast(Any, FailingPublisher())
        await agent._probe_health_recorder.record(
            TargetHealth(
                bench_id="bench-01",
                status=TargetHealthStatus.OFFLINE,
            )
        )

        record = agent.catalog.get("bench-01")
        assert not record.online
        failures = await agent.event_service.list_events(event_type="INVENTORY_SYNC_FAILED")
        assert len(failures) == 1
        assert failures[0].payload["error"] == "outgoing inventory queue is full"

        agent.distributed_runtime = None
        await agent.shutdown()

    asyncio.run(scenario())


def test_reservation_and_history_survive_agent_restart(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)

    async def scenario() -> None:
        first = create_agent(tmp_path)
        await first.start()
        reservation = await first.reservation_service.reserve("bench-01", "michael")
        await first.shutdown()

        second = create_agent(tmp_path)
        await second.start()
        restored = await second.reservation_service.get_reservation("bench-01")
        assert restored == reservation
        bench = await second.bench_service.get_bench("bench-01")
        assert bench.reserved_by == "michael"
        events = await second.event_service.list_events(bench_id="bench-01")
        assert [event.type for event in events] == ["BENCH_RESERVED", "BENCH_DISCOVERED"]
        await second.shutdown()

    asyncio.run(scenario())


def test_agent_cli_once_and_disabled_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, benches=2)
    assert agent_main(["--config-dir", str(tmp_path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "Lab Agent v0.9.0-beta" in output
    assert "✓ SimLab backend started" in output
    assert "✓ 2 benches registered" in output

    disabled = tmp_path / "disabled"
    disabled.mkdir()
    _write_config(disabled, enabled=False)
    assert agent_main(["--config-dir", str(disabled), "--once"]) == 0
    output = capsys.readouterr().out
    assert "✓ SimLab backend disabled" in output
    assert "✓ 0 benches registered" in output


def test_startup_fails_interrupted_operation_records(tmp_path: Path) -> None:
    _write_config(tmp_path, benches=1)

    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / ".lab-platform" / "lab.db")
        database.initialize()
        operations = SQLiteOperationRepository(database)
        pending = Operation.pending("bench-01", OperationType.POWER_ON, "alice")
        await operations.create(pending)
        database.close()

        agent = create_agent(tmp_path)
        await agent.start()
        recovered = await agent.operation_service.get_operation(pending.id)
        assert recovered.status is OperationStatus.FAILED
        assert recovered.error_code == "AGENT_RESTARTED"
        events = await agent.event_service.list_events(event_type="BACKEND_ERROR")
        assert events[0].payload["operations_failed"] == 1
        await agent.shutdown()

    asyncio.run(scenario())


def test_agent_composes_multiple_backend_instances(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: mixed-agent\n  log_level: ERROR\nplugins: []\n",
        encoding="utf-8",
    )
    (tmp_path / "backends.yaml").write_text(
        """backends:
  - id: virtual-a
    type: simlab
    config:
      benches: 2
      bench_prefix: alpha
      speed_multiplier: 100
  - id: virtual-b
    type: simlab
    config:
      benches: 1
      bench_prefix: beta
      speed_multiplier: 100
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()

        assert agent.backend_registry.backend_ids == ("virtual-a", "virtual-b")
        assert [bench.name for bench in agent.benches()] == [
            "alpha-01",
            "alpha-02",
            "beta-01",
        ]
        assert [record.id for record in agent.catalog.list(backend_id="virtual-b")] == ["beta-01"]
        assert [record.id for record in agent.catalog.list(labels={"location": "simulation"})] == [
            "alpha-01",
            "alpha-02",
            "beta-01",
        ]
        assert agent.health_payload() == {
            "status": "healthy",
            "version": "0.9.0-beta",
            "backend": "mixed",
            "backends": {"ids": ["virtual-a", "virtual-b"], "unavailable": []},
            "database": "healthy",
            "benches": {"total": 3, "online": 3},
        }

        virtual_a = agent.backend_registry.get_backend("virtual-a")
        assert isinstance(virtual_a, SimLabBackend)
        virtual_a.simulator.set_online("alpha-01", False)
        refresh = await agent.catalog.refresh()
        await agent._persist_catalog_refresh(refresh)
        health_events = await agent.event_service.list_events(
            bench_id="alpha-01", event_type="BENCH_HEALTH_CHANGED"
        )
        assert len(health_events) == 1
        assert health_events[0].payload == {
            "backend_id": "virtual-a",
            "health": "unhealthy",
            "online": False,
        }

        await agent.shutdown()

    asyncio.run(scenario())


def test_agent_routes_simlab_and_fake_physical_and_isolates_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: mixed-physical-agent\n  log_level: ERROR\nplugins: []\n",
        encoding="utf-8",
    )
    (tmp_path / "backends.yaml").write_text(
        """backends:
  - id: virtual-lab
    type: simlab
    config:
      benches: 10
      bench_prefix: virtual
      speed_multiplier: 100
  - id: fake-physical
    type: real
    config:
      benches:
        - id: fake-physical-01
          name: Fake ESP32
          target_type: esp32
scheduler:
  poll_interval_seconds: 60
""",
        encoding="utf-8",
    )
    physical = FakePhysicalBackend()
    monkeypatch.setattr(
        RealLabBackend,
        "from_config",
        classmethod(lambda cls, config: physical),
    )

    async def wait_for(operation: Operation, agent: LabAgent) -> Operation:
        for _ in range(100):
            current = await agent.operation_service.get_operation(operation.id)
            if current.status in {
                OperationStatus.SUCCEEDED,
                OperationStatus.FAILED,
                OperationStatus.CANCELLED,
            }:
                return current
            await asyncio.sleep(0.005)
        raise AssertionError(f"operation {operation.id} did not complete")

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            assert agent.backend_registry.backend_ids == ("fake-physical", "virtual-lab")
            assert len(agent.catalog.list(backend_id="virtual-lab")) == 10
            assert [record.id for record in agent.catalog.list(backend_id="fake-physical")] == [
                "fake-physical-01"
            ]

            await agent.reservation_service.reserve("virtual-01", "alice")
            await agent.reservation_service.reserve("fake-physical-01", "bob")
            simulated = await agent.bench_service.reset("virtual-01", "alice")
            hardware = await agent.bench_service.reset("fake-physical-01", "bob")
            completed = await asyncio.gather(
                wait_for(simulated, agent),
                wait_for(hardware, agent),
            )
            assert all(item.status is OperationStatus.SUCCEEDED for item in completed)
            assert physical.actions == [("reset", "fake-physical-01")]

            physical.fail_inventory = True
            refresh = await agent.catalog.refresh()
            assert [failure.backend_id for failure in refresh.backend_failures] == ["fake-physical"]
            assert all(record.online for record in agent.catalog.list(backend_id="virtual-lab"))
            assert not agent.catalog.get("fake-physical-01").online
        finally:
            await agent.shutdown()

    asyncio.run(scenario())


def test_workflow_definitions_load_and_release_after_run(tmp_path: Path) -> None:
    workflow_directory = tmp_path / "workflows"
    workflow_directory.mkdir()
    (workflow_directory / "wait.yaml").write_text(
        """name: wait-smoke
version: 1
requirements:
  capabilities: []
steps:
  - action: wait
    seconds: 0.01
""",
        encoding="utf-8",
    )
    (tmp_path / "probe.yml").write_text(
        """name: probe-smoke
version: 1
requirements:
  capabilities: [probe]
steps:
  - action: probe
""",
        encoding="utf-8",
    )
    (tmp_path / "agent.yaml").write_text(
        """agent:
  name: workflow-agent
  log_level: ERROR
plugins: []
simlab:
  benches: 1
  speed_multiplier: 100
scheduler:
  poll_interval_seconds: 60
workflows:
  definitions_directory: ./workflows
  definition_paths:
    - ./probe.yml
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        assert [
            definition.name for definition in await agent.workflow_service.list_definitions()
        ] == [
            "probe-smoke",
            "wait-smoke",
        ]

        reservation = await agent.reservation_service.reserve("bench-01", "alice")
        run = await agent.workflow_service.start(
            "wait-smoke",
            bench_id="bench-01",
            owner="alice",
        )
        agent.release_reservation_after_workflow(run.id, reservation.id, "alice")
        completed = await agent.workflow_service.wait(run.id)
        assert completed.status is WorkflowRunStatus.SUCCEEDED

        for _ in range(10):
            if await agent.reservation_service.get_active("bench-01") is None:
                break
            await asyncio.sleep(0)
        released = await agent.reservation_service.get(reservation.id)
        assert released.status is ReservationStatus.RELEASED
        timeline = await agent.timeline_repository.list_timeline("bench-01")
        workflow_events = [
            entry for entry in timeline if entry.category is TimelineCategory.WORKFLOW
        ]
        assert {entry.event_type for entry in workflow_events} >= {"WORKFLOW_COMPLETED"}
        lock_events = [
            entry
            for entry in timeline
            if entry.event_type in {"OPERATION_LOCK_ACQUIRED", "OPERATION_LOCK_RELEASED"}
        ]
        assert {entry.event_type for entry in lock_events} == {
            "OPERATION_LOCK_ACQUIRED",
            "OPERATION_LOCK_RELEASED",
        }
        assert all(entry.operation_id is None for entry in lock_events)

        await agent.shutdown()

    asyncio.run(scenario())


def _write_config_runtime_edges(
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
    _write_config_runtime_edges(
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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path, scheduler_interval=0.01)

    async def scenario() -> None:
        agent = create_agent(tmp_path)
        await agent.start()
        try:
            assert len(agent._background_tasks) == 3
            await agent.start_background_workers()
            assert len(agent._background_tasks) == 3

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
    _write_config_runtime_edges(tmp_path, scheduler_interval=0.01)

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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path)

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
    _write_config_runtime_edges(tmp_path)

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
