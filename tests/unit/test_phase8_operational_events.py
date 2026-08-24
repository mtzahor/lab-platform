from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from lab_platform.control_plane.cli import (
    _backup_service,
    _run_database_migration,
)
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.control_plane.operational_events import (
    OPERATIONAL_EVENT_TYPES,
    RetentionOperationalEventSink,
    StructuredOperationalEventSink,
)
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane_core.errors import AgentIncompatibleError
from lab_platform.core.retention import RetentionEvent
from lab_platform.logging import StructuredFormatter
from lab_platform.persistence import SQLiteDatabase
from lab_platform.persistence.database_management import SchemaStatus

_LOGGER_NAME = "lab-platform.control-plane.operational-events"


def _config(tmp_path: Path, *, database_name: str = "control-plane.db") -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / database_name}"},
            "artifacts": {"directory": tmp_path / f"{database_name}-artifacts"},
            "development": {"allow_insecure_agent_transport": True},
        }
    )


def _event_types(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        str(record.event_type)
        for record in caplog.records
        if record.name == _LOGGER_NAME and hasattr(record, "event_type")
    ]


def test_operational_event_catalogue_and_structured_payload_redaction() -> None:
    assert {
        "DATABASE_MIGRATION_STARTED",
        "DATABASE_MIGRATION_COMPLETED",
        "BACKUP_STARTED",
        "BACKUP_COMPLETED",
        "BACKUP_FAILED",
        "RESTORE_STARTED",
        "RESTORE_COMPLETED",
        "RETENTION_DELETION",
        "STORAGE_UNAVAILABLE",
        "VERSION_INCOMPATIBLE",
        "AGENT_UPGRADE_AVAILABLE",
    } == OPERATIONAL_EVENT_TYPES
    record = logging.LogRecord(
        _LOGGER_NAME,
        logging.INFO,
        __file__,
        1,
        "BACKUP_STARTED",
        (),
        None,
    )
    record.event_type = "BACKUP_STARTED"
    record.event_payload = {
        "destination": "/backups/lab.tar.zst",
        "credential": "do-not-render",
    }

    rendered = json.loads(StructuredFormatter().format(record))

    assert rendered["event_type"] == "BACKUP_STARTED"
    assert rendered["event_payload"] == {
        "destination": "/backups/lab.tar.zst",
        "credential": "[REDACTED]",
    }


def test_operational_event_sink_rejects_unregistered_event_types() -> None:
    with pytest.raises(ValueError, match="unknown operational event"):
        StructuredOperationalEventSink().emit("TYPO_EVENT", {})


def test_retention_event_adapter_emits_the_transactional_deletion_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    organisation_id = uuid4()
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    asyncio.run(
        RetentionOperationalEventSink().emit(
            RetentionEvent(
                type="RETENTION_DELETION",
                occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
                resource_kind="artifact",
                resource_id="artifact-1",
                organisation_id=organisation_id,
                metadata={"size_bytes": 42},
            )
        )
    )

    assert _event_types(caplog) == ["RETENTION_DELETION"]
    payload = cast(Any, caplog.records[-1]).event_payload
    assert payload["organisation_id"] == str(organisation_id)
    assert payload["size_bytes"] == 42


def test_backup_cli_service_emits_create_restore_and_failure_events(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_config = _config(tmp_path, database_name="source.db")
    source_database = SQLiteDatabase(tmp_path / "source.db")
    source_database.initialize()
    source_database.close()
    source_config.artifacts.directory.mkdir(parents=True)
    source_config.artifacts.directory.joinpath("serial.log").write_text(
        "serial output",
        encoding="utf-8",
    )
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    backup = _backup_service(source_config).create(tmp_path / "backups")

    assert _event_types(caplog) == ["BACKUP_STARTED", "BACKUP_COMPLETED"]
    caplog.clear()

    target_config = _config(tmp_path, database_name="restored.db")
    restored = _backup_service(target_config).restore(
        backup,
        confirmation="RESTORE",
    )

    assert restored.database_restored
    assert restored.artifacts_restored == 1
    assert _event_types(caplog) == ["RESTORE_STARTED", "RESTORE_COMPLETED"]
    caplog.clear()

    missing_config = _config(tmp_path, database_name="missing.db")
    with pytest.raises(FileNotFoundError):
        _backup_service(missing_config).create(tmp_path / "failed-backups")

    assert _event_types(caplog) == ["BACKUP_STARTED", "BACKUP_FAILED"]


def test_database_migration_command_emits_started_and_completed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = SchemaStatus(
        backend="sqlite",
        current_version=12,
        target_version=12,
        minimum_supported_version=11,
        applied_versions=tuple(range(2, 13)),
        state="current",
    )
    calls: list[tuple[str, bool]] = []

    def migrate(url: str, *, allow_unsupported_source: bool) -> SchemaStatus:
        calls.append((url, allow_unsupported_source))
        return status

    monkeypatch.setattr("lab_platform.control_plane.cli.migrate_database", migrate)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    result = _run_database_migration(
        _config(tmp_path),
        allow_unsupported_source=False,
        output="json",
    )

    assert result == 0
    assert calls == [(f"sqlite:///{tmp_path / 'control-plane.db'}", False)]
    assert _event_types(caplog) == [
        "DATABASE_MIGRATION_STARTED",
        "DATABASE_MIGRATION_COMPLETED",
    ]
    completed = next(
        record
        for record in caplog.records
        if getattr(record, "event_type", None) == "DATABASE_MIGRATION_COMPLETED"
    )
    assert cast(Any, completed).event_payload["schema_version"] == 12


def test_agent_compatibility_emits_upgrade_and_incompatible_events(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = ControlPlaneRuntime(_config(tmp_path))
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    runtime.ensure_agent_compatible("0.8.0", "1.0")
    with pytest.raises(AgentIncompatibleError):
        runtime.ensure_agent_compatible("0.5.0", "1.0")

    assert _event_types(caplog) == [
        "AGENT_UPGRADE_AVAILABLE",
        "AGENT_UPGRADE_AVAILABLE",
        "VERSION_INCOMPATIBLE",
    ]
    incompatible = caplog.records[-1]
    incompatible_payload = cast(Any, incompatible).event_payload
    assert incompatible_payload["upgrade_status"] == "upgrade_required"
    assert "minimum supported" in incompatible_payload["reason"].lower()
    runtime.database.close()


def test_readiness_emits_storage_unavailable_once_per_outage(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ToggleStorage:
        available = False

        async def exists(self, _key: str) -> bool:
            if not self.available:
                raise OSError("storage credential=super-secret is unavailable")
            return False

    async def scenario() -> None:
        runtime = ControlPlaneRuntime(_config(tmp_path))
        runtime.database.initialize()
        runtime._started = True
        storage = ToggleStorage()
        runtime.artifact_storage = cast(Any, storage)
        try:
            first = await runtime.readiness()
            second = await runtime.readiness()
            assert first["ready"] is False
            assert second["ready"] is False
            assert _event_types(caplog) == ["STORAGE_UNAVAILABLE"]

            storage.available = True
            recovered = await runtime.readiness()
            recovered_checks = cast(dict[str, object], recovered["checks"])
            assert recovered_checks["artifact_storage"] == {"ready": True}
            storage.available = False
            await runtime.readiness()
            assert _event_types(caplog) == [
                "STORAGE_UNAVAILABLE",
                "STORAGE_UNAVAILABLE",
            ]
        finally:
            runtime._started = False
            runtime.database.close()

    caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
    asyncio.run(scenario())
