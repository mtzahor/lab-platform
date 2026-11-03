from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.agent_protocol import (
    AgentBenchSnapshot,
    BenchConnectivity,
    BenchHealth,
    BenchKind,
    BenchSnapshotPayload,
    ProtocolSequenceError,
)
from lab_platform.control_plane_core.errors import (
    AgentAuthenticationFailedError,
    AgentNotFoundError,
    AgentRevokedError,
    BenchAgentMismatchError,
    GlobalBenchIdConflictError,
    InventorySyncFailedError,
)
from lab_platform.control_plane_core.inventory import (
    InMemoryInventoryRepository,
    InventoryService,
)
from lab_platform.control_plane_core.presence import (
    AgentConnectionConflictError,
    AgentIdentityConflictError,
    AgentPresenceService,
    InMemoryAgentPresenceRepository,
    StaleAgentConnectionError,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
)

NOW = datetime(2026, 7, 28, 9, tzinfo=UTC)


def _agent(
    number: int,
    slug: str,
    *,
    status: AgentStatus = AgentStatus.OFFLINE,
    enrollment_status: EnrollmentStatus = EnrollmentStatus.ENROLLED,
    location: str | None = "Jerusalem",
) -> AgentRecord:
    revoked_at = NOW if status is AgentStatus.REVOKED else None
    return AgentRecord(
        id=UUID(int=number),
        slug=slug,
        name=f"Agent {number}",
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location=location,
        labels={"environment": "development", "number": str(number)},
        registered_at=NOW,
        enrollment_status=enrollment_status,
        revoked_at=revoked_at,
    )


def _bench(
    local_id: str,
    *,
    connectivity: BenchConnectivity = BenchConnectivity.ONLINE,
    health: BenchHealth = BenchHealth.HEALTHY,
    kind: BenchKind = BenchKind.SIMULATED,
    name: str | None = None,
    label: str = "test",
) -> AgentBenchSnapshot:
    return AgentBenchSnapshot(
        local_bench_id=local_id,
        name=name or local_id,
        backend_id="simlab",
        kind=kind,
        target_type="esp32",
        connectivity=connectivity,
        health=health,
        capabilities=frozenset({"probe", "firmware"}),
        labels={"purpose": label},
        firmware_version="1.2.3",
    )


def _snapshot(
    boot_id: UUID,
    *benches: AgentBenchSnapshot,
    generated_at: datetime = NOW,
) -> BenchSnapshotPayload:
    return BenchSnapshotPayload(
        boot_id=boot_id,
        generated_at=generated_at,
        benches=benches,
    )


def test_presence_registers_authenticated_connection_and_filters_agents() -> None:
    async def scenario() -> None:
        home = _agent(1, "home-lab")
        office = _agent(2, "office-lab", location="Tel Aviv")
        repository = InMemoryAgentPresenceRepository((office, home))
        service = AgentPresenceService(repository)
        connection_id = UUID(int=101)
        boot_id = UUID(int=201)

        registered = await service.register_authenticated_connection(
            home,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version="1.0",
            agent_version="0.6.1-alpha",
            sequence_number=1,
            observed_at=NOW + timedelta(seconds=1),
            observed_monotonic=10,
        )

        assert registered.agent.status is AgentStatus.ONLINE
        assert registered.agent.version == "0.6.1-alpha"
        assert registered.agent.last_connected_at == NOW + timedelta(seconds=1)
        assert registered.connection.agent_id == home.id
        assert registered.connection.last_sequence_number == 1
        assert registered.superseded_connection_id is None
        assert await service.require_current_connection(
            home.id,
            connection_id=connection_id,
            boot_id=boot_id,
        )
        assert [agent.id for agent in await service.list_agents()] == [home.id, office.id]
        assert await service.list_agents(status=AgentStatus.ONLINE) == [registered.agent]
        assert await service.list_agents(location="jerusalem") == [registered.agent]
        assert await service.list_agents(labels={"number": "2"}) == [office]
        assert await service.list_agents(version="0.6.1-alpha") == [registered.agent]

        repeated = await service.register_authenticated_connection(
            registered.agent,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version="1.0",
            sequence_number=1,
            observed_at=NOW + timedelta(seconds=2),
            observed_monotonic=11,
        )
        assert repeated.connection == registered.connection

    asyncio.run(scenario())


