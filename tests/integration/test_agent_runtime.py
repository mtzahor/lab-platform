from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lab_platform.agent.cli import main as agent_main
from lab_platform.agent.runtime import create_agent
from lab_platform.models import HealthStatus, Operation, OperationStatus, OperationType
from lab_platform.persistence import SQLiteDatabase, SQLiteOperationRepository


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
            "version": "0.2.0-alpha",
            "backend": "simlab",
            "database": "healthy",
            "benches": {"total": 2, "online": 2},
        }
        assert all(report.status is HealthStatus.HEALTHY for report in agent.health_reports())

        await agent.shutdown()
        await agent.shutdown()
        assert agent.benches() == []
        assert agent.plugins() == []
        assert agent.health_payload()["status"] == "warning"

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
        assert [event.type for event in events] == ["BENCH_RESERVED"]
        await second.shutdown()

    asyncio.run(scenario())


def test_agent_cli_once_and_disabled_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, benches=2)
    assert agent_main(["--config-dir", str(tmp_path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "Lab Agent v0.2.0-alpha" in output
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
