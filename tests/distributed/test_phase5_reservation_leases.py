from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    CoordinatedReservationLease,
    LeaseApplicationReceipt,
    LeaseWriteDisposition,
    LeaseWriteResult,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.core.errors import (
    BenchAlreadyReservedError,
    ReservationOwnerMismatchError,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    ReservationLease,
    ReservationStatus,
)

NOW = datetime(2026, 7, 28, 14, tzinfo=UTC)
AGENT_ID = UUID(int=1)
BENCH_ID = "home-lab/bench-a"


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SequentialIds:
    def __init__(self, first: int = 10_000) -> None:
        self.next = first

    def __call__(self) -> UUID:
        identifier = UUID(int=self.next)
        self.next += 1
        return identifier


def _agent(status: AgentStatus = AgentStatus.ONLINE) -> AgentRecord:
    revoked = status is AgentStatus.REVOKED
    return AgentRecord(
        id=AGENT_ID,
        slug="home-lab",
        name="Home Lab",
        status=status,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=NOW - timedelta(days=1),
        enrollment_status=(EnrollmentStatus.REVOKED if revoked else EnrollmentStatus.ENROLLED),
        revoked_at=NOW if revoked else None,
    )


def _bench(
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    *,
    agent_id: UUID = AGENT_ID,
) -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=BENCH_ID,
        agent_id=agent_id,
        agent_slug="home-lab",
        local_bench_id="bench-a",
        name="Bench A",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=status,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"workflow", "firmware"}),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        last_seen_at=NOW,
    )


class FakeDirectory:
    def __init__(self, agent: AgentRecord, bench: GlobalBenchRecord) -> None:
        self.agents = {agent.id: agent}
        self.benches = {bench.id: bench}

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self.agents.get(agent_id)

    async def get_bench(self, bench_id: str) -> GlobalBenchRecord | None:
        return self.benches.get(bench_id)

    def set_agent_status(self, status: AgentStatus) -> None:
        agent = self.agents[AGENT_ID]
        self.agents[AGENT_ID] = agent.model_copy(update={"status": status})

    def set_bench_status(self, status: GlobalBenchStatus) -> None:
        bench = self.benches[BENCH_ID]
        self.benches[BENCH_ID] = bench.model_copy(update={"status": status})


@dataclass(slots=True)
class _Mutation:
    fingerprint: str
    result: LeaseWriteResult