def test_presence_rejects_unregistered_unenrolled_revoked_and_conflicting_identity() -> None:
    async def scenario() -> None:
        enrolled = _agent(1, "home-lab")
        pending = _agent(
            2,
            "pending-lab",
            status=AgentStatus.PENDING,
            enrollment_status=EnrollmentStatus.PENDING,
        )
        revoked = _agent(
            3,
            "revoked-lab",
            status=AgentStatus.REVOKED,
            enrollment_status=EnrollmentStatus.REVOKED,
        )
        repository = InMemoryAgentPresenceRepository((enrolled, pending, revoked))
        service = AgentPresenceService(repository)

        async def connect(agent: AgentRecord, connection: int) -> None:
            await service.register_authenticated_connection(
                agent,
                connection_id=UUID(int=connection),
                boot_id=UUID(int=connection + 100),
                protocol_version="1.0",
                observed_at=NOW,
                observed_monotonic=1,
            )

        with pytest.raises(AgentNotFoundError):
            await connect(_agent(99, "missing"), 99)
        with pytest.raises(AgentAuthenticationFailedError):
            await connect(pending, 2)
        with pytest.raises(AgentRevokedError):
            await connect(revoked, 3)

        conflicting = enrolled.model_copy(update={"slug": "renamed-lab"})
        with pytest.raises(AgentIdentityConflictError):
            await connect(conflicting, 4)

        await connect(enrolled, 10)
        other = _agent(4, "other-lab")
        await repository.add_agent(other)
        with pytest.raises(AgentConnectionConflictError):
            await service.register_authenticated_connection(
                other,
                connection_id=UUID(int=10),
                boot_id=UUID(int=999),
                protocol_version="1.0",
                observed_at=NOW,
                observed_monotonic=2,
            )

        with pytest.raises(AgentIdentityConflictError):
            await repository.add_agent(_agent(5, "home-lab"))

    asyncio.run(scenario())


def test_new_connection_supersedes_old_and_fences_stale_updates() -> None:
    async def scenario() -> None:
        agent = _agent(1, "home-lab")
        repository = InMemoryAgentPresenceRepository((agent,))
        service = AgentPresenceService(repository)
        first_connection = UUID(int=101)
        first_boot = UUID(int=201)
        second_connection = UUID(int=102)
        second_boot = UUID(int=202)

        await service.register_authenticated_connection(
            agent,
            connection_id=first_connection,
            boot_id=first_boot,
            protocol_version="1.0",
            observed_at=NOW,
            observed_monotonic=1,
        )
        replacement = await service.register_authenticated_connection(
            await service.get_agent(agent.id),
            connection_id=second_connection,
            boot_id=second_boot,
            protocol_version="1.0",
            observed_at=NOW + timedelta(seconds=1),
            observed_monotonic=2,
        )

        assert replacement.superseded_connection_id == first_connection
        old = await repository.get_connection(first_connection)
        assert old is not None and old.connection.disconnected_at is not None
        assert not await service.disconnect(
            agent.id,
            connection_id=first_connection,
            boot_id=first_boot,
            observed_at=NOW + timedelta(seconds=2),
        )
        with pytest.raises(StaleAgentConnectionError):
            await service.record_heartbeat(
                agent.id,
                connection_id=first_connection,
                boot_id=first_boot,
                sequence_number=2,
                observed_at=NOW + timedelta(seconds=2),
                observed_monotonic=3,
            )
        current = await service.require_current_connection(
            agent.id,
            connection_id=second_connection,
            boot_id=second_boot,
        )
        assert current.connection.id == second_connection
        assert (await service.get_agent(agent.id)).status is AgentStatus.ONLINE

    asyncio.run(scenario())


