from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from lab_platform.core.artifact_storage import LocalArtifactStorage
from lab_platform.core.artifacts import ArtifactService
from lab_platform.core.retention import (
    CallableMaintenanceJob,
    MaintenanceResult,
    RetentionCandidate,
    RetentionClass,
    RetentionEvent,
    RetentionPolicy,
    RetentionState,
    RetentionWorker,
    retention_class_for_artifact_type,
)
from lab_platform.models import ArtifactOwnerType, EventRecord
from lab_platform.persistence import SQLiteDatabase, SQLiteGenericArtifactRepository

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def test_retention_policy_distinguishes_artifact_classes_and_failed_workflows() -> None:
    policy = RetentionPolicy(
        default_days=30,
        failed_workflow_days=90,
        firmware_days=180,
        class_days={
            RetentionClass.SERIAL_LOG: 14,
            RetentionClass.JUNIT_REPORT: 45,
            RetentionClass.DIAGNOSTIC_BUNDLE: None,
        },
    )

    assert policy.expires_at(NOW, RetentionClass.OTHER) == NOW + timedelta(days=30)
    assert policy.expires_at(NOW, RetentionClass.FIRMWARE) == NOW + timedelta(days=180)
    assert policy.expires_at(NOW, RetentionClass.SERIAL_LOG) == NOW + timedelta(days=14)
    assert policy.expires_at(NOW, RetentionClass.JUNIT_REPORT) == NOW + timedelta(days=45)
    assert policy.expires_at(NOW, RetentionClass.DIAGNOSTIC_BUNDLE) is None
    assert policy.expires_at(
        NOW,
        RetentionClass.SERIAL_LOG,
        failed_workflow=True,
    ) == NOW + timedelta(days=90)

    assert retention_class_for_artifact_type("firmware-binary") is RetentionClass.FIRMWARE
    assert retention_class_for_artifact_type("serial-log") is RetentionClass.SERIAL_LOG
    assert retention_class_for_artifact_type("junit_xml") is RetentionClass.JUNIT_REPORT
    assert retention_class_for_artifact_type("flash-log") is RetentionClass.WORKFLOW_LOG
    assert (
        retention_class_for_artifact_type("diagnostic-bundle") is RetentionClass.DIAGNOSTIC_BUNDLE
    )
    assert retention_class_for_artifact_type("unknown") is RetentionClass.OTHER

    with pytest.raises(ValueError, match="positive or null"):
        RetentionPolicy(default_days=0)
    with pytest.raises(ValueError, match="class_days.serial_log"):
        RetentionPolicy(class_days={RetentionClass.SERIAL_LOG: -1})
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.expires_at(NOW.replace(tzinfo=None), RetentionClass.OTHER)


def test_retention_candidate_and_worker_configuration_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _candidate(size_bytes=-1)
    with pytest.raises(ValueError, match="already be claimed"):
        RetentionCandidate(
            artifact_id=UUID(int=1),
            organisation_id=UUID(int=2),
            storage_key="object",
            retention_class=RetentionClass.OTHER,
            expires_at=NOW,
            claim_token=UUID(int=3),
            state=RetentionState.ACTIVE,
        )
    with pytest.raises(ValueError, match="attempt count"):
        RetentionCandidate(
            artifact_id=UUID(int=1),
            organisation_id=UUID(int=2),
            storage_key="object",
            retention_class=RetentionClass.OTHER,
            expires_at=NOW,
            claim_token=UUID(int=3),
            attempt_count=0,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        RetentionCandidate(
            artifact_id=UUID(int=1),
            organisation_id=UUID(int=2),
            storage_key="object",
            retention_class=RetentionClass.OTHER,
            expires_at=NOW.replace(tzinfo=None),
            claim_token=UUID(int=3),
        )

    repository = _RetentionRepository([])
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="TTL"):
        RetentionWorker(repository, storage, claim_ttl=timedelta(0))
    duplicate_jobs = (
        CallableMaintenanceJob("audit", lambda _now: 0),
        CallableMaintenanceJob("audit", lambda _now: 0),
    )
    with pytest.raises(ValueError, match="unique"):
        RetentionWorker(repository, storage, maintenance_jobs=duplicate_jobs)


