from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from lab_platform.control_plane_core.errors import ReservationLeaseInvalidError
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    LeaseWriteDisposition,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.models import (
    AgentStatus,
    GlobalBenchStatus,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
)
from lab_platform.persistence import SQLiteCentralReservationLeaseRepository
from lab_platform.persistence.database import SQLiteDatabase

NOW = datetime(2026, 7, 28, 14, tzinfo=UTC)
AGENT_ID = UUID(int=101)
OTHER_AGENT_ID = UUID(int=102)
BENCH_ID = "home-lab/bench-a"
FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
ORG_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _seed_agent(
    database: SQLiteDatabase,
    agent_id: UUID = AGENT_ID,
    *,
    slug: str = "home-lab",
    status: AgentStatus = AgentStatus.ONLINE,
) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO agents "
            "(id, slug, name, status, version, protocol_version, labels_json, "
            "registered_at, enrollment_status) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'ENROLLED')",
            (
                str(agent_id),
                slug,
                slug,
                status.value,
                "0.6.0-alpha",
                "1.0",
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )


def _seed_bench(
    database: SQLiteDatabase,
    *,
    agent_id: UUID = AGENT_ID,
    status: GlobalBenchStatus = GlobalBenchStatus.ONLINE,
) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO global_benches "
            "(id, agent_id, agent_slug, local_bench_id, name, backend_id, kind, status, "
            "health, capabilities_json, labels_json, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, 'home-lab', 'bench-a', 'Bench A', 'simlab', 'SIMULATED', ?, "
            "'healthy', '[]', '{}', ?, ?, ?)",
            (
                BENCH_ID,
                str(agent_id),
                status.value,
                NOW.isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
                NOW.isoformat(),
            ),
        )


def _reservation(
    reservation_id: UUID,
    *,
    owner: str = "github-actions",
    idempotency_key: str = "grant-1",
    starts_at: datetime = NOW,
) -> Reservation:
    return Reservation(
        id=reservation_id,
        bench_id=BENCH_ID,
        owner=owner,
        created_at=starts_at,
        requested_at=starts_at,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=ReservationStatus.SCHEDULED,
        source=ReservationSource.API,
        metadata={"suite": "smoke"},
        idempotency_key=idempotency_key,
    )


def _grant_request(
    reservation_id: UUID,
    *,
    agent_id: UUID = AGENT_ID,
    owner: str = "github-actions",
    idempotency_key: str = "grant-1",
    starts_at: datetime = NOW,
) -> ReservationGrantRequest:
    return ReservationGrantRequest(
        reservation=_reservation(
            reservation_id,
            owner=owner,
            idempotency_key=idempotency_key,
            starts_at=starts_at,
        ),
        agent_id=agent_id,
        lease_valid_until=starts_at + timedelta(minutes=10),
    )


async def _grant(
    repository: SQLiteCentralReservationLeaseRepository,
    request: ReservationGrantRequest,
    *,
    mutation_key: str,
    fingerprint: str,
) -> CoordinatedReservationLease | None:
    result = await repository.grant_if_eligible(
        request,
        mutation_key=mutation_key,
        request_fingerprint=fingerprint,
        expected_agent_status=AgentStatus.ONLINE,
        expected_bench_status=GlobalBenchStatus.ONLINE,
    )
    return result.record if result is not None else None


def test_coordinated_schema_is_migrated_and_repository_is_exported(tmp_path: Path) -> None:
    database = _database(tmp_path / "schema.db")
    with database.transaction() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert {
        "coordinated_reservation_leases",
        "reservation_lease_mutations",
    } <= tables
    assert {
        "coordinated_reservation_leases_current_bench",
        "coordinated_reservation_leases_agent_state",
        "reservation_lease_mutations_reservation",
    } <= indexes
    assert isinstance(
        SQLiteCentralReservationLeaseRepository(database),
        SQLiteCentralReservationLeaseRepository,
    )
    database.close()