def test_heartbeat_sequence_and_monotonic_liveness_drive_degraded_and_offline() -> None:
    async def scenario() -> None:
        agent = _agent(1, "home-lab")
        repository = InMemoryAgentPresenceRepository((agent,))
        service = AgentPresenceService(
            repository,
            heartbeat_timeout_seconds=45,
            offline_timeout_seconds=90,
        )
        connection_id = UUID(int=101)
        boot_id = UUID(int=201)
        await service.register_authenticated_connection(
            agent,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version="1.0",
            observed_at=NOW,
            observed_monotonic=0,
        )
        stale = await repository.get_active_connection(agent.id)
        assert stale is not None
        heartbeat = await service.record_heartbeat(
            agent.id,
            connection_id=connection_id,
            boot_id=boot_id,
            sequence_number=2,
            observed_at=NOW + timedelta(seconds=10),
            observed_monotonic=10,
            observed_clock_offset_seconds=0.25,
        )
        assert heartbeat.connection.last_sequence_number == 2
        assert heartbeat.connection.observed_clock_offset_seconds == 0.25
        assert not await repository.update_if_current(
            heartbeat.agent.model_copy(update={"status": AgentStatus.DEGRADED}),
            stale,
        )
        assert (await service.get_agent(agent.id)).status is AgentStatus.ONLINE

        with pytest.raises(ProtocolSequenceError):
            await service.record_heartbeat(
                agent.id,
                connection_id=connection_id,
                boot_id=boot_id,
                sequence_number=2,
                observed_at=NOW + timedelta(seconds=11),
                observed_monotonic=11,
            )
        with pytest.raises(ProtocolSequenceError):
            await service.record_heartbeat(
                agent.id,
                connection_id=connection_id,
                boot_id=boot_id,
                sequence_number=3,
                observed_at=NOW + timedelta(seconds=11),
                observed_monotonic=9,
            )

        degraded = await service.check_timeouts(
            observed_at=NOW + timedelta(seconds=55),
            observed_monotonic=55,
        )
        assert len(degraded) == 1
        assert degraded[0].previous_status is AgentStatus.ONLINE
        assert degraded[0].agent.status is AgentStatus.DEGRADED
        assert (
            await service.check_timeouts(
                observed_at=NOW + timedelta(seconds=56),
                observed_monotonic=56,
            )
            == ()
        )

        restored = await service.record_heartbeat(
            agent.id,
            connection_id=connection_id,
            boot_id=boot_id,
            sequence_number=3,
            observed_at=NOW + timedelta(seconds=56),
            observed_monotonic=56,
        )
        assert restored.agent.status is AgentStatus.ONLINE

        offline = await service.check_timeouts(
            observed_at=NOW + timedelta(seconds=146),
            observed_monotonic=146,
        )
        assert len(offline) == 1
        assert offline[0].agent.status is AgentStatus.OFFLINE
        assert offline[0].connection.disconnected_at == NOW + timedelta(seconds=146)
        assert (await service.get_agent(agent.id)).status is AgentStatus.OFFLINE
        with pytest.raises(StaleAgentConnectionError):
            await service.require_current_connection(
                agent.id,
                connection_id=connection_id,
                boot_id=boot_id,
            )

    asyncio.run(scenario())


def test_presence_validates_timeouts_and_timezone_inputs() -> None:
    repository = InMemoryAgentPresenceRepository()
    with pytest.raises(ValueError, match="positive"):
        AgentPresenceService(repository, heartbeat_timeout_seconds=0)
    with pytest.raises(ValueError, match="longer"):
        AgentPresenceService(
            repository,
            heartbeat_timeout_seconds=45,
            offline_timeout_seconds=45,
        )

    async def scenario() -> None:
        agent = _agent(1, "home-lab")
        await repository.add_agent(agent)
        service = AgentPresenceService(repository)
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.register_authenticated_connection(
                agent,
                connection_id=UUID(int=1),
                boot_id=UUID(int=2),
                protocol_version="1.0",
                observed_at=NOW.replace(tzinfo=None),
                observed_monotonic=0,
            )
        with pytest.raises(ValueError, match="Monotonic"):
            await service.check_timeouts(observed_at=NOW, observed_monotonic=-1)

    asyncio.run(scenario())


