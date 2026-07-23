from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, overload

from lab_platform.core.bench_catalog import BenchMetadata, BenchRecord
from lab_platform.core.errors import BenchNotFoundError
from lab_platform.models import HealthStatus
from lab_platform.persistence.database import SQLiteDatabase
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BackendRegistration(BaseModel):
    """Persisted definition of one configured backend instance."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class SQLiteCatalogRepository:
    """Durable backend registry definitions and authoritative bench inventory.

    Backend refreshes own connectivity, health, capabilities, and the current
    backend-provided name. Labels are platform-owned: once a bench exists, a
    snapshot upsert never replaces its stored labels. Explicit metadata writes
    are the only path that changes those labels.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    @overload
    async def upsert_backend(self, backend: BackendRegistration) -> BackendRegistration: ...

    @overload
    async def upsert_backend(
        self,
        backend: str,
        backend_type: str,
        config: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> BackendRegistration: ...

    async def upsert_backend(
        self,
        backend: BackendRegistration | str,
        backend_type: str | None = None,
        config: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> BackendRegistration:
        """Insert a backend definition or update its type and configuration."""

        registration = _coerce_backend_registration(backend, backend_type, config, now)
        with self._database.transaction(immediate=True) as connection:
            _upsert_backend(connection, registration)
            row = connection.execute(
                "SELECT * FROM backend_registrations WHERE id = ?", (registration.id,)
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError(f"Backend registration {registration.id!r} was not persisted")
        return _backend_from_row(row)

    async def upsert_backends(
        self, registrations: Iterable[BackendRegistration]
    ) -> list[BackendRegistration]:
        """Atomically upsert a set of already-normalized backend definitions."""

        items = list(registrations)
        with self._database.transaction(immediate=True) as connection:
            for registration in items:
                _upsert_backend(connection, registration)
            rows = connection.execute("SELECT * FROM backend_registrations ORDER BY id").fetchall()
        return [_backend_from_row(row) for row in rows]

    async def get_backend(self, backend_id: str) -> BackendRegistration | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM backend_registrations WHERE id = ?", (backend_id,)
            ).fetchone()
        return _backend_from_row(row) if row is not None else None

    async def list_backends(self) -> list[BackendRegistration]:
        with self._database.transaction() as connection:
            rows = connection.execute("SELECT * FROM backend_registrations ORDER BY id").fetchall()
        return [_backend_from_row(row) for row in rows]

    async def upsert_record(self, record: BenchRecord) -> BenchRecord:
        """Upsert one backend snapshot while retaining existing platform labels."""

        with self._database.transaction(immediate=True) as connection:
            _upsert_record(connection, record)
            row = connection.execute(
                "SELECT * FROM bench_catalog WHERE id = ?", (record.id,)
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError(f"Bench record {record.id!r} was not persisted")
        return _record_from_row(row)

    async def upsert_records(self, records: Iterable[BenchRecord]) -> list[BenchRecord]:
        """Atomically upsert catalog snapshots without reconciling absent benches."""

        items = list(records)
        with self._database.transaction(immediate=True) as connection:
            for record in items:
                _upsert_record(connection, record)
            rows = _select_records(connection)
        return [_record_from_row(row) for row in rows]

    async def reconcile(
        self,
        records: Iterable[BenchRecord],
        *,
        reconciled_backend_ids: Iterable[str] = (),
        observed_at: datetime | None = None,
    ) -> list[BenchRecord]:
        """Persist a refresh and mark missing benches from reconciled backends offline.

        ``reconciled_backend_ids`` should include both successfully refreshed and
        failed backend IDs. This matches the in-memory catalog rule: known benches
        remain addressable, but disappearances and transient failures make them
        offline without changing ``last_seen_at`` or platform metadata.
        """

        items = list(records)
        backend_ids = sorted(set(reconciled_backend_ids))
        seen_ids = {record.id for record in items}
        timestamp = _as_utc(observed_at or datetime.now(UTC)).isoformat()
        with self._database.transaction(immediate=True) as connection:
            for record in items:
                _upsert_record(connection, record)
            if backend_ids:
                backend_placeholders = ", ".join("?" for _ in backend_ids)
                values: list[object] = [timestamp, *backend_ids]
                unseen_clause = ""
                if seen_ids:
                    seen_placeholders = ", ".join("?" for _ in seen_ids)
                    unseen_clause = f" AND id NOT IN ({seen_placeholders})"
                    values.extend(sorted(seen_ids))
                connection.execute(
                    "UPDATE bench_catalog SET online = 0, health = 'unhealthy', "
                    "updated_at = ? "
                    f"WHERE backend_id IN ({backend_placeholders})"  # noqa: S608
                    f"{unseen_clause} AND (online != 0 OR health != 'unhealthy')",
                    values,
                )
            rows = _select_records(connection)
        return [_record_from_row(row) for row in rows]

    async def get_record(self, bench_id: str) -> BenchRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM bench_catalog WHERE id = ?", (bench_id,)
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def load_records(self) -> list[BenchRecord]:
        """Load the prior catalog in stable bench-ID order for restart hydration."""

        return await self.list_records()

    async def list_records(
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
        """Query durable records using the same filters as the in-memory catalog."""

        required_capabilities = {item.casefold() for item in capabilities}
        if capability is not None:
            required_capabilities.add(capability.casefold())
        required_labels = labels or {}
        with self._database.transaction() as connection:
            rows = _select_records(connection)

        records: list[BenchRecord] = []
        for row in rows:
            record = _record_from_row(row)
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
        return records

    async def save_metadata(
        self,
        bench_id: str,
        metadata: BenchMetadata,
        *,
        updated_at: datetime | None = None,
    ) -> BenchMetadata:
        """Replace the platform-owned metadata for an existing bench."""

        timestamp = _as_utc(updated_at or datetime.now(UTC)).isoformat()
        labels_json = _dump_json(dict(metadata.labels))
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT name, target_type FROM bench_catalog WHERE id = ?", (bench_id,)
            ).fetchone()
            if row is None:
                raise BenchNotFoundError(
                    f"Bench {bench_id} does not exist in the catalog.", bench_id=bench_id
                )
            effective_name = metadata.name if metadata.name is not None else row["name"]
            effective_target_type = (
                metadata.target_type if metadata.target_type is not None else row["target_type"]
            )
            connection.execute(
                "UPDATE bench_catalog SET name = ?, target_type = ?, labels_json = ?, "
                "metadata_name = ?, metadata_target_type = ?, updated_at = ? WHERE id = ?",
                (
                    effective_name,
                    effective_target_type,
                    labels_json,
                    metadata.name,
                    metadata.target_type,
                    timestamp,
                    bench_id,
                ),
            )
        return metadata

    async def set_metadata(
        self,
        bench_id: str,
        *,
        name: str | None = None,
        target_type: str | None = None,
        labels: Mapping[str, str] | None = None,
        replace_labels: bool = False,
        updated_at: datetime | None = None,
    ) -> BenchMetadata:
        """Merge metadata using the same semantics as ``BenchCatalog.set_metadata``."""

        current = (await self.load_metadata()).get(bench_id)
        if current is None:
            if await self.get_record(bench_id) is None:
                raise BenchNotFoundError(
                    f"Bench {bench_id} does not exist in the catalog.", bench_id=bench_id
                )
            current = BenchMetadata()
        updated_labels = {} if replace_labels else dict(current.labels)
        if labels is not None:
            updated_labels.update(labels)
        metadata = BenchMetadata(
            name=name if name is not None else current.name,
            target_type=(target_type if target_type is not None else current.target_type),
            labels=updated_labels,
        )
        return await self.save_metadata(bench_id, metadata, updated_at=updated_at)

    async def load_metadata(self) -> dict[str, BenchMetadata]:
        """Load platform metadata for injecting into ``BenchCatalog`` on restart."""

        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT id, target_type, labels_json, metadata_name, "
                "metadata_target_type FROM bench_catalog ORDER BY id"
            ).fetchall()
        return {
            row["id"]: BenchMetadata(
                name=row["metadata_name"],
                target_type=row["metadata_target_type"] or row["target_type"],
                labels=_load_labels(row["labels_json"]),
            )
            for row in rows
        }


# The longer name is retained as a discoverable compatibility alias for callers
# that distinguish this repository from other future catalog implementations.
SQLiteBenchCatalogRepository = SQLiteCatalogRepository


def _coerce_backend_registration(
    backend: BackendRegistration | str,
    backend_type: str | None,
    config: Mapping[str, Any] | None,
    now: datetime | None,
) -> BackendRegistration:
    if isinstance(backend, BackendRegistration):
        if backend_type is not None or config is not None or now is not None:
            raise TypeError("A BackendRegistration cannot be combined with backend arguments")
        return backend
    if backend_type is None:
        raise TypeError("backend_type is required when backend is an ID")
    timestamp = _as_utc(now or datetime.now(UTC))
    return BackendRegistration(
        id=backend,
        type=backend_type,
        config=dict(config or {}),
        created_at=timestamp,
        updated_at=timestamp,
    )


def _upsert_backend(connection: sqlite3.Connection, registration: BackendRegistration) -> None:
    connection.execute(
        "INSERT INTO backend_registrations "
        "(id, type, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET type = excluded.type, "
        "config_json = excluded.config_json, updated_at = excluded.updated_at",
        (
            registration.id,
            registration.type,
            _dump_json(registration.config),
            _as_utc(registration.created_at).isoformat(),
            _as_utc(registration.updated_at).isoformat(),
        ),
    )


def _upsert_record(connection: sqlite3.Connection, record: BenchRecord) -> None:
    connection.execute(
        "INSERT INTO bench_catalog "
        "(id, backend_id, name, target_type, online, health, capabilities_json, "
        "labels_json, last_seen_at, created_at, updated_at, metadata_name, "
        "metadata_target_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
        "ON CONFLICT(id) DO UPDATE SET backend_id = excluded.backend_id, "
        "name = COALESCE(bench_catalog.metadata_name, excluded.name), "
        "target_type = COALESCE(bench_catalog.metadata_target_type, excluded.target_type), "
        "online = excluded.online, health = excluded.health, "
        "capabilities_json = excluded.capabilities_json, "
        "labels_json = bench_catalog.labels_json, last_seen_at = excluded.last_seen_at, "
        "created_at = bench_catalog.created_at, updated_at = excluded.updated_at",
        (
            record.id,
            record.backend_id,
            record.name,
            record.target_type,
            int(record.online),
            record.health.value,
            _dump_json(sorted(record.capabilities)),
            _dump_json(dict(record.labels)),
            _datetime_value(record.last_seen_at),
            _as_utc(record.created_at).isoformat(),
            _as_utc(record.updated_at).isoformat(),
        ),
    )


def _select_records(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM bench_catalog ORDER BY id").fetchall()


def _backend_from_row(row: sqlite3.Row) -> BackendRegistration:
    config = json.loads(row["config_json"])
    if not isinstance(config, dict):  # pragma: no cover - protected by schema constraint
        raise ValueError("Persisted backend configuration must be a JSON object")
    return BackendRegistration(
        id=row["id"],
        type=row["type"],
        config=config,
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _record_from_row(row: sqlite3.Row) -> BenchRecord:
    capabilities = json.loads(row["capabilities_json"])
    if not isinstance(capabilities, list):  # pragma: no cover - protected by schema constraint
        raise ValueError("Persisted capabilities must be a JSON array")
    return BenchRecord(
        id=row["id"],
        backend_id=row["backend_id"],
        name=row["name"],
        target_type=row["target_type"],
        online=bool(row["online"]),
        health=HealthStatus(row["health"]),
        capabilities=set(capabilities),
        labels=_load_labels(row["labels_json"]),
        last_seen_at=_parse_optional_datetime(row["last_seen_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _load_labels(value: str) -> dict[str, str]:
    labels = json.loads(value)
    if not isinstance(labels, dict):  # pragma: no cover - protected by schema constraint
        raise ValueError("Persisted labels must be a JSON object")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items()):
        raise ValueError("Persisted labels must map strings to strings")
    return labels


def _dump_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _datetime_value(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Persisted catalog timestamps must be timezone-aware")
    return value.astimezone(UTC)
