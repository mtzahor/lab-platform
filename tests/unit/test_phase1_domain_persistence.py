from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from lab_platform.core import (
    BackendTimeoutError,
    BenchNotFoundError,
    BenchOperationInProgressError,
    CapabilityNotSupportedError,
    SimulationFailureError,
)
from lab_platform.models import (
    BackendProgress,
    EventRecord,
    FirmwareArtifact,
    FirmwareInput,
    InvalidOperationTransition,
    Operation,
    OperationStatus,
    OperationType,
    Reservation,
    ReservationStatus,
)
from lab_platform.persistence import (
    SCHEMA_VERSION,
    SQLiteArtifactRepository,
    SQLiteDatabase,
    SQLiteEventRepository,
    SQLiteOperationRepository,
    SQLiteReservationRepository,
)
from lab_platform.simlab import SimLab
from lab_platform.simlab_adapter import SimLabBackend, map_simlab_bench
from pydantic import ValidationError

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def test_operation_model_enforces_state_machine_and_firmware_metadata() -> None:
    pending = Operation.pending("bench-01", OperationType.FLASH_FIRMWARE, "michael", now=NOW)
    assert pending.status is OperationStatus.PENDING
    assert pending.progress == 0
    with pytest.raises(InvalidOperationTransition):
        pending.transition(OperationStatus.SUCCEEDED, now=NOW)

    running = pending.transition(OperationStatus.RUNNING, now=NOW + timedelta(seconds=1))
    requested = running.transition(
        OperationStatus.CANCEL_REQUESTED,
        now=NOW + timedelta(seconds=2),
    )
    succeeded = requested.transition(
        OperationStatus.SUCCEEDED,
        now=NOW + timedelta(seconds=3),
        message="completed before cancellation",
    )
    assert succeeded.progress == 100
    assert succeeded.completed_at == NOW + timedelta(seconds=3)

    running_again = pending.transition(OperationStatus.RUNNING, now=NOW)
    failed = running_again.transition(
        OperationStatus.FAILED,
        now=NOW,
        error_code="BACKEND_FAILURE",
        error_message="boom",
    )
    assert failed.error_code == "BACKEND_FAILURE"
    cancelled = pending.transition(OperationStatus.CANCELLED, now=NOW)
    assert cancelled.status is OperationStatus.CANCELLED

    with pytest.raises(ValidationError):
        FirmwareInput(
            filename="bad.bin",
            local_path=Path("bad.bin"),
            sha256="bad",
            size_bytes=1,
        )
    with pytest.raises(ValidationError):
        FirmwareInput(
            filename="empty.bin",
            local_path=Path("empty.bin"),
            sha256="0" * 64,
            size_bytes=0,
        )