class FakeCentralReservationLeaseRepository:
    """Deterministic transactional fake with per-bench lease high-water marks."""

    def __init__(self, directory: FakeDirectory) -> None:
        self.directory = directory
        self.records: dict[UUID, CoordinatedReservationLease] = {}
        self.current_by_bench: dict[str, UUID] = {}
        self.history: dict[str, list[ReservationLease]] = {}
        self.mutations: dict[str, _Mutation] = {}
        self.agent_status_on_next_grant: AgentStatus | None = None
        self.race_on_next_replace: CoordinatedReservationLease | None = None

    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease | None:
        return self.records.get(reservation_id)

    async def get_current_for_bench(
        self,
        bench_id: str,
    ) -> CoordinatedReservationLease | None:
        reservation_id = self.current_by_bench.get(bench_id)
        return self.records.get(reservation_id) if reservation_id is not None else None

    async def get_mutation_result(
        self,
        mutation_key: str,
        *,
        request_fingerprint: str,
    ) -> LeaseWriteResult | None:
        mutation = self.mutations.get(mutation_key)
        if mutation is None:
            return None
        if mutation.fingerprint != request_fingerprint:
            raise ReservationLeaseInvalidError(
                "Idempotency key was reused with different lease content.",
                idempotency_key=mutation_key,
            )
        return LeaseWriteResult(
            record=mutation.result.record,
            disposition=LeaseWriteDisposition.REPLAY,
        )

    async def grant_if_eligible(
        self,
        request: ReservationGrantRequest,
        *,
        mutation_key: str,
        request_fingerprint: str,
        expected_agent_status: AgentStatus,
        expected_bench_status: GlobalBenchStatus,
    ) -> LeaseWriteResult | None:
        replay = await self.get_mutation_result(
            mutation_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        if self.agent_status_on_next_grant is not None:
            self.directory.set_agent_status(self.agent_status_on_next_grant)
            self.agent_status_on_next_grant = None
        agent = self.directory.agents.get(request.agent_id)
        bench = self.directory.benches.get(request.reservation.bench_id)
        if (
            agent is None
            or bench is None
            or agent.status is not expected_agent_status
            or bench.status is not expected_bench_status
            or bench.agent_id != request.agent_id
            or request.reservation.bench_id in self.current_by_bench
        ):
            return None
        history = self.history.setdefault(request.reservation.bench_id, [])
        lease_version = max((lease.lease_version for lease in history), default=0) + 1
        starts_at = request.reservation.starts_at or request.reservation.created_at
        lease = ReservationLease(
            reservation_id=request.reservation.id,
            agent_id=request.agent_id,
            bench_id=request.reservation.bench_id,
            owner=request.reservation.owner,
            valid_from=starts_at,
            valid_until=request.lease_valid_until,
            lease_version=lease_version,
        )
        record = CoordinatedReservationLease(
            reservation=request.reservation,
            lease=lease,
            state=ReservationLeaseState.ACTIVATING,
            revision=1,
        )
        result = LeaseWriteResult(record, LeaseWriteDisposition.APPLIED)
        self.records[record.reservation.id] = record
        self.current_by_bench[record.lease.bench_id] = record.reservation.id
        history.append(lease)
        self.mutations[mutation_key] = _Mutation(request_fingerprint, result)
        return result

    async def replace_if_current(
        self,
        record: CoordinatedReservationLease,
        *,
        expected_revision: int,
        mutation_key: str,
        request_fingerprint: str,
        expected_agent_status: AgentStatus | None = None,
        expected_bench_status: GlobalBenchStatus | None = None,
    ) -> LeaseWriteResult | None:
        replay = await self.get_mutation_result(
            mutation_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        if self.race_on_next_replace is not None:
            raced = self.race_on_next_replace
            self.race_on_next_replace = None
            self._persist_record(raced)
            return None
        current = self.records.get(record.reservation.id)
        if current is None or current.revision != expected_revision:
            return None
        agent = self.directory.agents.get(record.lease.agent_id)
        bench = self.directory.benches.get(record.lease.bench_id)
        if expected_agent_status is not None and (
            agent is None or agent.status is not expected_agent_status
        ):
            return None
        if expected_bench_status is not None and (
            bench is None
            or bench.status is not expected_bench_status
            or bench.agent_id != record.lease.agent_id
        ):
            return None
        history = self.history.setdefault(record.lease.bench_id, [])
        if record.lease.lease_version < current.lease.lease_version:
            return None
        if record.lease.lease_version > current.lease.lease_version:
            maximum = max((lease.lease_version for lease in history), default=0)
            if record.lease.lease_version != maximum + 1:
                return None
            replacement_at = record.lease.valid_from
            history[:] = [
                lease.model_copy(update={"released_at": replacement_at})
                if lease.lease_version == current.lease.lease_version and lease.released_at is None
                else lease
                for lease in history
            ]
            history.append(record.lease)
        else:
            history[:] = [
                record.lease if lease.lease_version == record.lease.lease_version else lease
                for lease in history
            ]
        self._persist_record(record)
        result = LeaseWriteResult(record, LeaseWriteDisposition.APPLIED)
        self.mutations[mutation_key] = _Mutation(request_fingerprint, result)
        return result

    async def list(
        self,
        *,
        agent_id: UUID | None = None,
        states: Iterable[ReservationLeaseState] | None = None,
        limit: int = 10_000,
    ) -> list[CoordinatedReservationLease]:
        selected = frozenset(states) if states is not None else None
        return [
            record
            for record in sorted(
                self.records.values(),
                key=lambda item: str(item.reservation.id),
            )
            if (agent_id is None or record.lease.agent_id == agent_id)
            and (selected is None or record.state in selected)
        ][:limit]

    def _persist_record(self, record: CoordinatedReservationLease) -> None:
        self.records[record.reservation.id] = record
        if record.state in {
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }:
            self.current_by_bench.pop(record.lease.bench_id, None)
        else:
            self.current_by_bench[record.lease.bench_id] = record.reservation.id


class FakeLeaseSynchronizer:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.apply_calls: list[ReservationLease] = []
        self.release_calls: list[ReservationLease] = []
        self.local_by_bench: dict[str, ReservationLease] = {}
        self.fail_apply = False
        self.mismatch_receipt = False
        self.fail_release = False
        self.before_apply: Callable[[ReservationLease], None] | None = None
        self.apply_entered: asyncio.Event | None = None
        self.allow_apply: asyncio.Event | None = None

    async def apply_lease(self, lease: ReservationLease) -> LeaseApplicationReceipt:
        self.apply_calls.append(lease)
        if self.before_apply is not None:
            self.before_apply(lease)
        if self.apply_entered is not None:
            self.apply_entered.set()
        if self.allow_apply is not None:
            await self.allow_apply.wait()
        if self.fail_apply:
            raise OSError("simulated lost activation acknowledgement")
        current = self.local_by_bench.get(lease.bench_id)
        if current is not None:
            if current.lease_version > lease.lease_version:
                raise ReservationLeaseVersionMismatchError("Local lease is newer.")
            if current.lease_version == lease.lease_version and current != lease:
                raise ReservationLeaseInvalidError("Lease version content changed.")
        self.local_by_bench[lease.bench_id] = lease
        return LeaseApplicationReceipt(
            reservation_id=(UUID(int=999) if self.mismatch_receipt else lease.reservation_id),
            agent_id=lease.agent_id,
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            confirmed_at=self.clock.now,
        )

    async def release_lease(self, lease: ReservationLease) -> None:
        self.release_calls.append(lease)
        if self.fail_release:
            raise OSError("simulated lost release")
        current = self.local_by_bench.get(lease.bench_id)
        if current is not None and current.lease_version == lease.lease_version:
            self.local_by_bench.pop(lease.bench_id)


def _stack(
    *,
    agent_status: AgentStatus = AgentStatus.ONLINE,
    bench_status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
    offline_grace_seconds: int = 300,
) -> tuple[
    CentralReservationLeaseService,
    FakeCentralReservationLeaseRepository,
    FakeDirectory,
    FakeLeaseSynchronizer,
    MutableClock,
]:
    clock = MutableClock()
    directory = FakeDirectory(_agent(agent_status), _bench(bench_status))
    repository = FakeCentralReservationLeaseRepository(directory)
    synchronizer = FakeLeaseSynchronizer(clock)
    service = CentralReservationLeaseService(
        repository,
        directory,
        synchronizer,
        clock=clock,
        id_factory=SequentialIds(),
        default_reservation_duration_seconds=3_600,
        maximum_reservation_duration_seconds=7_200,
        default_lease_ttl_seconds=300,
        maximum_lease_ttl_seconds=3_600,
        maximum_clock_skew_seconds=30,
        offline_reservation_grace_seconds=offline_grace_seconds,
    )
    return service, repository, directory, synchronizer, clock


def test_grant_is_tentative_until_exact_agent_confirmation() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        observed_states: list[tuple[ReservationLeaseState, ReservationStatus]] = []

        def observe_pending(lease: ReservationLease) -> None:
            pending = repository.records[lease.reservation_id]
            observed_states.append((pending.state, pending.reservation.status))
            assert not pending.accepts_new_work

        synchronizer.before_apply = observe_pending

        record = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="github-actions",
            idempotency_key="grant-1",
        )

        assert observed_states == [(ReservationLeaseState.ACTIVATING, ReservationStatus.SCHEDULED)]
        assert record.state is ReservationLeaseState.ACTIVE
        assert record.reservation.status is ReservationStatus.ACTIVE
        assert record.lease.lease_version == 1
        assert record.lease.valid_from == NOW
        assert record.lease.valid_until == NOW + timedelta(seconds=300)
        assert record.accepts_new_work
        assert synchronizer.local_by_bench[BENCH_ID] == record.lease

    asyncio.run(scenario())


def test_configuration_and_request_bounds_are_strict() -> None:
    directory = FakeDirectory(_agent(), _bench())
    repository = FakeCentralReservationLeaseRepository(directory)
    clock = MutableClock()
    synchronizer = FakeLeaseSynchronizer(clock)
    invalid_options: tuple[dict[str, object], ...] = (
        {"default_reservation_duration_seconds": 0},
        {
            "default_reservation_duration_seconds": 10,
            "maximum_reservation_duration_seconds": 9,
        },
        {"default_lease_ttl_seconds": 0},
        {"maximum_clock_skew_seconds": -1},
        {"maximum_clock_skew_seconds": True},
        {"offline_reservation_grace_seconds": -1},
    )
    for options in invalid_options:
        with pytest.raises(ValueError):
            CentralReservationLeaseService(
                repository,
                directory,
                synchronizer,
                **options,  # type: ignore[arg-type]
            )

    async def scenario() -> None:
        service, grant_repository, _, _, _ = _stack()
        invalid_requests = (
            {"owner": "", "idempotency_key": "valid"},
            {"owner": "owner", "idempotency_key": "   "},
            {
                "owner": "owner",
                "idempotency_key": "duration-zero",
                "reservation_duration_seconds": 0,
            },
            {
                "owner": "owner",
                "idempotency_key": "ttl-too-large",
                "lease_ttl_seconds": 3_601,
            },
        )
        for request in invalid_requests:
            with pytest.raises(ValueError):
                await service.grant(
                    agent_id=AGENT_ID,
                    bench_id=BENCH_ID,
                    **request,
                )
        assert not grant_repository.records

    asyncio.run(scenario())


def test_concurrent_global_grants_have_exactly_one_winner() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()

        results = await asyncio.gather(
            service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner-a",
                idempotency_key="grant-a",
            ),
            service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner-b",
                idempotency_key="grant-b",
            ),
            return_exceptions=True,
        )

        records = [item for item in results if isinstance(item, CoordinatedReservationLease)]
        failures = [item for item in results if isinstance(item, BenchAlreadyReservedError)]
        assert len(records) == 1
        assert len(failures) == 1
        assert len(repository.records) == 1
        assert len(synchronizer.apply_calls) == 1

    asyncio.run(scenario())


