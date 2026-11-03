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
from lab_platform.control_plane.observability import OperationalMetrics
from lab_platform.control_plane.operational_events import (
    OPERATIONAL_EVENT_TYPES,
    RetentionOperationalEventSink,
    StructuredOperationalEventSink,
)
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.control_plane_core.errors import AgentIncompatibleError
from lab_platform.core.retention import RetentionEvent
from lab_platform.logging import HumanFormatter, StructuredFormatter, redact_log_text
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


def test_operational_metrics_render_bounded_prometheus_labels_and_legacy_gauges() -> None:
    metrics = OperationalMetrics()
    metrics.observe_http(
        method="get",
        route="/api/v1/agents/{agent_id:uuid}",
        status_code=200,
        duration_seconds=0.125,
    )
    metrics.observe_http(
        method="GET",
        route="/api/v1/agents/{agent_id:uuid}",
        status_code=200,
        duration_seconds=0.375,
    )
    metrics.record_background_failure('retention"worker')

    rendered = metrics.render_prometheus(
        {"agents_online": 3, "invalid-name": 7, "request_latency": float("inf")}
    )

    assert 'method="GET"' in rendered
    assert 'route="/api/v1/agents/{agent_id:uuid}"' in rendered
    assert "lab_platform_http_requests_total" in rendered
    assert " 2\n" in rendered
    assert "_sum" in rendered and " 0.500000000\n" in rendered
    assert 'worker="retention\\"worker"' in rendered
    assert 'lab_platform_runtime{name="agents_online"} 3' in rendered
    assert "agents_online 3" in rendered
    assert "invalid-name" not in rendered
    assert "request_latency" not in rendered


@pytest.mark.parametrize(
    ("route", "status", "duration"),
    [("relative", 200, 0.1), ("/route", 99, 0.1), ("/route", 200, -0.1)],
)
def test_operational_metrics_reject_invalid_observations(
    route: str,
    status: int,
    duration: float,
) -> None:
    with pytest.raises(ValueError):
        OperationalMetrics().observe_http(
            method="GET",
            route=route,
            status_code=status,
            duration_seconds=duration,
        )


def test_production_log_format_redacts_platform_tokens_bearers_and_dsn_passwords() -> None:
    token = "lpa_abcdefghijklmnopqrstuvwxyz0123456789"
    message = (
        f"Authorization: Bearer {token}; database="
        "postgresql://lab:database-secret@postgres/lab_platform password=plain-secret"
    )
    record = logging.LogRecord("control-plane", logging.INFO, __file__, 1, message, (), None)
    record.agent_id = "agent-1"

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["agent_id"] == "agent-1"
    assert token not in payload["message"]
    assert "database-secret" not in payload["message"]
    assert "plain-secret" not in payload["message"]
    assert payload["message"].count("[REDACTED]") >= 3
    assert token not in HumanFormatter().format(record)


def test_redaction_does_not_hide_non_secret_operational_identifiers() -> None:
    value = "agent=lab-agent-01 operation=123e4567-e89b-12d3-a456-426614174000"

    assert redact_log_text(value) == value


def test_structured_context_redacts_nested_compound_secret_fields() -> None:
    record = logging.LogRecord("control-plane", logging.INFO, __file__, 1, "event", (), None)
    record.event_payload = {
        "database_password": "hidden-password",
        "agent_token_value": "hidden-token",
        "nested": {"client-secret": "hidden-secret", "agent_id": "agent-1"},
    }

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["event_payload"] == {
        "database_password": "[REDACTED]",
        "agent_token_value": "[REDACTED]",
        "nested": {"client-secret": "[REDACTED]", "agent_id": "agent-1"},
    }
