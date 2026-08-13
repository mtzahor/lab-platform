from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from lab_platform.agent_protocol import (
    AgentBenchSnapshot,
    BenchAddedPayload,
    BenchConnectivity,
    BenchHealth,
    BenchHealthChangedPayload,
    BenchKind,
    BenchRemovedPayload,
    BenchSnapshotPayload,
)
from lab_platform.control_plane_core.errors import (
    AgentAuthenticationFailedError,
    AgentNotFoundError,
    AgentRevokedError,
    BenchAgentMismatchError,
    GlobalBenchIdConflictError,
    InventorySyncFailedError,
)
from lab_platform.core.errors import BenchNotFoundError
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
)


@dataclass(frozen=True, slots=True)
class InventoryReconciliation:
    benches: tuple[GlobalBenchRecord, ...]
    added_ids: frozenset[str]
    updated_ids: frozenset[str]
    offline_ids: frozenset[str]


class InventoryRepository(Protocol):
    """Atomic persistence boundary for one Agent-scoped inventory snapshot."""

    async def get(self, bench_id: str) -> GlobalBenchRecord | None: ...

    async def list(self) -> list[GlobalBenchRecord]: ...

    async def reconcile_agent_snapshot(
        self,
        agent: AgentRecord,
        benches: tuple[GlobalBenchRecord, ...],
        *,
        observed_at: datetime,
        snapshot_id: UUID | None = None,
        boot_id: UUID | None = None,
        generated_at: datetime | None = None,
    ) -> InventoryReconciliation: ...

    async def mark_agent_offline(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
    ) -> tuple[GlobalBenchRecord, ...]: ...


class InMemoryInventoryRepository:
    """Atomic in-memory inventory used by service and distributed tests."""

    def __init__(self, benches: Iterable[GlobalBenchRecord] = ()) -> None:
        self._benches: dict[str, GlobalBenchRecord] = {}
        self._agent_slugs: dict[UUID, str] = {}
        self._agent_ids_by_slug: dict[str, UUID] = {}
        for bench in benches:
            self._seed(bench)

    async def get(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord | None:
        bench = self._benches.get(bench_id)
        if bench is not None and (
            organisation_id is None or bench.organisation_id == organisation_id
        ):
            return bench
        return None

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
    ) -> list[GlobalBenchRecord]:
        return sorted(
            (
                bench
                for bench in self._benches.values()
                if organisation_id is None or bench.organisation_id == organisation_id
            ),
            key=lambda bench: bench.id,
        )

    async def reconcile_agent_snapshot(
        self,
        agent: AgentRecord,
        benches: tuple[GlobalBenchRecord, ...],
        *,
        observed_at: datetime,
        snapshot_id: UUID | None = None,
        boot_id: UUID | None = None,
        generated_at: datetime | None = None,
    ) -> InventoryReconciliation:
        now = _as_utc(observed_at, field="inventory reconciliation timestamp")
        _validate_agent_identity(self._agent_slugs, self._agent_ids_by_slug, agent)

        incoming: dict[str, GlobalBenchRecord] = {}
        for bench in benches:
            _require_bench_owner(bench, agent)
            if bench.id in incoming:
                raise GlobalBenchIdConflictError(
                    "Inventory snapshot contains a duplicate global bench ID.",
                    bench_id=bench.id,
                    agent_id=str(agent.id),
                )
            existing = self._benches.get(bench.id)
            if existing is not None and existing.agent_id != agent.id:
                raise BenchAgentMismatchError(
                    "Global bench ID is already owned by another Agent.",
                    bench_id=bench.id,
                    expected_agent_id=str(existing.agent_id),
                    received_agent_id=str(agent.id),
                )
            incoming[bench.id] = bench

        # Stage every change first. The identity maps and live records are changed
        # only after all conflict checks have succeeded.
        staged = dict(self._benches)
        added: set[str] = set()
        updated: set[str] = set()
        offlined: set[str] = set()
        for bench_id, received in incoming.items():
            existing = staged.get(bench_id)
            effective_now = max(
                now,
                received.updated_at,
                existing.updated_at if existing is not None else received.created_at,
            )
            candidate = received.model_copy(
                update={
                    "created_at": existing.created_at if existing is not None else effective_now,
                    "updated_at": effective_now,
                    "last_seen_at": effective_now,
                }
            )
            staged[bench_id] = candidate
            if existing is None:
                added.add(bench_id)
            elif candidate != existing:
                updated.add(bench_id)

        incoming_ids = set(incoming)
        for bench_id, existing in tuple(staged.items()):
            if existing.agent_id != agent.id or bench_id in incoming_ids:
                continue
            if existing.status is GlobalBenchStatus.OFFLINE:
                continue
            effective_now = max(now, existing.updated_at)
            staged[bench_id] = existing.model_copy(
                update={
                    "status": GlobalBenchStatus.OFFLINE,
                    "updated_at": effective_now,
                }
            )
            offlined.add(bench_id)

        self._benches = staged
        self._agent_slugs[agent.id] = agent.slug
        self._agent_ids_by_slug[agent.slug] = agent.id
        scoped = tuple(
            sorted(
                (bench for bench in staged.values() if bench.agent_id == agent.id),
                key=lambda bench: bench.id,
            )
        )
        return InventoryReconciliation(
            benches=scoped,
            added_ids=frozenset(added),
            updated_ids=frozenset(updated),
            offline_ids=frozenset(offlined),
        )

    async def mark_agent_offline(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
    ) -> tuple[GlobalBenchRecord, ...]:
        now = _as_utc(observed_at, field="Agent offline inventory timestamp")
        changed: list[GlobalBenchRecord] = []
        staged = dict(self._benches)
        for bench_id, existing in staged.items():
            if existing.agent_id != agent_id or existing.status is GlobalBenchStatus.OFFLINE:
                continue
            updated = existing.model_copy(
                update={
                    "status": GlobalBenchStatus.OFFLINE,
                    "updated_at": max(now, existing.updated_at),
                }
            )
            staged[bench_id] = updated
            changed.append(updated)
        self._benches = staged
        return tuple(sorted(changed, key=lambda bench: bench.id))

    def _seed(self, bench: GlobalBenchRecord) -> None:
        if bench.id in self._benches:
            raise GlobalBenchIdConflictError(
                "Global bench ID is already registered.",
                bench_id=bench.id,
            )
        agent = _inventory_agent_identity(bench)
        _validate_agent_identity(self._agent_slugs, self._agent_ids_by_slug, agent)
        self._benches[bench.id] = bench
        self._agent_slugs[bench.agent_id] = bench.agent_slug
        self._agent_ids_by_slug[bench.agent_slug] = bench.agent_id