def test_grant_idempotency_replays_and_rejects_changed_content() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        first = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-a",
            idempotency_key="same-request",
        )
        replay = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-a",
            idempotency_key="same-request",
        )

        assert replay == first
        assert len(repository.records) == 1
        assert len(synchronizer.apply_calls) == 1
        with pytest.raises(ReservationLeaseInvalidError):
            await service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="changed-owner",
                idempotency_key="same-request",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (AgentStatus.PENDING, AgentOfflineError),
        (AgentStatus.OFFLINE, AgentOfflineError),
        (AgentStatus.DEGRADED, AgentDegradedError),
        (AgentStatus.DRAINING, AgentDrainingError),
        (AgentStatus.DRAINED, AgentDrainingError),
        (AgentStatus.REVOKED, AgentRevokedError),
        (AgentStatus.INCOMPATIBLE, AgentIncompatibleError),
    ],
)
def test_grant_rejects_ineligible_agent_statuses(
    status: AgentStatus,
    error: type[Exception],
) -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack(agent_status=status)
        with pytest.raises(error):
            await service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                idempotency_key=f"grant-{status.value}",
            )
        assert not repository.records
        assert not synchronizer.apply_calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (GlobalBenchStatus.OFFLINE, AgentOfflineError),
        (GlobalBenchStatus.DEGRADED, AgentDegradedError),
    ],
)
def test_grant_rejects_ineligible_benches(
    status: GlobalBenchStatus,
    error: type[Exception],
) -> None:
    async def scenario() -> None:
        service, _, _, _, _ = _stack(bench_status=status)
        with pytest.raises(error):
            await service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                idempotency_key=f"bench-{status.value}",
            )

    asyncio.run(scenario())


