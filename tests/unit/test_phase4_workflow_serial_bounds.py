from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.workflows import SingleBackendResolver, WorkflowRunner, WorkflowService
from lab_platform.models import (
    ArtifactOwnerType,
    ArtifactRecord,
    BackendProgress,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
    WorkflowRunStatus,
    WorkflowStepStatus,
)
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.workflows import SQLiteWorkflowRepository

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class ArtifactRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, ArtifactRecord] = {}
        self.keys: dict[tuple[UUID, str], UUID] = {}

    async def save(
        self,
        record: ArtifactRecord,
        *,
        idempotency_key: str | None = None,
    ) -> ArtifactRecord:
        if idempotency_key is not None:
            existing_id = self.keys.get((record.owner_id, idempotency_key))
            if existing_id is not None:
                return self.records[existing_id]
            self.keys[(record.owner_id, idempotency_key)] = record.id
        self.records[record.id] = record
        return record

    async def get(self, artifact_id: UUID) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    async def get_by_idempotency_key(
        self,
        owner_id: UUID,
        idempotency_key: str,
    ) -> ArtifactRecord | None:
        artifact_id = self.keys.get((owner_id, idempotency_key))
        return self.records.get(artifact_id) if artifact_id is not None else None

    async def list_for_owner(
        self,
        owner_type: ArtifactOwnerType,
        owner_id: UUID,
    ) -> list[ArtifactRecord]:
        return [
            record
            for record in self.records.values()
            if record.owner_type is owner_type and record.owner_id == owner_id
        ]


class SerialBackend:
    def __init__(self) -> None:
        self.serial_text: list[str] = []
        self.flash_progress: list[BackendProgress] = []
        self.flash_error: Exception | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def list_benches(self) -> list[BenchSnapshot]:
        return [await self.get_bench("bench-01")]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        return BenchSnapshot(
            id=bench_id,
            name="Serial bounds bench",
            status=BenchStatus.AVAILABLE,
            online=True,
            powered=True,
            capabilities=["firmware", "serial"],
        )

    async def power_on(self, bench_id: str) -> None:
        return None

    async def power_off(self, bench_id: str) -> None:
        return None

    async def power_cycle(self, bench_id: str) -> None:
        return None

    async def reset(self, bench_id: str) -> None:
        return None

    async def probe(self, bench_id: str) -> TargetHealth:
        return TargetHealth(bench_id=bench_id, status=TargetHealthStatus.ONLINE)

    async def flash_firmware(
        self,
        bench_id: str,
        firmware: FirmwareInput,
    ) -> AsyncIterator[BackendProgress]:
        for progress in self.flash_progress:
            yield progress
        if self.flash_error is not None:
            raise self.flash_error

    async def read_serial(
        self,
        bench_id: str,
        request: SerialReadRequest,
    ) -> AsyncIterator[SerialLine]:
        for text in self.serial_text:
            yield SerialLine(timestamp=NOW, text=text)


class Reservations:
    def __init__(self) -> None:
        self.id = uuid4()

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        return self.id


class Stack:
    def __init__(
        self,
        tmp_path: Path,
        *,
        maximum_size_bytes: int,
        maximum_message_bytes: int,
        buffer_lines: int = 500,
        redact_patterns: tuple[str, ...] = (),
    ) -> None:
        self.database = SQLiteDatabase(tmp_path / "workflows.db")
        self.database.initialize()
        self.workflows = SQLiteWorkflowRepository(self.database)
        self.backend = SerialBackend()
        self.reservations = Reservations()
        self.artifact_repository = ArtifactRepository()
        self.artifacts = ArtifactService(
            self.artifact_repository,
            tmp_path / "artifacts",
            maximum_size_bytes=maximum_size_bytes,
        )
        resolver = SingleBackendResolver(self.backend)
        self.runner = WorkflowRunner(
            self.workflows,
            resolver,
            self.reservations,
            clock=lambda: NOW,
            artifact_service=self.artifacts,
            serial_buffer_lines=buffer_lines,
            serial_artifact_max_bytes=maximum_size_bytes,
            serial_message_max_bytes=maximum_message_bytes,
            serial_redact_patterns=redact_patterns,
        )
        self.service = WorkflowService(
            self.workflows,
            resolver,
            self.reservations,
            self.runner,
            clock=lambda: NOW,
        )

    async def close(self) -> None:
        await self.runner.shutdown()
        self.database.close()


