from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from lab_platform.core.artifact_storage import ArtifactStorage


class RetentionClass(StrEnum):
    FIRMWARE = "firmware"
    SERIAL_LOG = "serial_log"
    JUNIT_REPORT = "junit_report"
    WORKFLOW_LOG = "workflow_log"
    DIAGNOSTIC_BUNDLE = "diagnostic_bundle"
    OTHER = "other"


class RetentionState(StrEnum):
    ACTIVE = "active"
    PENDING_DELETION = "pending_deletion"
    TOMBSTONED = "tombstoned"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    default_days: int | None = 30
    failed_workflow_days: int | None = 90
    firmware_days: int | None = 180
    class_days: Mapping[RetentionClass, int | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("default_days", self.default_days),
            ("failed_workflow_days", self.failed_workflow_days),
            ("firmware_days", self.firmware_days),
            *((f"class_days.{key.value}", days) for key, days in self.class_days.items()),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or null")

    def expires_at(
        self,
        created_at: datetime,
        retention_class: RetentionClass,
        *,
        failed_workflow: bool = False,
    ) -> datetime | None:
        created = _utc(created_at, field_name="artifact creation time")
        configured = self.class_days.get(retention_class)
        if retention_class not in self.class_days:
            configured = (
                self.firmware_days
                if retention_class is RetentionClass.FIRMWARE
                else self.default_days
            )
        durations = [days for days in (configured,) if days is not None]
        if failed_workflow and self.failed_workflow_days is not None:
            durations.append(self.failed_workflow_days)
        if not durations:
            return None
        return created + timedelta(days=max(durations))


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    artifact_id: UUID
    organisation_id: UUID
    storage_key: str
    retention_class: RetentionClass
    expires_at: datetime
    claim_token: UUID
    size_bytes: int = 0
    state: RetentionState = RetentionState.PENDING_DELETION
    attempt_count: int = 1

    def __post_init__(self) -> None:
        _utc(self.expires_at, field_name="artifact expiry time")
        if self.size_bytes < 0:
            raise ValueError("retained artifact size cannot be negative")
        if self.state is not RetentionState.PENDING_DELETION:
            raise ValueError("retention candidates must already be claimed for deletion")
        if self.attempt_count <= 0:
            raise ValueError("retention attempt count must be positive")


class RetentionRepository(Protocol):
    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        claim_ttl: timedelta,
    ) -> list[RetentionCandidate]:
        """Atomically claim expired, inactive resources, including abandoned claims."""

    async def mark_tombstoned(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        deleted_at: datetime,
    ) -> bool: ...

    async def mark_failed(
        self,
        artifact_id: UUID,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RetentionEvent:
    type: str
    occurred_at: datetime
    resource_kind: str
    resource_id: str | None
    organisation_id: UUID | None
    count: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)


class RetentionEventSink(Protocol):
    async def emit(self, event: RetentionEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    name: str
    deleted_count: int
    metadata: Mapping[str, object] = field(default_factory=dict)


class RetentionMaintenanceJob(Protocol):
    @property
    def name(self) -> str: ...

    async def run(self, now: datetime) -> MaintenanceResult: ...


@dataclass(frozen=True, slots=True)
class CallableMaintenanceJob:
    name: str
    callback: Callable[
        [datetime],
        int | MaintenanceResult | Awaitable[int | MaintenanceResult],
    ]

    async def run(self, now: datetime) -> MaintenanceResult:
        result = self.callback(now)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, MaintenanceResult):
            if result.name != self.name:
                raise ValueError("maintenance result name does not match its registered job")
            return result
        if not isinstance(result, int):
            raise TypeError("retention maintenance callbacks must return int or MaintenanceResult")
        return MaintenanceResult(name=self.name, deleted_count=result)


@dataclass(frozen=True, slots=True)
class RetentionFailure:
    operation: str
    resource_id: str | None
    error: str


@dataclass(frozen=True, slots=True)
class RetentionRunResult:
    claimed: int
    tombstoned: int
    deleted_bytes: int
    maintenance_deleted: Mapping[str, int]
    failures: tuple[RetentionFailure, ...]

    @property
    def successful(self) -> bool:
        return not self.failures