def test_atomic_grant_rechecks_eligibility_inside_write() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        repository.agent_status_on_next_grant = AgentStatus.OFFLINE

        with pytest.raises(AgentOfflineError):
            await service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                idempotency_key="offline-race",
            )
        assert not repository.records
        assert not synchronizer.apply_calls

    asyncio.run(scenario())


def test_lost_or_mismatched_activation_confirmation_becomes_unknown() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        synchronizer.fail_apply = True
        record = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="lost-ack",
        )

        assert record.state is ReservationLeaseState.UNKNOWN
        assert record.reservation.status is ReservationStatus.SCHEDULED
        assert not record.accepts_new_work
        with pytest.raises(ReservationLeaseInvalidError):
            await service.require_for_new_work(
                record.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=record.lease.lease_version,
            )
        with pytest.raises(BenchAlreadyReservedError):
            await service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="other",
                idempotency_key="blocked-by-unknown",
            )
        assert repository.current_by_bench[BENCH_ID] == record.reservation.id

        service2, _, _, synchronizer2, _ = _stack()
        synchronizer2.mismatch_receipt = True
        mismatched = await service2.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="bad-receipt",
        )
        assert mismatched.state is ReservationLeaseState.UNKNOWN

    asyncio.run(scenario())


def test_renewal_advances_version_and_stale_versions_are_rejected() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, clock = _stack()
        first = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        clock.now += timedelta(seconds=100)
        renewal_states: list[ReservationLeaseState] = []

        def observe_renewal(lease: ReservationLease) -> None:
            if lease.lease_version == 2:
                renewal_states.append(repository.records[first.reservation.id].state)

        synchronizer.before_apply = observe_renewal
        renewed = await service.renew(
            first.reservation.id,
            owner="owner",
            expected_lease_version=1,
            idempotency_key="renew-1",
            lease_ttl_seconds=600,
        )

        assert renewed.state is ReservationLeaseState.ACTIVE
        assert renewal_states == [ReservationLeaseState.RENEWING]
        assert renewed.lease.lease_version == 2
        assert renewed.lease.valid_from == clock.now
        history = repository.history[BENCH_ID]
        assert history[0].released_at == clock.now
        assert history[1] == renewed.lease
        assert [lease.lease_version for lease in synchronizer.apply_calls] == [1, 2]

        replay = await service.renew(
            first.reservation.id,
            owner="owner",
            expected_lease_version=1,
            idempotency_key="renew-1",
            lease_ttl_seconds=600,
        )
        assert replay == renewed
        assert len(synchronizer.apply_calls) == 2
        with pytest.raises(ReservationLeaseVersionMismatchError):
            await service.renew(
                first.reservation.id,
                owner="owner",
                expected_lease_version=1,
                idempotency_key="stale-renew",
            )
        with pytest.raises(ReservationLeaseVersionMismatchError):
            await service.require_for_new_work(
                first.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
            )

    asyncio.run(scenario())