class _RetentionRepository:
    def __init__(self, candidates: list[RetentionCandidate]) -> None:
        self.candidates = candidates
        self.claim_calls: list[tuple[datetime, int, timedelta]] = []
        self.tombstones: list[tuple[UUID, UUID, datetime]] = []
        self.failures: list[tuple[UUID, UUID, datetime, str]] = []

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> list[RetentionCandidate]:
        self.claim_calls.append((now, limit, claim_ttl))
        claimed = self.candidates[:limit]
        del self.candidates[:limit]
        return claimed

    async def mark_tombstoned(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        deleted_at: datetime,
    ) -> bool:
        self.tombstones.append((artifact_id, claim_token, deleted_at))
        return True

    async def mark_failed(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> bool:
        self.failures.append((artifact_id, claim_token, failed_at, error))
        return True


class _RetentionEvents:
    def __init__(self) -> None:
        self.items: list[RetentionEvent] = []

    async def emit(self, event: RetentionEvent) -> None:
        self.items.append(event)


class _FailingRetentionEvents(_RetentionEvents):
    async def emit(self, event: RetentionEvent) -> None:
        raise RuntimeError(f"event sink unavailable for {event.resource_kind}")


def _candidate(*, artifact_id: int = 1, size_bytes: int = 12) -> RetentionCandidate:
    return RetentionCandidate(
        artifact_id=UUID(int=artifact_id),
        organisation_id=UUID(int=100),
        storage_key=f"objects/{artifact_id}/content",
        retention_class=RetentionClass.SERIAL_LOG,
        expires_at=NOW - timedelta(minutes=1),
        claim_token=UUID(int=artifact_id + 1_000),
        size_bytes=size_bytes,
    )


def test_retention_worker_deletes_then_tombstones_and_audits_idempotently(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        candidate = _candidate()
        repository = _RetentionRepository([candidate])
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        await storage.put(
            candidate.storage_key,
            _content(b"expired-data"),
            maximum_size_bytes=100,
        )
        events = _RetentionEvents()
        worker = RetentionWorker(
            repository,
            storage,
            events=events,
            maintenance_jobs=(CallableMaintenanceJob("audit_events", lambda _now: 3),),
            claim_ttl=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        first = await worker.run_once(limit=10)
        assert first.claimed == 1
        assert first.tombstoned == 1
        assert first.deleted_bytes == candidate.size_bytes
        assert first.maintenance_deleted == {"audit_events": 3}
        assert first.successful is True
        assert await storage.exists(candidate.storage_key) is False
        assert repository.tombstones == [(candidate.artifact_id, candidate.claim_token, NOW)]
        assert repository.failures == []
        assert [(item.type, item.resource_kind, item.count) for item in events.items] == [
            ("RETENTION_DELETION", "artifact", 1),
            ("RETENTION_DELETION", "audit_events", 3),
        ]
        assert events.items[0].metadata["storage_key"] == candidate.storage_key

        second = await worker.run_once(limit=10)
        assert second.claimed == 0
        assert second.tombstoned == 0
        assert second.maintenance_deleted == {"audit_events": 3}
        assert len(repository.tombstones) == 1
        assert len([item for item in events.items if item.resource_kind == "artifact"]) == 1

    asyncio.run(scenario())


async def _content(value: bytes) -> AsyncIterable[bytes]:
    yield value


class _FailingDeleteStorage(LocalArtifactStorage):
    async def delete(self, key: str) -> None:
        raise OSError(f"object store unavailable for {key}")


class _LostClaimRepository(_RetentionRepository):
    async def mark_tombstoned(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        deleted_at: datetime,
    ) -> bool:
        self.tombstones.append((artifact_id, claim_token, deleted_at))
        return False


class _FailingRetryRepository(_RetentionRepository):
    async def mark_failed(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> bool:
        raise RuntimeError("retry database unavailable")


def test_retention_worker_persists_retry_state_when_physical_delete_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        candidate = _candidate(artifact_id=2)
        repository = _RetentionRepository([candidate])
        worker = RetentionWorker(
            repository,
            _FailingDeleteStorage(tmp_path / "artifacts"),
            clock=lambda: NOW,
        )

        result = await worker.run_once()

        assert result.claimed == 1
        assert result.tombstoned == 0
        assert result.successful is False
        assert result.failures[0].operation == "artifact_delete"
        assert result.failures[0].resource_id == str(candidate.artifact_id)
        assert "object store unavailable" in result.failures[0].error
        assert repository.tombstones == []
        assert repository.failures == [
            (
                candidate.artifact_id,
                candidate.claim_token,
                NOW,
                f"object store unavailable for {candidate.storage_key}",
            )
        ]

    asyncio.run(scenario())


def test_retention_worker_reports_lost_claim_retry_and_event_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        lost_candidate = _candidate(artifact_id=3)
        lost_repository = _LostClaimRepository([lost_candidate])
        storage = LocalArtifactStorage(tmp_path / "artifacts")
        await storage.put(
            lost_candidate.storage_key,
            _content(b"expired"),
            maximum_size_bytes=100,
        )
        lost_result = await RetentionWorker(lost_repository, storage).run_once(now=NOW)
        assert lost_result.tombstoned == 0
        assert "claim was lost" in lost_result.failures[0].error
        assert lost_repository.failures[0][3] == ("retention claim was lost before tombstoning")

        retry_candidate = _candidate(artifact_id=4)
        retry_result = await RetentionWorker(
            _FailingRetryRepository([retry_candidate]),
            _FailingDeleteStorage(tmp_path / "failed-storage"),
        ).run_once(now=NOW)
        assert retry_result.tombstoned == 0
        assert "failed to persist retry state: retry database unavailable" in (
            retry_result.failures[0].error
        )

        event_candidate = _candidate(artifact_id=5)
        event_repository = _RetentionRepository([event_candidate])
        await storage.put(
            event_candidate.storage_key,
            _content(b"expired"),
            maximum_size_bytes=100,
        )
        event_result = await RetentionWorker(
            event_repository,
            storage,
            events=_FailingRetentionEvents(),
        ).run_once(now=NOW)
        assert event_result.tombstoned == 1
        assert event_result.failures[0].operation == "event_emit"
        assert event_result.failures[0].resource_id == str(event_candidate.artifact_id)

    asyncio.run(scenario())


def test_callable_maintenance_jobs_validate_results_and_worker_isolates_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def async_result(_now: datetime) -> MaintenanceResult:
            return MaintenanceResult("async", 2, {"cutoff": NOW.isoformat()})

        result = await CallableMaintenanceJob("async", async_result).run(NOW)
        assert result.deleted_count == 2
        assert result.metadata == {"cutoff": NOW.isoformat()}

        with pytest.raises(ValueError, match="does not match"):
            await CallableMaintenanceJob(
                "expected",
                lambda _now: MaintenanceResult("other", 1),
            ).run(NOW)
        with pytest.raises(TypeError, match="int or MaintenanceResult"):
            await CallableMaintenanceJob("bad", lambda _now: "invalid").run(NOW)  # type: ignore[arg-type,return-value]

        def explodes(_now: datetime) -> int:
            raise RuntimeError("maintenance unavailable")

        jobs = (
            CallableMaintenanceJob("zero", lambda _now: 0),
            CallableMaintenanceJob("negative", lambda _now: -1),
            CallableMaintenanceJob("explodes", explodes),
        )
        worker = RetentionWorker(
            _RetentionRepository([]),
            LocalArtifactStorage(tmp_path / "artifacts"),
            maintenance_jobs=jobs,
        )
        maintenance = await worker.run_once(now=NOW)
        assert maintenance.maintenance_deleted == {"zero": 0}
        assert [(item.operation, item.error) for item in maintenance.failures] == [
            ("negative", "maintenance deletion count cannot be negative"),
            ("explodes", "maintenance unavailable"),
        ]

        with pytest.raises(ValueError, match="batch size"):
            await worker.run_once(now=NOW, limit=0)
        with pytest.raises(ValueError, match="timezone-aware"):
            await worker.run_once(now=NOW.replace(tzinfo=None))

    asyncio.run(scenario())


class _ArtifactEvents:
    def __init__(self) -> None:
        self.items: list[EventRecord] = []

    async def create(self, event: EventRecord) -> EventRecord:
        self.items.append(event)
        return event


def test_artifact_service_applies_class_policy_and_audits_retention_deletion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SQLiteDatabase(tmp_path / "artifacts.db")
        database.initialize()
        repository = SQLiteGenericArtifactRepository(database)
        events = _ArtifactEvents()
        service = ArtifactService(
            repository,
            tmp_path / "artifacts",
            maximum_size_bytes=100,
            retention_policy=RetentionPolicy(
                default_days=30,
                failed_workflow_days=90,
                firmware_days=180,
                class_days={RetentionClass.JUNIT_REPORT: 7},
            ),
            events=events,
            clock=lambda: NOW,
        )
        try:
            firmware = await service.store_bytes(
                b"firmware",
                owner_type=ArtifactOwnerType.WORKFLOW_RUN,
                owner_id=UUID(int=300),
                name="firmware.bin",
                artifact_type="firmware",
            )
            failed_junit = await service.store_bytes(
                b"junit",
                owner_type=ArtifactOwnerType.WORKFLOW_RUN,
                owner_id=UUID(int=301),
                name="report.xml",
                artifact_type="junit",
                metadata={"workflow_status": "FAILED"},
            )
            assert firmware.expires_at == NOW + timedelta(days=180)
            assert failed_junit.expires_at == NOW + timedelta(days=90)

            assert (
                await service.expire_due(
                    now=NOW + timedelta(days=90),
                    limit=10,
                )
                == 1
            )
            assert [event.type for event in events.items].count("ARTIFACT_EXPIRED") == 1
            assert [event.type for event in events.items].count("RETENTION_DELETION") == 1
            deletion = next(event for event in events.items if event.type == "RETENTION_DELETION")
            assert deletion.payload["artifact_id"] == str(failed_junit.id)
            assert await repository.get(firmware.id) == firmware
            assert await repository.get(failed_junit.id) is None
        finally:
            database.close()

    asyncio.run(scenario())