class RetentionWorker:
    """Crash-safe two-phase artifact and audit-retention maintenance.

    The repository owns eligibility and atomic claims, including exclusion of
    active resources. Object deletion is idempotent. If a worker dies after the
    object is removed but before the tombstone commits, a later claim repeats the
    harmless delete and completes the metadata transition.
    """

    def __init__(
        self,
        repository: RetentionRepository,
        storage: ArtifactStorage,
        *,
        events: RetentionEventSink | None = None,
        maintenance_jobs: Sequence[RetentionMaintenanceJob] = (),
        claim_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if claim_ttl <= timedelta(0):
            raise ValueError("retention claim TTL must be positive")
        names = [job.name for job in maintenance_jobs]
        if len(set(names)) != len(names):
            raise ValueError("retention maintenance job names must be unique")
        self._repository = repository
        self._storage = storage
        self._events = events
        self._maintenance_jobs = tuple(maintenance_jobs)
        self._claim_ttl = claim_ttl
        self._clock = clock

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> RetentionRunResult:
        if limit <= 0:
            raise ValueError("retention batch size must be positive")
        observed_at = _utc(now or self._clock(), field_name="retention run time")
        candidates = await self._repository.claim_due(
            now=observed_at,
            limit=limit,
            claim_ttl=self._claim_ttl,
        )
        failures: list[RetentionFailure] = []
        tombstoned = 0
        deleted_bytes = 0
        for candidate in candidates:
            try:
                await self._storage.delete(candidate.storage_key)
                committed = await self._repository.mark_tombstoned(
                    candidate.artifact_id,
                    claim_token=candidate.claim_token,
                    deleted_at=observed_at,
                )
                if not committed:
                    raise RuntimeError("retention claim was lost before tombstoning")
                tombstoned += 1
                deleted_bytes += candidate.size_bytes
                await self._emit(
                    RetentionEvent(
                        type="RETENTION_DELETION",
                        occurred_at=observed_at,
                        resource_kind="artifact",
                        resource_id=str(candidate.artifact_id),
                        organisation_id=candidate.organisation_id,
                        metadata={
                            "retention_class": candidate.retention_class.value,
                            "storage_key": candidate.storage_key,
                            "size_bytes": candidate.size_bytes,
                            "attempt_count": candidate.attempt_count,
                        },
                    ),
                    failures,
                )
            except Exception as exc:
                error = _bounded_error(exc)
                try:
                    await self._repository.mark_failed(
                        candidate.artifact_id,
                        claim_token=candidate.claim_token,
                        failed_at=observed_at,
                        error=error,
                    )
                except Exception as repository_exc:
                    retry_error = _bounded_error(repository_exc)
                    error = f"{error}; failed to persist retry state: {retry_error}"
                failures.append(
                    RetentionFailure(
                        operation="artifact_delete",
                        resource_id=str(candidate.artifact_id),
                        error=error,
                    )
                )

        maintenance_deleted: dict[str, int] = {}
        for job in self._maintenance_jobs:
            try:
                result = await job.run(observed_at)
                if result.deleted_count < 0:
                    raise ValueError("maintenance deletion count cannot be negative")
                maintenance_deleted[job.name] = result.deleted_count
                if result.deleted_count:
                    await self._emit(
                        RetentionEvent(
                            type="RETENTION_DELETION",
                            occurred_at=observed_at,
                            resource_kind=job.name,
                            resource_id=None,
                            organisation_id=None,
                            count=result.deleted_count,
                            metadata=result.metadata,
                        ),
                        failures,
                    )
            except Exception as exc:
                failures.append(
                    RetentionFailure(
                        operation=job.name,
                        resource_id=None,
                        error=_bounded_error(exc),
                    )
                )

        return RetentionRunResult(
            claimed=len(candidates),
            tombstoned=tombstoned,
            deleted_bytes=deleted_bytes,
            maintenance_deleted=maintenance_deleted,
            failures=tuple(failures),
        )

    async def _emit(
        self,
        event: RetentionEvent,
        failures: list[RetentionFailure],
    ) -> None:
        if self._events is None:
            return
        try:
            await self._events.emit(event)
        except Exception as exc:
            failures.append(
                RetentionFailure(
                    operation="event_emit",
                    resource_id=event.resource_id,
                    error=_bounded_error(exc),
                )
            )


def retention_class_for_artifact_type(artifact_type: str) -> RetentionClass:
    normalized = artifact_type.strip().casefold().replace("-", "_")
    aliases = {
        "firmware": RetentionClass.FIRMWARE,
        "firmware_binary": RetentionClass.FIRMWARE,
        "serial": RetentionClass.SERIAL_LOG,
        "serial_log": RetentionClass.SERIAL_LOG,
        "junit": RetentionClass.JUNIT_REPORT,
        "junit_xml": RetentionClass.JUNIT_REPORT,
        "junit_report": RetentionClass.JUNIT_REPORT,
        "workflow_log": RetentionClass.WORKFLOW_LOG,
        "flash_log": RetentionClass.WORKFLOW_LOG,
        "diagnostic": RetentionClass.DIAGNOSTIC_BUNDLE,
        "diagnostic_bundle": RetentionClass.DIAGNOSTIC_BUNDLE,
    }
    return aliases.get(normalized, RetentionClass.OTHER)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:1000]
