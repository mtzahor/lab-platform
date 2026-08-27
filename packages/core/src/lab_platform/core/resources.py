from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from uuid import UUID

from lab_platform.core.errors import PlatformError
from lab_platform.models.hardware import (
    BenchComposition,
    BenchResourceBinding,
    HardwareResource,
    ResourceHealthStatus,
    ResourceLock,
    ResourceLockOwnerType,
    ResourceSharingMode,
)


class HardwareResourceNotFoundError(PlatformError):
    code = "HARDWARE_RESOURCE_NOT_FOUND"


class BenchCompositionNotFoundError(PlatformError):
    code = "BENCH_COMPOSITION_NOT_FOUND"


class ResourceConflictError(PlatformError):
    code = "RESOURCE_CONFLICT"


class ResourceUnavailableError(PlatformError):
    code = "RESOURCE_UNAVAILABLE"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ResourceCatalog:
    """Authoritative in-memory catalog for physical resources and composed benches.

    Agents rebuild this catalog from plugin discovery. Control-plane persistence can
    mirror the immutable models without teaching the core about hardware vendors.
    """

    def __init__(self) -> None:
        self._resources: dict[str, HardwareResource] = {}
        self._benches: dict[str, BenchComposition] = {}

    def upsert_resource(self, resource: HardwareResource) -> HardwareResource:
        existing = self._resources.get(resource.id)
        if existing is not None and (
            existing.agent_id != resource.agent_id or existing.plugin != resource.plugin
        ):
            raise ResourceConflictError(
                f"Resource {resource.id!r} is already owned by another Agent or plugin.",
                resource_id=resource.id,
                existing_agent_id=str(existing.agent_id),
                incoming_agent_id=str(resource.agent_id),
            )
        self._resources[resource.id] = resource
        return resource

    def remove_resource(self, resource_id: str) -> HardwareResource | None:
        referenced_by = [
            bench.id
            for bench in self._benches.values()
            if any(binding.resource_id == resource_id for binding in bench.resources)
        ]
        if referenced_by:
            raise ResourceConflictError(
                f"Resource {resource_id!r} is still referenced by a composed bench.",
                resource_id=resource_id,
                bench_ids=sorted(referenced_by),
            )
        return self._resources.pop(resource_id, None)

    def get_resource(self, resource_id: str) -> HardwareResource:
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise HardwareResourceNotFoundError(
                f"Hardware resource {resource_id!r} was not found.",
                resource_id=resource_id,
            ) from exc

    def list_resources(
        self,
        *,
        agent_id: UUID | None = None,
        plugin: str | None = None,
        resource_type: str | None = None,
        capability: str | None = None,
    ) -> list[HardwareResource]:
        normalized_capability = capability.strip().casefold() if capability else None
        return [
            resource
            for resource in sorted(self._resources.values(), key=lambda item: item.id)
            if (agent_id is None or resource.agent_id == agent_id)
            and (plugin is None or resource.plugin == plugin)
            and (resource_type is None or resource.type == resource_type)
            and (normalized_capability is None or normalized_capability in resource.capabilities)
        ]

    def register_bench(self, composition: BenchComposition) -> BenchComposition:
        for binding in composition.resources:
            self._validate_binding(composition.id, binding)
        self._benches[composition.id] = composition
        return composition

    def remove_bench(self, bench_id: str) -> BenchComposition | None:
        return self._benches.pop(bench_id, None)

    def get_bench(self, bench_id: str) -> BenchComposition:
        try:
            return self._benches[bench_id]
        except KeyError as exc:
            raise BenchCompositionNotFoundError(
                f"Composed bench {bench_id!r} was not found.",
                bench_id=bench_id,
            ) from exc

    def list_benches(self) -> list[BenchComposition]:
        return [self._benches[key] for key in sorted(self._benches)]

    def resources_for_bench(self, bench_id: str) -> list[HardwareResource]:
        bench = self.get_bench(bench_id)
        return [self.get_resource(binding.resource_id) for binding in bench.resources]

    def capabilities_for_bench(self, bench_id: str) -> frozenset[str]:
        bench = self.get_bench(bench_id)
        capabilities: set[str] = set()
        for binding in bench.resources:
            resource = self.get_resource(binding.resource_id)
            capabilities.update(binding.capabilities or resource.capabilities)
        return frozenset(capabilities)

    def unavailable_resources(self, bench_id: str) -> list[HardwareResource]:
        bench = self.get_bench(bench_id)
        unavailable = {
            ResourceHealthStatus.UNHEALTHY,
            ResourceHealthStatus.OFFLINE,
        }
        return [
            resource
            for binding in bench.resources
            if binding.required
            and (resource := self.get_resource(binding.resource_id)).health in unavailable
        ]

    def require_available(self, bench_id: str) -> None:
        unavailable = self.unavailable_resources(bench_id)
        if unavailable:
            raise ResourceUnavailableError(
                f"Bench {bench_id!r} has unavailable required hardware resources: "
                + ", ".join(resource.id for resource in unavailable),
                bench_id=bench_id,
                resource_ids=[resource.id for resource in unavailable],
            )

    def _validate_binding(self, bench_id: str, binding: BenchResourceBinding) -> None:
        resource = self.get_resource(binding.resource_id)
        if resource.sharing is ResourceSharingMode.CHANNEL:
            if binding.channel is None:
                raise ResourceConflictError(
                    f"Bench {bench_id!r} must select a channel for resource {resource.id!r}.",
                    bench_id=bench_id,
                    resource_id=resource.id,
                )
            if binding.channel not in resource.channels:
                raise ResourceConflictError(
                    f"Resource {resource.id!r} does not expose channel {binding.channel!r}.",
                    bench_id=bench_id,
                    resource_id=resource.id,
                    channel=binding.channel,
                )
        elif binding.channel is not None:
            raise ResourceConflictError(
                f"Resource {resource.id!r} does not support channel bindings.",
                bench_id=bench_id,
                resource_id=resource.id,
                channel=binding.channel,
            )
        missing = binding.capabilities.difference(resource.capabilities)
        if missing:
            raise ResourceConflictError(
                f"Resource {resource.id!r} does not provide the requested capabilities: "
                + ", ".join(sorted(missing)),
                bench_id=bench_id,
                resource_id=resource.id,
                missing_capabilities=sorted(missing),
            )


