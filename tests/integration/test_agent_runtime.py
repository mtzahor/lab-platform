from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from lab_platform.agent.api import create_app
from lab_platform.agent.cli import main as agent_main
from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.core.errors import ConfigurationError
from lab_platform.models import (
    BackendProgress,
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
            "version": "0.8.0-alpha",
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
    assert "Lab Agent v0.8.0-alpha" in output
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
            "version": "0.8.0-alpha",
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