def test_inventory_snapshot_reconciles_missing_benches_and_restores_them() -> None:
    async def scenario() -> None:
        agent = _agent(1, "home-lab", status=AgentStatus.ONLINE)
        boot_id = UUID(int=201)
        repository = InMemoryInventoryRepository()
        service = InventoryService(repository)

        first = await service.reconcile_snapshot(
            agent,
            _snapshot(
                boot_id,
                _bench("bench-01"),
                _bench(
                    "bench-02",
                    connectivity=BenchConnectivity.DEGRADED,
                    health=BenchHealth.WARNING,
                    kind=BenchKind.PHYSICAL,
                ),
            ),
            expected_boot_id=boot_id,
            observed_at=NOW + timedelta(seconds=1),
        )
        assert first.added_ids == {"home-lab/bench-01", "home-lab/bench-02"}
        assert first.updated_ids == set()
        assert first.offline_ids == set()
        first_by_id = {bench.id: bench for bench in first.benches}
        assert first_by_id["home-lab/bench-01"].status is GlobalBenchStatus.ONLINE
        assert first_by_id["home-lab/bench-02"].status is GlobalBenchStatus.DEGRADED
        assert first_by_id["home-lab/bench-02"].health is HealthStatus.WARNING
        assert first_by_id["home-lab/bench-02"].kind is GlobalBenchKind.PHYSICAL
        first_created_at = first_by_id["home-lab/bench-01"].created_at

        second = await service.reconcile_snapshot(
            agent,
            _snapshot(boot_id, _bench("bench-02", name="Renamed bench")),
            expected_boot_id=boot_id,
            observed_at=NOW + timedelta(seconds=2),
        )
        assert second.offline_ids == {"home-lab/bench-01"}
        assert (await service.get_bench("home-lab/bench-01")).status is (GlobalBenchStatus.OFFLINE)
        assert (await service.get_bench("home-lab/bench-02")).name == "Renamed bench"

        restored = await service.reconcile_snapshot(
            agent,
            _snapshot(boot_id, _bench("bench-01"), _bench("bench-02")),
            expected_boot_id=boot_id,
            observed_at=NOW + timedelta(seconds=3),
        )
        assert restored.offline_ids == set()
        bench_one = await service.get_bench("home-lab/bench-01")
        assert bench_one.status is GlobalBenchStatus.ONLINE
        assert bench_one.created_at == first_created_at
        assert bench_one.updated_at == NOW + timedelta(seconds=3)

    asyncio.run(scenario())


def test_inventory_is_agent_scoped_and_supports_deterministic_filters() -> None:
    async def scenario() -> None:
        home = _agent(1, "home-lab", status=AgentStatus.ONLINE)
        office = _agent(2, "office-lab", status=AgentStatus.ONLINE)
        repository = InMemoryInventoryRepository()
        service = InventoryService(repository)
        await service.reconcile_snapshot(
            office,
            _snapshot(
                UUID(int=202),
                _bench("physical-01", kind=BenchKind.PHYSICAL, label="ci"),
            ),
            observed_at=NOW,
        )
        await service.reconcile_snapshot(
            home,
            _snapshot(
                UUID(int=201),
                _bench("sim-02", label="dev"),
                _bench("sim-01", connectivity=BenchConnectivity.OFFLINE, label="ci"),
            ),
            observed_at=NOW,
        )

        assert [bench.id for bench in await service.list_benches()] == [
            "home-lab/sim-01",
            "home-lab/sim-02",
            "office-lab/physical-01",
        ]
        assert [bench.id for bench in await service.list_benches(agent_id=home.id)] == [
            "home-lab/sim-01",
            "home-lab/sim-02",
        ]
        assert [bench.id for bench in await service.list_benches(agent_slug="OFFICE-LAB")] == [
            "office-lab/physical-01"
        ]
        assert len(await service.list_benches(kind=GlobalBenchKind.PHYSICAL)) == 1
        assert len(await service.list_benches(capability="FIRMWARE")) == 3
        assert len(await service.list_benches(labels={"purpose": "ci"})) == 2
        assert len(await service.list_benches(online=True)) == 2
        assert len(await service.list_benches(status=GlobalBenchStatus.OFFLINE)) == 1

    asyncio.run(scenario())