def test_serial_artifact_is_complete_redacted_and_sqlite_output_is_bounded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = Stack(
            tmp_path,
            maximum_size_bytes=200,
            maximum_message_bytes=100,
            buffer_lines=2,
            redact_patterns=(r"alpha",),
        )
        try:
            stack.backend.serial_text = ["secret=alpha", "MIDDLE", "READY"]
            await stack.service.register_yaml(
                """
name: complete-serial
version: 1
requirements: {capabilities: [serial]}
steps:
  - action: read_serial
  - action: assert_serial
    pattern: ^READY$
"""
            )
            run = await stack.service.start("complete-serial", bench_id="bench-01", owner="ci")
            completed = await stack.service.wait(run.id)
            results = await stack.service.list_step_results(run.id)

            assert completed.status is WorkflowRunStatus.SUCCEEDED
            assert results[0].status is WorkflowStepStatus.SUCCEEDED
            assert "lines" not in results[0].output
            assert results[0].output["serial_line_count"] == 3
            assert results[0].output["log_message_count"] == 3
            assert len(results[0].artifact_ids) == 1
            artifact = stack.artifact_repository.records[results[0].artifact_ids[0]]
            assert artifact.size_bytes <= 200
            assert Path(artifact.path).read_text() == ("secret=[REDACTED]\nMIDDLE\nREADY\n")
        finally:
            await stack.close()

    asyncio.run(scenario())


def test_serial_total_limit_fails_and_persists_only_the_complete_prefix(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = Stack(
            tmp_path,
            maximum_size_bytes=10,
            maximum_message_bytes=10,
        )
        try:
            stack.backend.serial_text = ["1234", "5678", "overflow"]
            await stack.service.register_yaml(
                """
name: total-limit
version: 1
requirements: {capabilities: [serial]}
steps: [{action: read_serial}]
"""
            )
            run = await stack.service.start("total-limit", bench_id="bench-01", owner="ci")
            failed = await stack.service.wait(run.id)
            result = (await stack.service.list_step_results(run.id))[0]

            assert failed.status is WorkflowRunStatus.FAILED
            assert failed.error_code == "WORKFLOW_STEP_FAILED"
            assert "artifact size limit" in (failed.error_message or "")
            assert result.output["serial_line_count"] == 2
            assert result.output["log_size_bytes"] == 10
            artifact = stack.artifact_repository.records[result.artifact_ids[0]]
            assert Path(artifact.path).read_bytes() == b"1234\n5678\n"
        finally:
            await stack.close()

    asyncio.run(scenario())


def test_serial_per_message_limit_fails_without_retaining_the_oversize_line(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stack = Stack(
            tmp_path,
            maximum_size_bytes=100,
            maximum_message_bytes=5,
        )
        try:
            stack.backend.serial_text = ["12345"]
            await stack.service.register_yaml(
                """
name: message-limit
version: 1
requirements: {capabilities: [serial]}
steps: [{action: read_serial}]
"""
            )
            run = await stack.service.start("message-limit", bench_id="bench-01", owner="ci")
            failed = await stack.service.wait(run.id)
            result = (await stack.service.list_step_results(run.id))[0]

            assert failed.status is WorkflowRunStatus.FAILED
            assert "per-message size limit" in (failed.error_message or "")
            assert result.output["serial_line_count"] == 0
            assert result.output["log_size_bytes"] == 0
            assert result.artifact_ids == []
        finally:
            await stack.close()

    asyncio.run(scenario())


def test_flash_failure_persists_bounded_redacted_progress_diagnostics(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = Stack(
            tmp_path,
            maximum_size_bytes=500,
            maximum_message_bytes=200,
            buffer_lines=1,
            redact_patterns=(r"token=[^ ]+",),
        )
        firmware = tmp_path / "firmware.bin"
        firmware.write_bytes(b"firmware")
        try:
            stack.backend.flash_progress = [
                BackendProgress(
                    percent=10,
                    message="connecting token=secret",
                    serial_lines=[SerialLine(timestamp=NOW, text="ROM token=serial-secret")],
                ),
                BackendProgress(percent=50, message="writing flash"),
            ]
            stack.backend.flash_error = RuntimeError("flasher transport broke")
            await stack.service.register_yaml(
                f"""
name: flash-diagnostics
version: 1
requirements: {{capabilities: [firmware]}}
steps: [{{action: flash, firmware: {firmware}}}]
"""
            )
            run = await stack.service.start("flash-diagnostics", bench_id="bench-01", owner="ci")
            failed = await stack.service.wait(run.id)
            result = (await stack.service.list_step_results(run.id))[0]

            assert failed.status is WorkflowRunStatus.FAILED
            assert failed.error_message == "flasher transport broke"
            assert result.output["progress_event_count"] == 2
            assert result.output["progress_truncated"] is True
            assert result.output["progress"] == [{"percent": 50, "message": "writing flash"}]
            artifact = stack.artifact_repository.records[result.artifact_ids[0]]
            assert artifact.artifact_type == "flash_log"
            assert Path(artifact.path).read_text() == (
                "[ 10%] connecting [REDACTED]\nROM [REDACTED]\n[ 50%] writing flash\n"
            )
        finally:
            await stack.close()

    asyncio.run(scenario())
