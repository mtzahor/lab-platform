from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.models import (
    AuditEvent,
    AuditOutcome,
    LoginAttempt,
    Organisation,
)
from lab_platform.persistence import (
    DEFAULT_ORGANISATION_ID,
    SQLiteDatabase,
    SQLiteIdentityRepository,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
DEFAULT_ORGANISATION = UUID(DEFAULT_ORGANISATION_ID)
SECOND_ORGANISATION = UUID("00000000-0000-0000-0000-000000000002")


def _audit_event(event_id: int, organisation_id: UUID, timestamp: datetime) -> AuditEvent:
    return AuditEvent(
        id=UUID(int=event_id),
        organisation_id=organisation_id,
        timestamp=timestamp,
        action="RETENTION_TEST",
        resource_type="SYSTEM",
        outcome=AuditOutcome.SUCCEEDED,
    )


def test_audit_retention_pruning_is_bounded_and_tenant_safe(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "audit-retention.db")
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        with database.transaction() as connection:
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        assert {
            "audit_events_retention_time",
            "login_attempts_retention_time",
        } <= indexes
        await repository.create_organisation(
            Organisation(
                id=SECOND_ORGANISATION,
                slug="second",
                name="Second",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        events = (
            _audit_event(1, DEFAULT_ORGANISATION, NOW - timedelta(days=100)),
            _audit_event(2, DEFAULT_ORGANISATION, NOW - timedelta(days=90)),
            _audit_event(3, SECOND_ORGANISATION, NOW - timedelta(days=80)),
            _audit_event(4, DEFAULT_ORGANISATION, NOW - timedelta(days=30)),
            _audit_event(5, SECOND_ORGANISATION, NOW - timedelta(days=29)),
        )
        for event in events:
            await repository.create_audit_event(event)

        cutoff = NOW - timedelta(days=30)
        assert (
            await repository.prune_audit_events_for_retention(
                cutoff,
                batch_size=1,
                organisation_id=DEFAULT_ORGANISATION,
            )
            == 1
        )
        assert await repository.get_audit_event(DEFAULT_ORGANISATION, events[0].id) is None
        assert await repository.get_audit_event(DEFAULT_ORGANISATION, events[1].id) == events[1]
        assert await repository.get_audit_event(SECOND_ORGANISATION, events[2].id) == events[2]

        assert await repository.prune_audit_events_for_retention(cutoff, batch_size=2) == 2
        assert await repository.get_audit_event(DEFAULT_ORGANISATION, events[1].id) is None
        assert await repository.get_audit_event(SECOND_ORGANISATION, events[2].id) is None
        # The retention boundary is exclusive and fresh events remain append-only.
        assert await repository.get_audit_event(DEFAULT_ORGANISATION, events[3].id) == events[3]
        assert await repository.get_audit_event(SECOND_ORGANISATION, events[4].id) == events[4]
        assert await repository.prune_audit_events_for_retention(cutoff) == 0

        with pytest.raises(ValueError, match="timezone-aware"):
            await repository.prune_audit_events_for_retention(
                datetime(2026, 8, 1),
            )
        with pytest.raises(ValueError, match="positive"):
            await repository.prune_audit_events_for_retention(cutoff, batch_size=0)
        database.close()

    asyncio.run(scenario())


def test_login_attempt_retention_pruning_is_bounded_and_slug_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "login-attempt-retention.db")
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        attempts = (
            LoginAttempt(
                id=UUID(int=11),
                organisation_slug="default",
                username="alice",
                attempted_at=NOW - timedelta(minutes=60),
            ),
            LoginAttempt(
                id=UUID(int=12),
                organisation_slug="DEFAULT",
                username="alice",
                attempted_at=NOW - timedelta(minutes=45),
            ),
            LoginAttempt(
                id=UUID(int=13),
                organisation_slug="second",
                username="bob",
                attempted_at=NOW - timedelta(minutes=30),
            ),
            LoginAttempt(
                id=UUID(int=14),
                organisation_slug="default",
                username="alice",
                attempted_at=NOW - timedelta(minutes=15),
            ),
        )
        for attempt in attempts:
            await repository.record_login_attempt(attempt)

        cutoff = NOW - timedelta(minutes=15)
        assert (
            await repository.prune_login_attempts_for_retention(
                cutoff,
                batch_size=1,
                organisation_slug="DeFaUlT",
            )
            == 1
        )
        with database.transaction() as connection:
            remaining = {
                UUID(str(row["id"]))
                for row in connection.execute("SELECT id FROM login_attempts").fetchall()
            }
        assert remaining == {attempt.id for attempt in attempts[1:]}

        assert await repository.prune_login_attempts_for_retention(cutoff, batch_size=2) == 2
        with database.transaction() as connection:
            remaining = {
                UUID(str(row["id"]))
                for row in connection.execute("SELECT id FROM login_attempts").fetchall()
            }
        assert remaining == {attempts[3].id}
        assert await repository.prune_login_attempts_for_retention(cutoff) == 0
        database.close()

    asyncio.run(scenario())


def test_runtime_schedules_bounded_identity_retention_from_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = ControlPlaneRuntime(
            ControlPlaneConfig.model_validate(
                {
                    "control_plane": {
                        "host": "127.0.0.1",
                        "port": 8443,
                        "public_url": "http://127.0.0.1:8443",
                    },
                    "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
                    "agent_gateway": {"monitor_interval_seconds": 3600},
                    "artifacts": {"directory": tmp_path / "artifacts"},
                    "audit": {"retention_days": 17},
                    "security": {"login_rate_limit": {"attempts": 5, "window_minutes": 23}},
                    "development": {"allow_insecure_agent_transport": True},
                }
            )
        )
        audit_prune = AsyncMock(return_value=0)
        login_prune = AsyncMock(return_value=0)
        monkeypatch.setattr(
            runtime.identity_repository,
            "prune_audit_events_for_retention",
            audit_prune,
        )
        monkeypatch.setattr(
            runtime.identity_repository,
            "prune_login_attempts_for_retention",
            login_prune,
        )

        initial_due_at = runtime._next_identity_maintenance_at
        assert initial_due_at is None
        await runtime.start()
        try:
            due_at = runtime._next_identity_maintenance_at
            assert due_at is not None
            await runtime.monitor_once()
            audit_prune.assert_awaited_once()
            login_prune.assert_awaited_once()
            audit_call = audit_prune.await_args
            login_call = login_prune.await_args
            assert audit_call is not None
            assert login_call is not None
            assert audit_call.kwargs == {"batch_size": 1_000}
            assert login_call.kwargs == {"batch_size": 1_000}
            assert login_call.args[0] - audit_call.args[0] == timedelta(
                days=17,
                minutes=-23,
            )

            # A second monitor pass inside the hourly interval is a no-op.
            await runtime.monitor_once()
            assert audit_prune.await_count == 1
            assert login_prune.await_count == 1
        finally:
            await runtime.stop()
        stopped_due_at = runtime._next_identity_maintenance_at
        assert stopped_due_at is None

    asyncio.run(scenario())
