from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from lab_platform.core.backend_registry import BackendFailure, BackendRegistry
from lab_platform.core.clock import Clock, UtcClock, as_utc
from lab_platform.core.errors import BenchNotFoundError
from lab_platform.models import HealthStatus, TargetHealthStatus
from pydantic import BaseModel, ConfigDict, Field


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class BenchRecord(CatalogModel):
    id: str
    backend_id: str
    name: str
    target_type: str | None = None
    online: bool
    health: HealthStatus
    capabilities: set[str] = Field(default_factory=set)
    labels: dict[str, str] = Field(default_factory=dict)
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BenchMetadata(CatalogModel):
    """Platform-owned fields that backend inventory refreshes cannot replace."""

    name: str | None = None
    target_type: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    records: tuple[BenchRecord, ...]
    discovered_bench_ids: frozenset[str]
    seen_bench_ids: frozenset[str]
    offline_bench_ids: frozenset[str]
    backend_failures: tuple[BackendFailure, ...]


class BenchCatalog:
    """Authoritative in-memory catalog reconciled from a ``BackendRegistry``.

    Backend snapshots own connectivity, health, capabilities, and the default
    display name. ``BenchMetadata`` owns labels, target type, and an optional name
    override. Missing benches are retained and marked offline so identity and
    metadata survive temporary backend outages or device removal.
    """

    def __init__(
        self,
        registry: BackendRegistry,
        *,
        metadata: Mapping[str, BenchMetadata] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._metadata = dict(metadata or {})
        self._clock = clock or UtcClock()
        self._records: dict[str, BenchRecord] = {}
        self._probe_required: set[str] = set()

    def restore(
        self,
        records: Iterable[BenchRecord],
        *,
        metadata: Mapping[str, BenchMetadata] | None = None,
    ) -> None:
        """Hydrate durable identity and metadata before the first backend refresh."""

        self._records = {record.id: record for record in records}
        persisted = dict(metadata or {})
        configured = dict(self._metadata)
        self._metadata = persisted
        for bench_id, override in configured.items():
            previous = self._metadata.get(bench_id, BenchMetadata())
            self._metadata[bench_id] = BenchMetadata(
                name=override.name if override.name is not None else previous.name,
                target_type=(
                    override.target_type
                    if override.target_type is not None
                    else previous.target_type
                ),
                labels={**previous.labels, **override.labels},
            )

    async def refresh(self) -> CatalogRefreshResult:
        inventory = await self._registry.refresh()
        now = self._now()
        seen: set[str] = set()
        discovered: set[str] = set()

        for registered in inventory.benches:
            snapshot = registered.snapshot
            seen.add(snapshot.id)
            previous = self._records.get(snapshot.id)
            if previous is None:
                discovered.add(snapshot.id)
            metadata = self._metadata.get(snapshot.id, BenchMetadata())
            labels = metadata.labels if snapshot.id in self._metadata else {}
            if previous is not None and snapshot.id not in self._metadata:
                labels = previous.labels
            target_type = metadata.target_type
            if target_type is None and previous is not None:
                target_type = previous.target_type
            name = metadata.name or snapshot.name
            probe_required = snapshot.id in self._probe_required
            self._records[snapshot.id] = BenchRecord(
                id=snapshot.id,
                backend_id=registered.backend_id,
                name=name,
                target_type=target_type,
                online=snapshot.online and not probe_required,
                health=(
                    HealthStatus.WARNING
                    if probe_required
                    else HealthStatus.HEALTHY
                    if snapshot.online
                    else HealthStatus.UNHEALTHY
                ),
                capabilities=set(snapshot.capabilities),
                labels=dict(labels),
                last_seen_at=now,
                created_at=previous.created_at if previous is not None else now,
                updated_at=now,
            )

        reconciled_backend_ids = inventory.refreshed_backend_ids | inventory.failed_backend_ids
        offline: set[str] = set()
        for bench_id, record in tuple(self._records.items()):
            if bench_id in seen or record.backend_id not in reconciled_backend_ids:
                continue
            offline.add(bench_id)
            if record.online or record.health is not HealthStatus.UNHEALTHY:
                self._records[bench_id] = record.model_copy(
                    update={
                        "online": False,
                        "health": HealthStatus.UNHEALTHY,
                        "updated_at": now,
                    }
                )

        return CatalogRefreshResult(
            records=tuple(self.list()),
            discovered_bench_ids=frozenset(discovered),
            seen_bench_ids=frozenset(seen),
            offline_bench_ids=frozenset(offline),
            backend_failures=inventory.failures,
        )

    def get(self, bench_id: str) -> BenchRecord:
        try:
            return self._records[bench_id]
        except KeyError as exc:
            raise BenchNotFoundError(
                f"Bench {bench_id} does not exist in the catalog.", bench_id=bench_id
            ) from exc

    def is_online(self, bench_id: str) -> bool:
        record = self._records.get(bench_id)
        return record is not None and record.online

    def requires_probe(self, bench_id: str) -> bool:
        return bench_id in self._probe_required

    def mark_probe_required(self, bench_id: str) -> None:
        record = self.get(bench_id)
        self._probe_required.add(bench_id)
        self._records[bench_id] = record.model_copy(
            update={
                "online": False,
                "health": HealthStatus.WARNING,
                "updated_at": self._now(),
            }
        )

    def clear_probe_required(self, bench_id: str) -> None:
        self._probe_required.discard(bench_id)

    def record_probe_result(
        self,
        bench_id: str,
        status: TargetHealthStatus,
    ) -> BenchRecord:
        """Apply a direct target probe before its maintenance lock is released."""

        record = self.get(bench_id)
        now = self._now()
        online = status is TargetHealthStatus.ONLINE
        if online:
            self._probe_required.discard(bench_id)
            health = HealthStatus.HEALTHY
        else:
            self._probe_required.add(bench_id)
            health = (
                HealthStatus.UNHEALTHY
                if status is TargetHealthStatus.OFFLINE
                else HealthStatus.WARNING
            )
        updated = record.model_copy(
            update={
                "online": online,
                "health": health,
                "last_seen_at": now if online else record.last_seen_at,
                "updated_at": now,
            }
        )
        self._records[bench_id] = updated
        return updated

    def list(
        self,
        *,
        online: bool | None = None,
        health: HealthStatus | None = None,
        backend_id: str | None = None,
        target_type: str | None = None,
        capability: str | None = None,
        capabilities: Iterable[str] = (),
        labels: Mapping[str, str] | None = None,
    ) -> list[BenchRecord]:
        required_capabilities = {item.casefold() for item in capabilities}
        if capability is not None:
            required_capabilities.add(capability.casefold())
        required_labels = labels or {}
        records: list[BenchRecord] = []
        for record in self._records.values():
            available_capabilities = {item.casefold() for item in record.capabilities}
            if online is not None and record.online is not online:
                continue
            if health is not None and record.health is not health:
                continue
            if backend_id is not None and record.backend_id != backend_id:
                continue
            if target_type is not None and (
                record.target_type is None
                or record.target_type.casefold() != target_type.casefold()
            ):
                continue
            if not required_capabilities.issubset(available_capabilities):
                continue
            if any(record.labels.get(key) != value for key, value in required_labels.items()):
                continue
            records.append(record)
        return sorted(records, key=lambda record: record.id)

    def set_metadata(
        self,
        bench_id: str,
        *,
        name: str | None = None,
        target_type: str | None = None,
        labels: Mapping[str, str] | None = None,
        replace_labels: bool = False,
    ) -> BenchMetadata:
        current = self._metadata.get(bench_id, BenchMetadata())
        updated_labels = {} if replace_labels else dict(current.labels)
        if labels is not None:
            updated_labels.update(labels)
        metadata = BenchMetadata(
            name=name if name is not None else current.name,
            target_type=target_type if target_type is not None else current.target_type,
            labels=updated_labels,
        )
        self._metadata[bench_id] = metadata

        record = self._records.get(bench_id)
        if record is not None:
            changes: dict[str, object] = {
                "labels": dict(metadata.labels),
                "target_type": metadata.target_type,
                "updated_at": self._now(),
            }
            if metadata.name is not None:
                changes["name"] = metadata.name
            self._records[bench_id] = record.model_copy(update=changes)
        return metadata

    def metadata_for(self, bench_id: str) -> BenchMetadata:
        return self._metadata.get(bench_id, BenchMetadata())

    def _now(self) -> datetime:
        return as_utc(self._clock.now())