def test_sqlite_repositories_are_atomic_filterable_and_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "lab.db"
        database = SQLiteDatabase(path)
        database.initialize()
        database.initialize()
        reservations = SQLiteReservationRepository(database)
        operations = SQLiteOperationRepository(database)
        events = SQLiteEventRepository(database)
        artifacts = SQLiteArtifactRepository(database)

        first = Reservation(
            id=uuid4(),
            bench_id="bench-01",
            owner="alice",
            created_at=NOW,
        )
        assert await reservations.create(first) == first
        contender = first.model_copy(update={"id": uuid4(), "owner": "bob"})
        assert await reservations.create(contender) == first
        assert await reservations.get_active("bench-01") == first
        released = first.model_copy(
            update={"status": ReservationStatus.RELEASED, "released_at": NOW}
        )
        await reservations.release(released)
        assert await reservations.get_active("bench-01") is None

        power = Operation.pending("bench-01", OperationType.POWER_CYCLE, "alice", now=NOW)
        await operations.create(power)
        with pytest.raises(BenchOperationInProgressError):
            await operations.create(
                Operation.pending("bench-01", OperationType.POWER_ON, "alice", now=NOW)
            )
        running = power.transition(OperationStatus.RUNNING, now=NOW)
        completed = running.transition(OperationStatus.SUCCEEDED, now=NOW)
        await operations.update(running)
        await operations.update(completed)
        assert await operations.get(power.id) == completed
        assert await operations.list(
            bench_id="bench-01",
            status=OperationStatus.SUCCEEDED,
            operation_type=OperationType.POWER_CYCLE,
            limit=1,
        ) == [completed]

        interrupted = Operation.pending("bench-02", OperationType.POWER_OFF, "bob", now=NOW)
        await operations.create(interrupted)
        assert await operations.recover_incomplete(NOW + timedelta(minutes=1)) == 1
        recovered = await operations.get(interrupted.id)
        assert recovered is not None
        assert recovered.status is OperationStatus.FAILED
        assert recovered.error_code == "AGENT_RESTARTED"

        event = EventRecord(
            timestamp=NOW,
            type="POWER_CYCLE_COMPLETED",
            source="platform",
            bench_id="bench-01",
            operation_id=power.id,
            actor="alice",
            payload={"status": "succeeded"},
        )
        await events.create(event)
        assert await events.list(
            bench_id="bench-01",
            event_type="POWER_CYCLE_COMPLETED",
            after=NOW - timedelta(seconds=1),
            before=NOW + timedelta(seconds=1),
            limit=1,
        ) == [event]
        assert await events.list(event_type="missing") == []

        artifact = FirmwareArtifact(
            sha256="a" * 64,
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            size_bytes=4,
            version="1.0",
            created_at=NOW,
        )
        assert await artifacts.save(artifact) == artifact
        updated_artifact = await artifacts.save(artifact.model_copy(update={"version": "2.0"}))
        assert updated_artifact.version == "2.0"
        database.close()
        database.close()

        connection = sqlite3.connect(path)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        connection.close()

    asyncio.run(scenario())


def test_simlab_mapping_and_backend_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = SimLabBackend(
            bench_count=3,
            clock_mode="manual",
            flash_duration_seconds=5,
        )
        await backend.start()
        benches = await backend.list_benches()
        assert [bench.id for bench in benches] == ["bench-01", "bench-02", "bench-03"]
        assert benches[0].capabilities == ["power", "serial", "firmware", "probe", "reset"]
        assert (await backend.get_bench("bench-02")).powered is False
        with pytest.raises(BenchNotFoundError):
            await backend.get_bench("missing")

        await backend.power_off("bench-01")
        assert (await backend.get_bench("bench-01")).powered is False
        await backend.power_on("bench-01")
        cycle = asyncio.create_task(backend.power_cycle("bench-01"))
        await asyncio.sleep(0)
        assert (await backend.get_bench("bench-01")).powered is False
        backend.simulator.tick(2)
        await cycle
        assert (await backend.get_bench("bench-01")).powered is True

        firmware = FirmwareInput(
            filename="demo.bin",
            local_path=tmp_path / "demo.bin",
            sha256="b" * 64,
            size_bytes=3,
            version="2.1.0",
        )
        with pytest.raises(CapabilityNotSupportedError):
            async for _ in backend.flash_firmware("bench-02", firmware):
                pass

        backend.simulator.inject_failure("bench-01", "flash_failure")
        with pytest.raises(SimulationFailureError):
            progress = backend.flash_firmware("bench-01", firmware)
            assert (await anext(progress)).percent == 0
            for expected in (20, 40):
                next_progress: asyncio.Future[BackendProgress] = asyncio.ensure_future(
                    anext(progress)
                )
                await asyncio.sleep(0)
                backend.simulator.tick(1)
                assert (await next_progress).percent == expected
            failed_progress: asyncio.Future[BackendProgress] = asyncio.ensure_future(
                anext(progress)
            )
            await asyncio.sleep(0)
            backend.simulator.tick(1)
            await failed_progress

        backend.simulator.inject_failure("bench-01", "backend_timeout")
        with pytest.raises(BackendTimeoutError):
            await backend.power_off("bench-01")
        await backend.stop()

        raw = SimLab(bench_count=1)
        await raw.start()
        internal = raw.get_bench("bench-01")
        shared = map_simlab_bench(internal)
        assert shared.id == "bench-01"
        with pytest.raises(FrozenInstanceError):
            internal.powered = False  # type: ignore[misc]
        await raw.shutdown()

    asyncio.run(scenario())