def test_grant_persists_base_lease_coordination_and_restart_safe_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "grant.db"
        database = _database(path)
        _seed_agent(database)
        _seed_bench(database)
        repository = SQLiteCentralReservationLeaseRepository(database)
        request = _grant_request(UUID(int=201))

        result = await repository.grant_if_eligible(
            request,
            mutation_key="grant-1",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert result is not None
        assert result.disposition is LeaseWriteDisposition.APPLIED
        assert result.record.state is ReservationLeaseState.ACTIVATING
        assert result.record.revision == 1
        assert result.record.lease.lease_version == 1
        assert await repository.get(request.reservation.id) == result.record
        assert await repository.get_current_for_bench(BENCH_ID) == result.record

        with database.transaction() as connection:
            base = connection.execute(
                "SELECT * FROM reservations WHERE id = ?",
                (str(request.reservation.id),),
            ).fetchone()
            lease = connection.execute(
                "SELECT * FROM reservation_leases WHERE reservation_id = ?",
                (str(request.reservation.id),),
            ).fetchone()
            mutation_count = connection.execute(
                "SELECT COUNT(*) FROM reservation_lease_mutations"
            ).fetchone()[0]
        assert base["status"] == ReservationStatus.SCHEDULED.value
        assert base["metadata"] == '{"suite":"smoke"}'
        assert lease["lease_version"] == 1
        assert lease["released_at"] is None
        assert mutation_count == 1

        replay = await repository.grant_if_eligible(
            request,
            mutation_key="grant-1",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert replay is not None
        assert replay.record == result.record
        assert replay.disposition is LeaseWriteDisposition.REPLAY
        with pytest.raises(ReservationLeaseInvalidError):
            await repository.get_mutation_result(
                "grant-1",
                organisation_id=result.record.reservation.organisation_id,
                request_fingerprint=FINGERPRINT_B,
            )

        database.close()
        reopened = _database(path)
        restarted = SQLiteCentralReservationLeaseRepository(reopened)
        assert await restarted.get(request.reservation.id) == result.record
        restarted_replay = await restarted.get_mutation_result(
            "grant-1",
            organisation_id=result.record.reservation.organisation_id,
            request_fingerprint=FINGERPRINT_A,
        )
        assert restarted_replay is not None
        assert restarted_replay.disposition is LeaseWriteDisposition.REPLAY
        assert restarted_replay.record == result.record
        assert await restarted.list(states={ReservationLeaseState.ACTIVATING}) == [result.record]
        assert await restarted.list(agent_id=OTHER_AGENT_ID) == []
        assert await restarted.list(states=set()) == []
        with pytest.raises(ValueError):
            await restarted.list(limit=0)
        reopened.close()

    asyncio.run(scenario())


def test_mutation_replay_is_tenant_scoped_for_direct_grants(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "tenant-mutations.db")
        routes = (
            (ORG_A, UUID(int=211), "tenant-a", "tenant-a/bench", UUID(int=213)),
            (ORG_B, UUID(int=212), "tenant-b", "tenant-b/bench", UUID(int=214)),
        )
        with database.transaction(immediate=True) as connection:
            for organisation_id, agent_id, slug, bench_id, _reservation_id in routes:
                connection.execute(
                    "INSERT INTO organisations "
                    "(id, slug, name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
                    (
                        str(organisation_id),
                        slug,
                        slug,
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO agents "
                    "(id, slug, name, status, version, protocol_version, labels_json, "
                    "registered_at, enrollment_status, organisation_id) "
                    "VALUES (?, ?, ?, 'ONLINE', '0.6.0-alpha', '1.0', '{}', ?, "
                    "'ENROLLED', ?)",
                    (
                        str(agent_id),
                        slug,
                        slug,
                        (NOW - timedelta(days=1)).isoformat(),
                        str(organisation_id),
                    ),
                )
                connection.execute(
                    "INSERT INTO global_benches "
                    "(id, agent_id, agent_slug, local_bench_id, name, backend_id, kind, "
                    "status, health, capabilities_json, labels_json, last_seen_at, created_at, "
                    "updated_at, organisation_id) "
                    "VALUES (?, ?, ?, 'bench', ?, 'simlab', 'SIMULATED', 'ONLINE', "
                    "'healthy', '[]', '{}', ?, ?, ?, ?)",
                    (
                        bench_id,
                        str(agent_id),
                        slug,
                        bench_id,
                        NOW.isoformat(),
                        (NOW - timedelta(days=1)).isoformat(),
                        NOW.isoformat(),
                        str(organisation_id),
                    ),
                )

        repository = SQLiteCentralReservationLeaseRepository(database)

        def request_for(
            organisation_id: UUID,
            agent_id: UUID,
            bench_id: str,
            reservation_id: UUID,
        ) -> ReservationGrantRequest:
            return ReservationGrantRequest(
                reservation=Reservation(
                    id=reservation_id,
                    organisation_id=organisation_id,
                    bench_id=bench_id,
                    owner=f"owner-{organisation_id}",
                    created_at=NOW,
                    requested_at=NOW,
                    starts_at=NOW,
                    ends_at=NOW + timedelta(hours=1),
                    status=ReservationStatus.SCHEDULED,
                    source=ReservationSource.API,
                    idempotency_key="shared-direct-key",
                ),
                agent_id=agent_id,
                lease_valid_until=NOW + timedelta(minutes=10),
            )

        requests = [
            request_for(organisation_id, agent_id, bench_id, reservation_id)
            for organisation_id, agent_id, _slug, bench_id, reservation_id in routes
        ]
        first = await repository.grant_if_eligible(
            requests[0],
            mutation_key="shared-direct-key",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        second = await repository.grant_if_eligible(
            requests[1],
            mutation_key="shared-direct-key",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert first is not None and first.disposition is LeaseWriteDisposition.APPLIED
        assert second is not None and second.disposition is LeaseWriteDisposition.APPLIED
        assert first.record.reservation.organisation_id == ORG_A
        assert second.record.reservation.organisation_id == ORG_B
        assert first.record.reservation.id != second.record.reservation.id

        replay_a = await repository.grant_if_eligible(
            requests[0],
            mutation_key="shared-direct-key",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        replay_b = await repository.grant_if_eligible(
            requests[1],
            mutation_key="shared-direct-key",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert replay_a is not None and replay_a.disposition is LeaseWriteDisposition.REPLAY
        assert replay_b is not None and replay_b.disposition is LeaseWriteDisposition.REPLAY
        assert replay_a.record == first.record
        assert replay_b.record == second.record
        with pytest.raises(ReservationLeaseInvalidError):
            await repository.get_mutation_result(
                "shared-direct-key",
                organisation_id=ORG_A,
                request_fingerprint=FINGERPRINT_B,
            )
        with database.transaction() as connection:
            mutation_scopes = connection.execute(
                "SELECT organisation_id FROM reservation_lease_mutations "
                "WHERE mutation_key = ? ORDER BY organisation_id",
                ("shared-direct-key",),
            ).fetchall()
        assert [row[0] for row in mutation_scopes] == [str(ORG_A), str(ORG_B)]
        database.close()

    asyncio.run(scenario())


def test_v9_mutation_replay_is_preserved_by_tenant_key_upgrade(tmp_path: Path) -> None:
    async def seed(path: Path) -> CoordinatedReservationLease:
        database = _database(path)
        _seed_agent(database)
        _seed_bench(database)
        repository = SQLiteCentralReservationLeaseRepository(database)
        result = await repository.grant_if_eligible(
            _grant_request(UUID(int=215)),
            mutation_key="legacy-mutation",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert result is not None
        with database.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE reservation_lease_mutations_v9 (
                    mutation_key TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    organisation_id TEXT NOT NULL
                );
                INSERT INTO reservation_lease_mutations_v9
                    (mutation_key, request_fingerprint, reservation_id, revision,
                     result_json, created_at, organisation_id)
                    SELECT mutation_key, request_fingerprint, reservation_id, revision,
                           result_json, created_at, organisation_id
                    FROM reservation_lease_mutations;
                DROP TABLE reservation_lease_mutations;
                ALTER TABLE reservation_lease_mutations_v9
                    RENAME TO reservation_lease_mutations;
                DELETE FROM schema_migrations WHERE version = 10;
                """
            )
        database.close()
        return result.record

    async def verify(path: Path, expected: CoordinatedReservationLease) -> None:
        upgraded = _database(path)
        repository = SQLiteCentralReservationLeaseRepository(upgraded)
        replay = await repository.get_mutation_result(
            "legacy-mutation",
            organisation_id=expected.reservation.organisation_id,
            request_fingerprint=FINGERPRINT_A,
        )
        assert replay is not None
        assert replay.disposition is LeaseWriteDisposition.REPLAY
        assert replay.record == expected
        with upgraded.transaction() as connection:
            primary_key = [
                row[1]
                for row in sorted(
                    connection.execute("PRAGMA table_info(reservation_lease_mutations)"),
                    key=lambda row: row[5],
                )
                if row[5]
            ]
        assert primary_key == ["organisation_id", "mutation_key"]
        upgraded.close()

    path = tmp_path / "upgrade-mutation.db"
    expected = asyncio.run(seed(path))
    asyncio.run(verify(path, expected))


def test_grant_atomically_checks_status_ownership_and_current_uniqueness(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "eligibility.db")
        _seed_agent(database, status=AgentStatus.OFFLINE)
        _seed_agent(database, OTHER_AGENT_ID, slug="other-lab")
        _seed_bench(database)
        repository = SQLiteCentralReservationLeaseRepository(database)

        assert (
            await _grant(
                repository,
                _grant_request(UUID(int=301)),
                mutation_key="offline",
                fingerprint=FINGERPRINT_A,
            )
            is None
        )
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agents SET status = 'ONLINE' WHERE id = ?",
                (str(AGENT_ID),),
            )
        assert (
            await _grant(
                repository,
                _grant_request(UUID(int=302), agent_id=OTHER_AGENT_ID),
                mutation_key="wrong-owner",
                fingerprint=FINGERPRINT_A,
            )
            is None
        )
        first = await _grant(
            repository,
            _grant_request(UUID(int=303)),
            mutation_key="winner",
            fingerprint=FINGERPRINT_A,
        )
        assert first is not None
        assert (
            await _grant(
                repository,
                _grant_request(
                    UUID(int=304),
                    owner="other-owner",
                    idempotency_key="loser",
                ),
                mutation_key="loser",
                fingerprint=FINGERPRINT_B,
            )
            is None
        )
        with database.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM coordinated_reservation_leases"
                ).fetchone()[0]
                == 1
            )
        database.close()

    asyncio.run(scenario())


def test_replacement_is_a_stateful_cas_and_synchronizes_history_and_base_row(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "replacement.db")
        _seed_agent(database)
        _seed_bench(database)
        repository = SQLiteCentralReservationLeaseRepository(database)
        pending = await _grant(
            repository,
            _grant_request(UUID(int=401)),
            mutation_key="grant",
            fingerprint=FINGERPRINT_A,
        )
        assert pending is not None

        active = CoordinatedReservationLease(
            reservation=pending.reservation.model_copy(
                update={
                    "status": ReservationStatus.ACTIVE,
                    "activated_at": NOW + timedelta(seconds=1),
                }
            ),
            lease=pending.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=2,
        )
        assert (
            await repository.replace_if_current(
                active,
                expected_revision=99,
                mutation_key="confirm",
                request_fingerprint=FINGERPRINT_A,
            )
            is None
        )
        confirmed = await repository.replace_if_current(
            active,
            expected_revision=1,
            mutation_key="confirm",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert confirmed is not None
        assert confirmed.disposition is LeaseWriteDisposition.APPLIED
        replay = await repository.replace_if_current(
            active,
            expected_revision=1,
            mutation_key="confirm",
            request_fingerprint=FINGERPRINT_A,
        )
        assert replay is not None
        assert replay.disposition is LeaseWriteDisposition.REPLAY
        with pytest.raises(ReservationLeaseInvalidError):
            await repository.replace_if_current(
                active,
                expected_revision=1,
                mutation_key="confirm",
                request_fingerprint=FINGERPRINT_B,
            )

        renewed_lease = ReservationLease(
            reservation_id=active.reservation.id,
            agent_id=AGENT_ID,
            bench_id=BENCH_ID,
            owner=active.reservation.owner,
            valid_from=NOW + timedelta(minutes=5),
            valid_until=NOW + timedelta(minutes=20),
            lease_version=2,
        )
        renewing = CoordinatedReservationLease(
            reservation=active.reservation,
            lease=renewed_lease,
            state=ReservationLeaseState.RENEWING,
            revision=3,
        )
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agents SET status = 'OFFLINE' WHERE id = ?",
                (str(AGENT_ID),),
            )
        assert (
            await repository.replace_if_current(
                renewing,
                expected_revision=2,
                mutation_key="renew",
                request_fingerprint=FINGERPRINT_A,
                expected_agent_status=AgentStatus.ONLINE,
                expected_bench_status=GlobalBenchStatus.ONLINE,
            )
            is None
        )
        assert await repository.get(active.reservation.id) == active
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE agents SET status = 'ONLINE' WHERE id = ?",
                (str(AGENT_ID),),
            )
        renewed = await repository.replace_if_current(
            renewing,
            expected_revision=2,
            mutation_key="renew",
            request_fingerprint=FINGERPRINT_A,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        assert renewed is not None
        assert renewed.record == renewing

        renewed_active = CoordinatedReservationLease(
            reservation=active.reservation,
            lease=renewed_lease,
            state=ReservationLeaseState.ACTIVE,
            revision=4,
        )
        assert (
            await repository.replace_if_current(
                renewed_active,
                expected_revision=3,
                mutation_key="confirm-renew",
                request_fingerprint=FINGERPRINT_A,
            )
            is not None
        )
        invalid_skip = CoordinatedReservationLease(
            reservation=active.reservation,
            lease=renewed_lease.model_copy(
                update={
                    "valid_from": NOW + timedelta(minutes=6),
                    "valid_until": NOW + timedelta(minutes=21),
                    "lease_version": 4,
                }
            ),
            state=ReservationLeaseState.RENEWING,
            revision=5,
        )
        with pytest.raises(ReservationLeaseInvalidError):
            await repository.replace_if_current(
                invalid_skip,
                expected_revision=4,
                mutation_key="skip-version",
                request_fingerprint=FINGERPRINT_A,
            )

        released_at = NOW + timedelta(minutes=7)
        released = CoordinatedReservationLease(
            reservation=active.reservation.model_copy(
                update={
                    "status": ReservationStatus.RELEASED,
                    "released_at": released_at,
                }
            ),
            lease=renewed_lease.model_copy(update={"released_at": released_at}),
            state=ReservationLeaseState.RELEASED,
            revision=5,
        )
        release_result = await repository.replace_if_current(
            released,
            expected_revision=4,
            mutation_key="release",
            request_fingerprint=FINGERPRINT_A,
        )
        assert release_result is not None
        assert await repository.get_current_for_bench(BENCH_ID) is None

        with database.transaction() as connection:
            history = connection.execute(
                "SELECT * FROM reservation_leases WHERE bench_id = ? ORDER BY lease_version",
                (BENCH_ID,),
            ).fetchall()
            base = connection.execute(
                "SELECT * FROM reservations WHERE id = ?",
                (str(released.reservation.id),),
            ).fetchone()
        assert [row["lease_version"] for row in history] == [1, 2]
        assert history[0]["released_at"] == renewed_lease.valid_from.isoformat()
        assert history[1]["released_at"] == released_at.isoformat()
        assert base["status"] == ReservationStatus.RELEASED.value
        assert base["released_at"] == released_at.isoformat()

        successor = await _grant(
            repository,
            _grant_request(
                UUID(int=402),
                idempotency_key="successor",
                starts_at=NOW + timedelta(minutes=8),
            ),
            mutation_key="successor",
            fingerprint=FINGERPRINT_B,
        )
        assert successor is not None
        assert successor.lease.lease_version == 3
        assert await repository.list(states={ReservationLeaseState.RELEASED}) == [released]
        assert await repository.list(states={ReservationLeaseState.ACTIVATING}) == [successor]
        database.close()

    asyncio.run(scenario())


def test_parallel_connections_have_exactly_one_grant_winner(tmp_path: Path) -> None:
    path = tmp_path / "parallel.db"
    seed = _database(path)
    _seed_agent(seed)
    _seed_bench(seed)
    seed.close()
    first_database = _database(path)
    second_database = _database(path)
    first_repository = SQLiteCentralReservationLeaseRepository(first_database)
    second_repository = SQLiteCentralReservationLeaseRepository(second_database)

    def run_grant(
        repository: SQLiteCentralReservationLeaseRepository,
        request: ReservationGrantRequest,
        mutation_key: str,
        fingerprint: str,
    ) -> CoordinatedReservationLease | None:
        return asyncio.run(
            _grant(
                repository,
                request,
                mutation_key=mutation_key,
                fingerprint=fingerprint,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            run_grant,
            first_repository,
            _grant_request(UUID(int=501), idempotency_key="parallel-a"),
            "parallel-a",
            FINGERPRINT_A,
        )
        second = executor.submit(
            run_grant,
            second_repository,
            _grant_request(
                UUID(int=502),
                owner="other-owner",
                idempotency_key="parallel-b",
            ),
            "parallel-b",
            FINGERPRINT_B,
        )
        results = [first.result(), second.result()]

    assert sum(record is not None for record in results) == 1
    with first_database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reservation_leases WHERE released_at IS NULL"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM coordinated_reservation_leases "
                "WHERE state NOT IN ('RELEASED', 'EXPIRED', 'REVOKED')"
            ).fetchone()[0]
            == 1
        )
    first_database.close()
    second_database.close()