class ResourceLockManager:
    """Atomic resource-level leases with channel isolation and fencing tokens."""

    def __init__(
        self,
        catalog: ResourceCatalog,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._catalog = catalog
        self._clock = clock
        self._mutex = asyncio.Lock()
        self._locks: dict[tuple[str, str | None], ResourceLock] = {}
        self._fencing_tokens: dict[tuple[str, str | None], int] = {}

    async def acquire_for_bench(
        self,
        bench_id: str,
        owner_type: ResourceLockOwnerType,
        owner_id: UUID,
        *,
        expires_at: datetime | None = None,
    ) -> tuple[ResourceLock, ...]:
        self._catalog.require_available(bench_id)
        composition = self._catalog.get_bench(bench_id)
        requested = self._lock_keys(composition.resources)
        now = self._clock()
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("resource lease expiry must be timezone-aware")
            expires_at = expires_at.astimezone(UTC)
            if expires_at <= now:
                raise ValueError("resource lease expiry must be in the future")

        async with self._mutex:
            self._expire_locked(now)
            conflicts = [
                lock
                for key in requested
                if (lock := self._locks.get(key)) is not None
                and (lock.owner_type != owner_type or lock.owner_id != owner_id)
            ]
            if conflicts:
                conflict = sorted(
                    conflicts,
                    key=lambda item: (item.resource_id, item.channel or ""),
                )[0]
                raise ResourceConflictError(
                    f"Hardware resource {conflict.resource_id!r} is already locked.",
                    bench_id=bench_id,
                    resource_id=conflict.resource_id,
                    channel=conflict.channel,
                    owner_type=conflict.owner_type.value,
                    owner_id=str(conflict.owner_id),
                )

            acquired: list[ResourceLock] = []
            for key in requested:
                current = self._locks.get(key)
                if current is not None:
                    if current.expires_at != expires_at:
                        current = current.model_copy(update={"expires_at": expires_at})
                        self._locks[key] = current
                    acquired.append(current)
                    continue
                fencing_token = self._fencing_tokens.get(key, 0) + 1
                self._fencing_tokens[key] = fencing_token
                lock = ResourceLock(
                    resource_id=key[0],
                    channel=key[1],
                    owner_type=owner_type,
                    owner_id=owner_id,
                    acquired_at=now,
                    expires_at=expires_at,
                    fencing_token=fencing_token,
                )
                self._locks[key] = lock
                acquired.append(lock)
            return tuple(acquired)

    async def renew_owner(
        self,
        owner_type: ResourceLockOwnerType,
        owner_id: UUID,
        expires_at: datetime,
    ) -> tuple[ResourceLock, ...]:
        now = self._clock()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("resource lease expiry must be timezone-aware")
        normalized_expiry = expires_at.astimezone(UTC)
        if normalized_expiry <= now:
            raise ValueError("resource lease expiry must be in the future")
        async with self._mutex:
            self._expire_locked(now)
            renewed: list[ResourceLock] = []
            for key, lock in list(self._locks.items()):
                if lock.owner_type == owner_type and lock.owner_id == owner_id:
                    updated = lock.model_copy(update={"expires_at": normalized_expiry})
                    self._locks[key] = updated
                    renewed.append(updated)
            return tuple(sorted(renewed, key=lambda item: (item.resource_id, item.channel or "")))

    async def release_owner(
        self,
        owner_type: ResourceLockOwnerType,
        owner_id: UUID,
    ) -> tuple[ResourceLock, ...]:
        async with self._mutex:
            released = [
                lock
                for lock in self._locks.values()
                if lock.owner_type == owner_type and lock.owner_id == owner_id
            ]
            for lock in released:
                self._locks.pop((lock.resource_id, lock.channel), None)
            return tuple(sorted(released, key=lambda item: (item.resource_id, item.channel or "")))

    async def active_locks(self) -> tuple[ResourceLock, ...]:
        async with self._mutex:
            self._expire_locked(self._clock())
            return tuple(
                sorted(
                    self._locks.values(),
                    key=lambda item: (item.resource_id, item.channel or ""),
                )
            )

    async def recover_expired(self) -> tuple[ResourceLock, ...]:
        async with self._mutex:
            return self._expire_locked(self._clock())

    def _lock_keys(
        self,
        bindings: Iterable[BenchResourceBinding],
    ) -> tuple[tuple[str, str | None], ...]:
        keys: set[tuple[str, str | None]] = set()
        for binding in bindings:
            resource = self._catalog.get_resource(binding.resource_id)
            if resource.sharing is ResourceSharingMode.SHARED:
                continue
            channel = binding.channel if resource.sharing is ResourceSharingMode.CHANNEL else None
            keys.add((resource.id, channel))
        return tuple(sorted(keys, key=lambda item: (item[0], item[1] or "")))

    def _expire_locked(self, now: datetime) -> tuple[ResourceLock, ...]:
        expired = [
            lock
            for lock in self._locks.values()
            if lock.expires_at is not None and lock.expires_at <= now
        ]
        for lock in expired:
            self._locks.pop((lock.resource_id, lock.channel), None)
        return tuple(sorted(expired, key=lambda item: (item.resource_id, item.channel or "")))


class ResourceReservationCoordinator:
    """Reservation-facing adapter that acquires all bench resources atomically."""

    def __init__(self, locks: ResourceLockManager) -> None:
        self._locks = locks

    async def acquire(
        self,
        bench_id: str,
        reservation_id: UUID,
        *,
        expires_at: datetime | None,
    ) -> tuple[ResourceLock, ...]:
        return await self._locks.acquire_for_bench(
            bench_id,
            ResourceLockOwnerType.RESERVATION,
            reservation_id,
            expires_at=expires_at,
        )

    async def renew(
        self,
        reservation_id: UUID,
        *,
        expires_at: datetime,
    ) -> tuple[ResourceLock, ...]:
        return await self._locks.renew_owner(
            ResourceLockOwnerType.RESERVATION,
            reservation_id,
            expires_at,
        )

    async def release(self, reservation_id: UUID) -> tuple[ResourceLock, ...]:
        return await self._locks.release_owner(
            ResourceLockOwnerType.RESERVATION,
            reservation_id,
        )


__all__ = [
    "BenchCompositionNotFoundError",
    "HardwareResourceNotFoundError",
    "ResourceCatalog",
    "ResourceConflictError",
    "ResourceLockManager",
    "ResourceReservationCoordinator",
    "ResourceUnavailableError",
]