class InventoryService:
    """Reconcile Agent-authoritative snapshots into one retained global inventory."""

    def __init__(self, repository: InventoryRepository) -> None:
        self._repository = repository

    async def reconcile_snapshot(
        self,
        agent: AgentRecord,
        snapshot: BenchSnapshotPayload,
        *,
        observed_at: datetime,
        expected_boot_id: UUID | None = None,
    ) -> InventoryReconciliation:
        if agent.status is AgentStatus.REVOKED:
            raise AgentRevokedError("The Agent has been revoked.", agent_id=str(agent.id))
        if agent.status is AgentStatus.PENDING:
            raise AgentNotFoundError("The Agent is not enrolled.", agent_id=str(agent.id))
        if agent.enrollment_status is not EnrollmentStatus.ENROLLED:
            raise AgentAuthenticationFailedError("Agent authentication failed.")
        if expected_boot_id is not None and snapshot.boot_id != expected_boot_id:
            raise InventorySyncFailedError(
                "Inventory snapshot boot ID does not match the active connection.",
                agent_id=str(agent.id),
                expected_boot_id=str(expected_boot_id),
                received_boot_id=str(snapshot.boot_id),
            )
        now = _as_utc(observed_at, field="inventory snapshot receipt timestamp")
        benches = tuple(_global_bench(agent, bench, now) for bench in snapshot.benches)
        return await self._repository.reconcile_agent_snapshot(
            agent,
            benches,
            observed_at=now,
            snapshot_id=uuid4(),
            boot_id=snapshot.boot_id,
            generated_at=snapshot.generated_at,
        )

    async def mark_agent_offline(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
    ) -> tuple[GlobalBenchRecord, ...]:
        return await self._repository.mark_agent_offline(
            agent_id,
            observed_at=_as_utc(observed_at, field="Agent offline timestamp"),
        )

    async def apply_bench_added(
        self,
        agent: AgentRecord,
        payload: BenchAddedPayload,
        *,
        observed_at: datetime,
        expected_boot_id: UUID,
    ) -> InventoryReconciliation:
        self._require_incremental_boot(payload.boot_id, expected_boot_id, agent.id)
        now = _as_utc(observed_at, field="bench addition receipt timestamp")
        existing = [bench for bench in await self._repository.list() if bench.agent_id == agent.id]
        incoming = _global_bench(agent, payload.bench, now)
        scoped = tuple(bench for bench in existing if bench.id != incoming.id) + (incoming,)
        return await self._repository.reconcile_agent_snapshot(agent, scoped, observed_at=now)

    async def apply_bench_removed(
        self,
        agent: AgentRecord,
        payload: BenchRemovedPayload,
        *,
        observed_at: datetime,
        expected_boot_id: UUID,
    ) -> InventoryReconciliation:
        self._require_incremental_boot(payload.boot_id, expected_boot_id, agent.id)
        now = _as_utc(observed_at, field="bench removal receipt timestamp")
        removed_id = f"{agent.slug}/{payload.local_bench_id}"
        existing = tuple(
            bench
            for bench in await self._repository.list()
            if bench.agent_id == agent.id and bench.id != removed_id
        )
        return await self._repository.reconcile_agent_snapshot(agent, existing, observed_at=now)

    async def apply_bench_health_changed(
        self,
        agent: AgentRecord,
        payload: BenchHealthChangedPayload,
        *,
        observed_at: datetime,
        expected_boot_id: UUID,
    ) -> InventoryReconciliation:
        self._require_incremental_boot(payload.boot_id, expected_boot_id, agent.id)
        now = _as_utc(observed_at, field="bench health receipt timestamp")
        bench_id = f"{agent.slug}/{payload.local_bench_id}"
        benches = [bench for bench in await self._repository.list() if bench.agent_id == agent.id]
        current = next((bench for bench in benches if bench.id == bench_id), None)
        if current is None:
            raise BenchNotFoundError(
                "Incremental health update refers to an unknown global bench.",
                bench_id=bench_id,
            )
        status = {
            BenchConnectivity.ONLINE: GlobalBenchStatus.ONLINE,
            BenchConnectivity.DEGRADED: GlobalBenchStatus.DEGRADED,
            BenchConnectivity.OFFLINE: GlobalBenchStatus.OFFLINE,
            BenchConnectivity.UNKNOWN: GlobalBenchStatus.OFFLINE,
        }[payload.connectivity]
        health = {
            BenchHealth.HEALTHY: HealthStatus.HEALTHY,
            BenchHealth.WARNING: HealthStatus.WARNING,
            BenchHealth.UNHEALTHY: HealthStatus.UNHEALTHY,
        }[payload.health]
        updated = current.model_copy(
            update={
                "status": status,
                "health": health,
                "last_seen_at": now,
                "updated_at": max(now, current.updated_at),
            }
        )
        scoped = tuple(updated if bench.id == bench_id else bench for bench in benches)
        return await self._repository.reconcile_agent_snapshot(agent, scoped, observed_at=now)

    async def get_bench(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> GlobalBenchRecord:
        bench = (
            await self._repository.get(bench_id)
            if organisation_id is None
            else await self._repository.get(  # type: ignore[call-arg]
                bench_id,
                organisation_id=organisation_id,
            )
        )
        if bench is None:
            raise BenchNotFoundError(
                f"Bench {bench_id} does not exist in the global inventory.",
                bench_id=bench_id,
            )
        return bench

    async def list_benches(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        agent_slug: str | None = None,
        status: GlobalBenchStatus | None = None,
        health: HealthStatus | None = None,
        kind: GlobalBenchKind | None = None,
        capability: str | None = None,
        labels: Mapping[str, str] | None = None,
        online: bool | None = None,
    ) -> list[GlobalBenchRecord]:
        benches = (
            await self._repository.list()
            if organisation_id is None
            else await self._repository.list(  # type: ignore[call-arg]
                organisation_id=organisation_id
            )
        )
        if agent_id is not None:
            benches = [bench for bench in benches if bench.agent_id == agent_id]
        if agent_slug is not None:
            normalized_slug = agent_slug.strip().casefold()
            benches = [bench for bench in benches if bench.agent_slug == normalized_slug]
        if status is not None:
            benches = [bench for bench in benches if bench.status is status]
        if health is not None:
            benches = [bench for bench in benches if bench.health is health]
        if kind is not None:
            benches = [bench for bench in benches if bench.kind is kind]
        if capability is not None:
            normalized_capability = capability.strip().casefold()
            benches = [
                bench
                for bench in benches
                if normalized_capability in {item.casefold() for item in bench.capabilities}
            ]
        for key, value in (labels or {}).items():
            benches = [bench for bench in benches if bench.labels.get(key) == value]
        if online is not None:
            benches = [bench for bench in benches if bench.online is online]
        return sorted(benches, key=lambda bench: bench.id)

    @staticmethod
    def _require_incremental_boot(
        received_boot_id: UUID,
        expected_boot_id: UUID,
        agent_id: UUID,
    ) -> None:
        if received_boot_id != expected_boot_id:
            raise InventorySyncFailedError(
                "Incremental inventory boot ID does not match the active connection.",
                agent_id=str(agent_id),
                expected_boot_id=str(expected_boot_id),
                received_boot_id=str(received_boot_id),
            )


def _global_bench(
    agent: AgentRecord,
    bench: AgentBenchSnapshot,
    observed_at: datetime,
) -> GlobalBenchRecord:
    status = {
        BenchConnectivity.ONLINE: GlobalBenchStatus.ONLINE,
        BenchConnectivity.DEGRADED: GlobalBenchStatus.DEGRADED,
        BenchConnectivity.OFFLINE: GlobalBenchStatus.OFFLINE,
        BenchConnectivity.UNKNOWN: GlobalBenchStatus.OFFLINE,
    }[bench.connectivity]
    health = {
        BenchHealth.HEALTHY: HealthStatus.HEALTHY,
        BenchHealth.WARNING: HealthStatus.WARNING,
        BenchHealth.UNHEALTHY: HealthStatus.UNHEALTHY,
    }[bench.health]
    kind = {
        BenchKind.SIMULATED: GlobalBenchKind.SIMULATED,
        BenchKind.PHYSICAL: GlobalBenchKind.PHYSICAL,
    }[bench.kind]
    return GlobalBenchRecord(
        id=f"{agent.slug}/{bench.local_bench_id}",
        organisation_id=agent.organisation_id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        local_bench_id=bench.local_bench_id,
        name=bench.name,
        backend_id=bench.backend_id,
        kind=kind,
        target_type=bench.target_type,
        status=status,
        health=health,
        capabilities=bench.capabilities,
        labels=dict(bench.labels),
        firmware_version=bench.firmware_version,
        last_seen_at=observed_at,
        created_at=observed_at,
        updated_at=observed_at,
    )


def _require_bench_owner(bench: GlobalBenchRecord, agent: AgentRecord) -> None:
    expected_id = f"{agent.slug}/{bench.local_bench_id}"
    if bench.agent_id != agent.id or bench.agent_slug != agent.slug or bench.id != expected_id:
        raise BenchAgentMismatchError(
            "Bench identity does not match the Agent-scoped inventory snapshot.",
            bench_id=bench.id,
            expected_bench_id=expected_id,
            expected_agent_id=str(agent.id),
            received_agent_id=str(bench.agent_id),
        )
    if bench.organisation_id != agent.organisation_id:
        raise BenchAgentMismatchError(
            "Bench organisation does not match its Agent.",
            bench_id=bench.id,
            expected_organisation_id=str(agent.organisation_id),
            received_organisation_id=str(bench.organisation_id),
        )


def _validate_agent_identity(
    agent_slugs: Mapping[UUID, str],
    agent_ids_by_slug: Mapping[str, UUID],
    agent: AgentRecord,
) -> None:
    known_slug = agent_slugs.get(agent.id)
    if known_slug is not None and known_slug != agent.slug:
        raise GlobalBenchIdConflictError(
            "An Agent ID cannot change the immutable slug used by global bench IDs.",
            agent_id=str(agent.id),
            expected_slug=known_slug,
            received_slug=agent.slug,
        )
    slug_owner = agent_ids_by_slug.get(agent.slug)
    if slug_owner is not None and slug_owner != agent.id:
        raise GlobalBenchIdConflictError(
            "The Agent slug is already used by another global inventory owner.",
            agent_slug=agent.slug,
            expected_agent_id=str(slug_owner),
            received_agent_id=str(agent.id),
        )


def _inventory_agent_identity(bench: GlobalBenchRecord) -> AgentRecord:
    # Only id and immutable slug are consulted by `_validate_agent_identity`.
    # The remaining valid fields deliberately carry no authority.
    return AgentRecord(
        id=bench.agent_id,
        organisation_id=bench.organisation_id,
        slug=bench.agent_slug,
        name=bench.agent_slug,
        status=AgentStatus.OFFLINE,
        version="inventory-seed",
        protocol_version="1.0",
        registered_at=bench.created_at,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