def test_release_is_owner_version_fenced_and_versions_survive_owner_change() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        first = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-a",
            idempotency_key="grant-a",
        )
        with pytest.raises(ReservationOwnerMismatchError):
            await service.release(
                first.reservation.id,
                owner="owner-b",
                expected_lease_version=1,
                idempotency_key="wrong-owner",
            )
        with pytest.raises(ReservationLeaseVersionMismatchError):
            await service.release(
                first.reservation.id,
                owner="owner-a",
                expected_lease_version=2,
                idempotency_key="wrong-version",
            )

        released = await service.release(
            first.reservation.id,
            owner="owner-a",
            expected_lease_version=1,
            idempotency_key="release-a",
        )
        assert released.state is ReservationLeaseState.RELEASED
        assert released.reservation.status is ReservationStatus.RELEASED
        assert released.lease.released_at == NOW
        assert synchronizer.release_calls == [released.lease]
        assert (
            await service.release(
                first.reservation.id,
                owner="owner-a",
                expected_lease_version=1,
                idempotency_key="release-a",
            )
            == released
        )
        second = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-b",
            idempotency_key="grant-b",
        )
        assert second.lease.lease_version == 2
        assert repository.current_by_bench[BENCH_ID] == second.reservation.id

    asyncio.run(scenario())