def test_inventory_rejects_boot_and_global_agent_identity_conflicts_atomically() -> None:
    async def scenario() -> None:
        agent = _agent(1, "home-lab", status=AgentStatus.ONLINE)
        repository = InMemoryInventoryRepository()
        service = InventoryService(repository)
        boot_id = UUID(int=201)

        with pytest.raises(InventorySyncFailedError):
            await service.reconcile_snapshot(
                agent,
                _snapshot(UUID(int=999), _bench("bench-01")),
                expected_boot_id=boot_id,
                observed_at=NOW,
            )
        assert await service.list_benches() == []

        await service.reconcile_snapshot(
            agent,
            _snapshot(boot_id, _bench("bench-01")),
            expected_boot_id=boot_id,
            observed_at=NOW,
        )
        original = await service.list_benches()

        changed_slug = agent.model_copy(update={"slug": "renamed-lab"})
        with pytest.raises(GlobalBenchIdConflictError):
            await service.reconcile_snapshot(
                changed_slug,
                _snapshot(boot_id, _bench("bench-02")),
                observed_at=NOW + timedelta(seconds=1),
            )
        with pytest.raises(GlobalBenchIdConflictError):
            await service.reconcile_snapshot(
                _agent(2, "home-lab", status=AgentStatus.ONLINE),
                _snapshot(UUID(int=202)),
                observed_at=NOW + timedelta(seconds=1),
            )
        assert await service.list_benches() == original

        malicious = GlobalBenchRecord(
            id="home-lab/bench-02",
            agent_id=agent.id,
            agent_slug=agent.slug,
            local_bench_id="bench-02",
            name="bench-02",
            backend_id="simlab",
            kind=GlobalBenchKind.SIMULATED,
            status=GlobalBenchStatus.ONLINE,
            health=HealthStatus.HEALTHY,
            created_at=NOW,
            updated_at=NOW,
        )
        other = _agent(3, "other-lab", status=AgentStatus.ONLINE)
        with pytest.raises(BenchAgentMismatchError):
            await repository.reconcile_agent_snapshot(
                other,
                (malicious,),
                observed_at=NOW,
            )
        assert await service.list_benches() == original

    asyncio.run(scenario())


