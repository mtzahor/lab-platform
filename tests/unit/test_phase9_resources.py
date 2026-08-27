from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from lab_platform.core.resources import (
    ResourceCatalog,
    ResourceConflictError,
    ResourceLockManager,
    ResourceReservationCoordinator,
    ResourceUnavailableError,
)
from lab_platform.models.hardware import (
    BenchComposition,
    BenchResourceBinding,
    HardwareResource,
    ResourceBindingRole,
    ResourceHealthStatus,
    ResourceLockOwnerType,
    ResourceSharingMode,
)
from pydantic import ValidationError

AGENT_ID = UUID("00000000-0000-0000-0000-000000000909")


def _resource(
    resource_id: str,
    *,
    capabilities: set[str],
    sharing: ResourceSharingMode = ResourceSharingMode.EXCLUSIVE,
    channels: set[str] | None = None,
    health: ResourceHealthStatus = ResourceHealthStatus.HEALTHY,
) -> HardwareResource:
    return HardwareResource(
        id=resource_id,
        agent_id=AGENT_ID,
        plugin="test-hardware",
        type="test",
        name=resource_id,
        health=health,
        capabilities=frozenset(capabilities),
        sharing=sharing,
        channels=frozenset(channels or set()),
    )


def _catalog() -> ResourceCatalog:
    catalog = ResourceCatalog()
    catalog.upsert_resource(_resource("target-1", capabilities={"probe", "flash", "serial"}))
    catalog.upsert_resource(
        _resource(
            "psu-1",
            capabilities={"power", "measure"},
            sharing=ResourceSharingMode.CHANNEL,
            channels={"1", "2"},
        )
    )
    catalog.upsert_resource(
        _resource(
            "can-1",
            capabilities={"can", "capture"},
            sharing=ResourceSharingMode.SHARED,
        )
    )
    return catalog


def _bench(bench_id: str, channel: str) -> BenchComposition:
    return BenchComposition(
        id=bench_id,
        name=bench_id,
        resources=(
            BenchResourceBinding(role=ResourceBindingRole.TARGET, resource_id="target-1"),
            BenchResourceBinding(
                role=ResourceBindingRole.POWER,
                resource_id="psu-1",
                channel=channel,
                capabilities=frozenset({"power"}),
            ),
            BenchResourceBinding(role=ResourceBindingRole.CAN, resource_id="can-1"),
        ),
    )


def test_catalog_validates_composition_and_reports_capabilities() -> None:
    catalog = _catalog()
    catalog.register_bench(_bench("validation-1", "1"))

    assert catalog.capabilities_for_bench("validation-1") == {
        "probe",
        "flash",
        "serial",
        "power",
        "can",
        "capture",
    }
    assert [item.id for item in catalog.list_resources(capability="POWER")] == ["psu-1"]

    with pytest.raises(ResourceConflictError, match="does not expose channel"):
        catalog.register_bench(_bench("invalid-channel", "9"))


def test_resource_model_requires_channels_only_for_channel_sharing() -> None:
    with pytest.raises(ValidationError, match="must declare at least one channel"):
        _resource(
            "missing-channels",
            capabilities={"power"},
            sharing=ResourceSharingMode.CHANNEL,
        )
    with pytest.raises(ValidationError, match="only channel-shared"):
        _resource("exclusive", capabilities={"debug"}, channels={"1"})


def test_required_unhealthy_resources_make_a_bench_unavailable() -> None:
    catalog = ResourceCatalog()
    catalog.upsert_resource(
        _resource(
            "target-offline",
            capabilities={"probe"},
            health=ResourceHealthStatus.OFFLINE,
        )
    )
    catalog.register_bench(
        BenchComposition(
            id="offline-bench",
            name="Offline bench",
            resources=(
                BenchResourceBinding(
                    role=ResourceBindingRole.TARGET,
                    resource_id="target-offline",
                ),
            ),
        )
    )

    with pytest.raises(ResourceUnavailableError, match="target-offline"):
        catalog.require_available("offline-bench")


def test_resource_locks_are_atomic_channel_aware_and_fenced() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 24, tzinfo=UTC)
        current = now
        catalog = _catalog()
        catalog.register_bench(_bench("bench-a", "1"))
        catalog.upsert_resource(_resource("target-2", capabilities={"probe", "flash"}))
        catalog.register_bench(
            BenchComposition(
                id="bench-b",
                name="Bench B",
                resources=(
                    BenchResourceBinding(
                        role=ResourceBindingRole.TARGET,
                        resource_id="target-2",
                    ),
                    BenchResourceBinding(
                        role=ResourceBindingRole.POWER,
                        resource_id="psu-1",
                        channel="2",
                    ),
                    BenchResourceBinding(role=ResourceBindingRole.CAN, resource_id="can-1"),
                ),
            )
        )
        locks = ResourceLockManager(catalog, clock=lambda: current)
        first_owner = uuid4()
        second_owner = uuid4()

        first = await locks.acquire_for_bench(
            "bench-a",
            ResourceLockOwnerType.RESERVATION,
            first_owner,
            expires_at=now + timedelta(minutes=10),
        )
        second = await locks.acquire_for_bench(
            "bench-b",
            ResourceLockOwnerType.RESERVATION,
            second_owner,
            expires_at=now + timedelta(minutes=10),
        )
        assert {(item.resource_id, item.channel) for item in first} == {
            ("target-1", None),
            ("psu-1", "1"),
        }
        assert {(item.resource_id, item.channel) for item in second} == {
            ("target-2", None),
            ("psu-1", "2"),
        }

        with pytest.raises(ResourceConflictError, match="already locked"):
            await locks.acquire_for_bench(
                "bench-a",
                ResourceLockOwnerType.WORKFLOW,
                uuid4(),
            )
        assert len(await locks.active_locks()) == 4

        released = await locks.release_owner(
            ResourceLockOwnerType.RESERVATION,
            first_owner,
        )
        assert len(released) == 2
        replacement = await locks.acquire_for_bench(
            "bench-a",
            ResourceLockOwnerType.WORKFLOW,
            uuid4(),
        )
        assert {item.fencing_token for item in replacement} == {2}

    asyncio.run(scenario())


def test_reservation_coordinator_renews_and_recovers_expired_locks() -> None:
    async def scenario() -> None:
        current = datetime(2026, 8, 24, tzinfo=UTC)
        catalog = _catalog()
        catalog.register_bench(_bench("bench-a", "1"))
        manager = ResourceLockManager(catalog, clock=lambda: current)
        coordinator = ResourceReservationCoordinator(manager)
        reservation_id = uuid4()

        acquired = await coordinator.acquire(
            "bench-a",
            reservation_id,
            expires_at=current + timedelta(minutes=5),
        )
        assert len(acquired) == 2
        renewed = await coordinator.renew(
            reservation_id,
            expires_at=current + timedelta(minutes=10),
        )
        assert all(item.expires_at == current + timedelta(minutes=10) for item in renewed)

        current += timedelta(minutes=11)
        expired = await manager.recover_expired()
        assert len(expired) == 2
        assert await manager.active_locks() == ()

    asyncio.run(scenario())