def test_revoke_cancels_reservation_and_notifies_agent() -> None:
    async def scenario() -> None:
        service, _, _, synchronizer, _ = _stack()
        active = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        revoked = await service.revoke(
            active.reservation.id,
            expected_lease_version=1,
            idempotency_key="admin-revoke",
        )
        assert revoked.state is ReservationLeaseState.REVOKED
        assert revoked.reservation.status is ReservationStatus.CANCELLED
        assert synchronizer.release_calls == [revoked.lease]

    asyncio.run(scenario())


def test_new_work_validation_enforces_route_ttl_version_and_clock_skew() -> None:
    async def scenario() -> None:
        service, _, _, _, clock = _stack()
        active = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
            lease_ttl_seconds=60,
        )

        assert (
            await service.require_for_new_work(
                active.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
                agent_observed_at=NOW + timedelta(seconds=30),
            )
            == active.lease
        )
        with pytest.raises(AgentDegradedError):
            await service.require_for_new_work(
                active.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
                agent_observed_at=NOW + timedelta(seconds=30, microseconds=1),
            )
        with pytest.raises(BenchAgentMismatchError):
            await service.require_for_new_work(
                active.reservation.id,
                agent_id=AGENT_ID,
                bench_id="home-lab/other",
                owner="owner",
                lease_version=1,
            )

        clock.now = active.lease.valid_until
        await service.require_for_new_work(
            active.reservation.id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            lease_version=1,
        )
        clock.now += timedelta(microseconds=1)
        with pytest.raises(ReservationLeaseExpiredError):
            await service.require_for_new_work(
                active.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
            )
        clock.now = active.lease.valid_until + timedelta(seconds=30, microseconds=1)
        assert await service.expire_due() == 1

    asyncio.run(scenario())


def test_disconnect_retains_unknown_until_exact_online_reconciliation() -> None:
    async def scenario() -> None:
        service, repository, directory, _, clock = _stack()
        first = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        clock.now += timedelta(seconds=10)
        unknown = (
            await service.mark_agent_disconnected(
                AGENT_ID,
                disconnect_id=UUID(int=50_001),
            )
        )[0]

        assert unknown.state is ReservationLeaseState.UNKNOWN
        assert unknown.reservation.status is ReservationStatus.ACTIVE
        assert unknown.lease.released_at is None
        assert unknown.reconciliation_deadline == clock.now + timedelta(seconds=300)
        with pytest.raises(ReservationLeaseInvalidError):
            await service.require_for_new_work(
                first.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
            )

        future_version = unknown.lease.model_copy(update={"lease_version": 2})
        with pytest.raises(ReservationLeaseVersionMismatchError):
            await service.reconcile_after_reconnect(
                future_version,
                idempotency_key="future-local-lease",
            )
        directory.set_agent_status(AgentStatus.OFFLINE)
        with pytest.raises(AgentOfflineError):
            await service.reconcile_after_reconnect(
                unknown.lease,
                idempotency_key="offline-reconcile",
            )
        assert repository.records[first.reservation.id].state is ReservationLeaseState.UNKNOWN

        directory.set_agent_status(AgentStatus.ONLINE)
        restored = await service.reconcile_after_reconnect(
            unknown.lease,
            idempotency_key="online-reconcile",
        )
        assert restored.state is ReservationLeaseState.ACTIVE
        assert restored.lease.lease_version == 1

    asyncio.run(scenario())