def test_explicit_disconnect_and_timeout_cascade_agent_benches_offline() -> None:
    async def scenario() -> None:
        agent = _agent(1, "home-lab")
        inventory_repository = InMemoryInventoryRepository()
        inventory = InventoryService(inventory_repository)
        presence_repository = InMemoryAgentPresenceRepository((agent,))
        presence = AgentPresenceService(
            presence_repository,
            heartbeat_timeout_seconds=5,
            offline_timeout_seconds=10,
            offline_inventory=inventory,
        )
        connection_id = UUID(int=101)
        boot_id = UUID(int=201)
        connected = await presence.register_authenticated_connection(
            agent,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version="1.0",
            observed_at=NOW,
            observed_monotonic=0,
        )
        await inventory.reconcile_snapshot(
            connected.agent,
            _snapshot(boot_id, _bench("bench-01"), _bench("bench-02")),
            expected_boot_id=boot_id,
            observed_at=NOW,
        )

        assert await presence.disconnect(
            agent.id,
            connection_id=connection_id,
            boot_id=boot_id,
            observed_at=NOW + timedelta(seconds=1),
        )
        assert all(
            bench.status is GlobalBenchStatus.OFFLINE for bench in await inventory.list_benches()
        )
        assert not await presence.disconnect(
            agent.id,
            connection_id=connection_id,
            boot_id=boot_id,
            observed_at=NOW + timedelta(seconds=2),
        )

        reconnected = await presence.register_authenticated_connection(
            await presence.get_agent(agent.id),
            connection_id=UUID(int=102),
            boot_id=UUID(int=202),
            protocol_version="1.0",
            observed_at=NOW + timedelta(seconds=3),
            observed_monotonic=3,
        )
        await inventory.reconcile_snapshot(
            reconnected.agent,
            _snapshot(UUID(int=202), _bench("bench-01"), _bench("bench-02")),
            expected_boot_id=UUID(int=202),
            observed_at=NOW + timedelta(seconds=3),
        )
        assert all(bench.online for bench in await inventory.list_benches())

        timed_out = await presence.check_timeouts(
            observed_at=NOW + timedelta(seconds=13),
            observed_monotonic=13,
        )
        assert len(timed_out) == 1
        assert timed_out[0].agent.status is AgentStatus.OFFLINE
        assert all(
            bench.status is GlobalBenchStatus.OFFLINE for bench in await inventory.list_benches()
        )

    asyncio.run(scenario())


def test_presence_does_not_publish_offline_when_inventory_cascade_fails() -> None:
    class FailingInventory:
        async def mark_agent_offline(
            self,
            agent_id: UUID,
            *,
            observed_at: datetime,
        ) -> object:
            raise RuntimeError(f"inventory unavailable for {agent_id} at {observed_at}")

    async def scenario() -> None:
        agent = _agent(1, "home-lab")
        repository = InMemoryAgentPresenceRepository((agent,))
        service = AgentPresenceService(repository, offline_inventory=FailingInventory())
        connection_id = UUID(int=101)
        boot_id = UUID(int=201)
        await service.register_authenticated_connection(
            agent,
            connection_id=connection_id,
            boot_id=boot_id,
            protocol_version="1.0",
            observed_at=NOW,
            observed_monotonic=0,
        )

        with pytest.raises(RuntimeError, match="inventory unavailable"):
            await service.disconnect(
                agent.id,
                connection_id=connection_id,
                boot_id=boot_id,
                observed_at=NOW + timedelta(seconds=1),
            )

        assert (await service.get_agent(agent.id)).status is AgentStatus.ONLINE
        assert await service.require_current_connection(
            agent.id,
            connection_id=connection_id,
            boot_id=boot_id,
        )

    asyncio.run(scenario())


def test_inventory_rejects_unenrolled_revoked_and_naive_receipt_time() -> None:
    async def scenario() -> None:
        service = InventoryService(InMemoryInventoryRepository())
        boot_id = UUID(int=201)
        with pytest.raises(AgentNotFoundError):
            await service.reconcile_snapshot(
                _agent(
                    1,
                    "pending-lab",
                    status=AgentStatus.PENDING,
                    enrollment_status=EnrollmentStatus.PENDING,
                ),
                _snapshot(boot_id),
                observed_at=NOW,
            )
        with pytest.raises(AgentRevokedError):
            await service.reconcile_snapshot(
                _agent(
                    2,
                    "revoked-lab",
                    status=AgentStatus.REVOKED,
                    enrollment_status=EnrollmentStatus.REVOKED,
                ),
                _snapshot(boot_id),
                observed_at=NOW,
            )
        with pytest.raises(AgentAuthenticationFailedError):
            await service.reconcile_snapshot(
                _agent(
                    3,
                    "unenrolled-lab",
                    status=AgentStatus.OFFLINE,
                    enrollment_status=EnrollmentStatus.PENDING,
                ),
                _snapshot(boot_id),
                observed_at=NOW,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            await service.reconcile_snapshot(
                _agent(4, "home-lab", status=AgentStatus.ONLINE),
                _snapshot(boot_id),
                observed_at=NOW.replace(tzinfo=None),
            )

    asyncio.run(scenario())