def test_disconnect_during_activation_cannot_publish_false_assignment() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, _ = _stack()
        synchronizer.apply_entered = asyncio.Event()
        synchronizer.allow_apply = asyncio.Event()
        grant_task = asyncio.create_task(
            service.grant(
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                idempotency_key="grant-race",
            )
        )
        await synchronizer.apply_entered.wait()
        pending = next(iter(repository.records.values()))
        assert pending.state is ReservationLeaseState.ACTIVATING

        changed = await service.mark_agent_disconnected(
            AGENT_ID,
            disconnect_id=UUID(int=50_003),
        )
        assert changed[0].state is ReservationLeaseState.UNKNOWN
        synchronizer.allow_apply.set()
        result = await grant_task

        assert result.state is ReservationLeaseState.UNKNOWN
        assert result.reservation.status is ReservationStatus.SCHEDULED
        assert not result.accepts_new_work

    asyncio.run(scenario())


def test_assignment_guard_rechecks_current_agent_status() -> None:
    async def scenario() -> None:
        service, _, directory, _, _ = _stack()
        active = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        directory.set_agent_status(AgentStatus.DRAINING)

        with pytest.raises(AgentDrainingError):
            await service.require_for_new_work(
                active.reservation.id,
                agent_id=AGENT_ID,
                bench_id=BENCH_ID,
                owner="owner",
                lease_version=1,
            )

    asyncio.run(scenario())


def test_unknown_grace_expiry_releases_lease_without_leaking_bench() -> None:
    async def scenario() -> None:
        service, repository, _, synchronizer, clock = _stack()
        first = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-a",
            idempotency_key="grant-a",
        )
        unknown = (
            await service.mark_agent_disconnected(
                AGENT_ID,
                disconnect_id=UUID(int=50_002),
            )
        )[0]
        deadline = unknown.reconciliation_deadline
        assert deadline is not None
        clock.now = deadline - timedelta(microseconds=1)
        assert await service.expire_due() == 0
        clock.now += timedelta(microseconds=1)
        assert await service.expire_due() == 1

        expired = repository.records[first.reservation.id]
        assert expired.state is ReservationLeaseState.EXPIRED
        assert expired.reservation.status is ReservationStatus.EXPIRED
        assert expired.lease.released_at == clock.now
        assert synchronizer.release_calls[-1] == expired.lease
        replacement = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner-b",
            idempotency_key="grant-b",
        )
        assert replacement.lease.lease_version == 2

    asyncio.run(scenario())


def test_zero_offline_grace_is_valid_and_immediately_expirable() -> None:
    async def scenario() -> None:
        service, repository, _, _, clock = _stack(offline_grace_seconds=0)
        active = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        unknown = (
            await service.mark_agent_disconnected(
                AGENT_ID,
                disconnect_id=UUID(int=50_004),
            )
        )[0]

        assert unknown.reconciliation_deadline == clock.now
        assert await service.expire_due() == 1
        assert repository.records[active.reservation.id].state is ReservationLeaseState.EXPIRED

    asyncio.run(scenario())


def test_renewal_cas_does_not_overwrite_concurrent_release() -> None:
    async def scenario() -> None:
        service, repository, _, _, clock = _stack()
        active = await service.grant(
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner="owner",
            idempotency_key="grant",
        )
        clock.now += timedelta(seconds=1)
        released_lease = active.lease.model_copy(update={"released_at": clock.now})
        concurrent_release = CoordinatedReservationLease(
            reservation=active.reservation.model_copy(
                update={
                    "status": ReservationStatus.RELEASED,
                    "released_at": clock.now,
                }
            ),
            lease=released_lease,
            state=ReservationLeaseState.RELEASED,
            revision=active.revision + 1,
        )
        repository.race_on_next_replace = concurrent_release

        with pytest.raises(ReservationLeaseInvalidError):
            await service.renew(
                active.reservation.id,
                owner="owner",
                expected_lease_version=1,
                idempotency_key="renew-race",
            )
        assert repository.records[active.reservation.id] == concurrent_release

    asyncio.run(scenario())
