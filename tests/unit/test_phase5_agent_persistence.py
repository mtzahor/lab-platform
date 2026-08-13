from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from lab_platform.control_plane_core import (
    AgentAuthenticationFailedError,
    AgentEnrollmentService,
    AgentEnrollmentTokenInvalidError,
    AgentEnrollmentTokenUsedError,
    EnrolledAgent,
)
from lab_platform.models import AgentEnrollmentToken, AgentStatus, EnrollmentStatus
from lab_platform.persistence import (
    SCHEMA_VERSION,
    SQLiteAgentEnrollmentRepository,
    SQLiteDatabase,
)
from pydantic import ValidationError

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
ENROLLMENT_SECRET = f"lpe_{'e' * 48}"
FIRST_CREDENTIAL = f"lpa_{'a' * 48}"
REPLACEMENT_CREDENTIAL = f"lpa_{'b' * 48}"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class BarrierEnrollmentRepository(SQLiteAgentEnrollmentRepository):
    """Align independent services after they read the still-unused token."""

    def __init__(self, database: SQLiteDatabase, barrier: Barrier) -> None:
        super().__init__(database)
        self._barrier = barrier

    async def get_token_by_hash(self, token_hash: str) -> AgentEnrollmentToken | None:
        token = await super().get_token_by_hash(token_hash)
        self._barrier.wait(timeout=5)
        return token


def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    database.initialize()
    return database


def _service(
    repository: SQLiteAgentEnrollmentRepository,
    clock: MutableClock,
    *,
    credentials: tuple[str, ...] = (FIRST_CREDENTIAL, REPLACEMENT_CREDENTIAL),
) -> AgentEnrollmentService:
    remaining_credentials = iter(credentials)
    return AgentEnrollmentService(
        repository,
        clock=clock,
        token_factory=lambda: ENROLLMENT_SECRET,
        credential_factory=lambda: next(remaining_credentials),
    )


def test_schema_v5_upgrade_preserves_phase4_data_and_repairs_v6_objects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "phase5-migration.db"
    database = _database(path)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO api_tokens "
            "(id, name, token_hash, owner, scopes_json, created_at) "
            "VALUES ('legacy-token', 'legacy-ci', ?, 'ci-owner', "
            "'[\"ci:sessions\"]', ?)",
            ("a" * 64, NOW.isoformat()),
        )
    database.close()

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE agent_enrollment_tokens;
            DROP TABLE agent_credentials;
            DROP TABLE agents;
            DELETE FROM schema_migrations WHERE version >= 6;
            """
        )
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5

    upgraded = _database(path)
    with upgraded.transaction() as connection:
        assert SCHEMA_VERSION == 10
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
        legacy = connection.execute(
            "SELECT name, token_hash, owner, scopes_json FROM api_tokens WHERE id = 'legacy-token'"
        ).fetchone()
        assert legacy is not None
        assert tuple(legacy) == (
            "legacy-ci",
            "a" * 64,
            "ci-owner",
            '["ci:sessions"]',
        )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"agents", "agent_enrollment_tokens", "agent_credentials"} <= tables
    upgraded.close()

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE agent_credentials")
        connection.execute("DROP INDEX agents_status_seen")

    repaired = _database(path)
    with repaired.transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
            ).fetchone()[0]
            == 1
        )
        objects = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name IN ('agent_credentials', 'agents_status_seen', "
                "'agent_credentials_one_active')"
            )
        }
        assert objects == {
            ("table", "agent_credentials"),
            ("index", "agents_status_seen"),
            ("index", "agent_credentials_one_active"),
        }
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM api_tokens WHERE id = 'legacy-token'"
            ).fetchone()[0]
            == 1
        )
    repaired.close()


def test_sqlite_agent_lifecycle_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "agent-lifecycle.db"
        clock = MutableClock()
        database = _database(path)
        repository = SQLiteAgentEnrollmentRepository(database)
        service = _service(repository, clock)

        issued = await service.issue_token(
            name="home-lab",
            expires_at=NOW + timedelta(minutes=30),
            allowed_labels={"location": "home", "tier": "development"},
        )
        enrolled = await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=10_000),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
            location="Jerusalem",
        )
        assert enrolled.plaintext.get_secret_value() == FIRST_CREDENTIAL
        assert enrolled.agent.status is AgentStatus.OFFLINE
        assert enrolled.agent.labels == {"location": "home", "tier": "development"}

        clock.value = NOW + timedelta(seconds=5)
        authenticated = await service.authenticate(
            enrolled.agent.id,
            enrolled.plaintext.get_secret_value(),
        )
        assert authenticated.last_used_at == clock.value
        assert authenticated.credential_version == 1

        clock.value = NOW + timedelta(minutes=1)
        replacement = await service.rotate_credential(
            enrolled.agent.id,
            current_secret=enrolled.plaintext.get_secret_value(),
        )
        assert replacement.plaintext.get_secret_value() == REPLACEMENT_CREDENTIAL
        assert replacement.credential_version == 2
        with pytest.raises(AgentAuthenticationFailedError):
            await service.authenticate(enrolled.agent.id, FIRST_CREDENTIAL)
        assert (
            await service.authenticate(
                enrolled.agent.id,
                replacement.plaintext.get_secret_value(),
            )
        ).credential_id == replacement.credential_id

        clock.value = NOW + timedelta(minutes=2)
        revoked = await service.revoke_agent(enrolled.agent.id)
        assert revoked.status is AgentStatus.REVOKED
        assert revoked.enrollment_status is EnrollmentStatus.REVOKED
        with pytest.raises(AgentAuthenticationFailedError):
            await service.authenticate(enrolled.agent.id, REPLACEMENT_CREDENTIAL)
        database.close()

        reopened_database = _database(path)
        reopened_repository = SQLiteAgentEnrollmentRepository(reopened_database)
        assert await reopened_repository.get_agent(enrolled.agent.id) == revoked
        persisted_token = await reopened_repository.get_token(issued.token.id)
        assert persisted_token is not None
        assert persisted_token.used_by_agent_id == enrolled.agent.id
        assert persisted_token.enrollment_request_id == UUID(int=10_000)
        with reopened_database.transaction() as connection:
            credentials = connection.execute(
                "SELECT version, revoked_at FROM agent_credentials "
                "WHERE agent_id = ? ORDER BY version",
                (str(enrolled.agent.id),),
            ).fetchall()
            event_types = [
                row[0]
                for row in connection.execute(
                    "SELECT type FROM events WHERE source = 'agent_registry' ORDER BY timestamp, id"
                )
            ]
        assert [row[0] for row in credentials] == [1, 2]
        assert all(row[1] is not None for row in credentials)
        assert sorted(event_types) == sorted(
            [
                "AGENT_ENROLLMENT_TOKEN_CREATED",
                "AGENT_ENROLLED",
                "AGENT_CREDENTIAL_ROTATED",
                "AGENT_REVOKED",
            ]
        )
        reopened_database.close()

    asyncio.run(scenario())


def test_sqlite_rows_database_file_and_audit_events_exclude_plaintext(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "secret-boundary.db"
        clock = MutableClock()
        database = _database(path)
        repository = SQLiteAgentEnrollmentRepository(database)
        service = _service(repository, clock)

        issued = await service.issue_token(
            name="secret-test",
            expires_at=NOW + timedelta(minutes=30),
        )
        enrolled = await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=20_000),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        clock.value = NOW + timedelta(minutes=1)
        replacement = await service.rotate_credential(
            enrolled.agent.id,
            current_secret=enrolled.plaintext.get_secret_value(),
        )

        secrets = (
            issued.plaintext.get_secret_value(),
            enrolled.plaintext.get_secret_value(),
            replacement.plaintext.get_secret_value(),
        )
        secret_hashes = tuple(service.hash_secret(secret) for secret in secrets)
        with database.transaction() as connection:
            persisted_values = [
                row[0]
                for row in connection.execute(
                    "SELECT token_hash FROM agent_enrollment_tokens "
                    "UNION ALL SELECT credential_hash FROM agent_credentials"
                )
            ]
            audit_rows = connection.execute(
                "SELECT type, actor, payload FROM events WHERE source = 'agent_registry'"
            ).fetchall()
        assert set(secret_hashes) <= set(persisted_values)
        persisted_text = "\n".join(persisted_values)
        audit_text = "\n".join(str(value) for row in audit_rows for value in row)
        for secret in secrets:
            assert secret not in persisted_text
            assert secret not in audit_text
        for secret_hash in secret_hashes:
            assert secret_hash not in audit_text

        database.close()
        database_bytes = b"".join(
            candidate.read_bytes()
            for candidate in path.parent.glob(f"{path.name}*")
            if candidate.is_file()
        )
        for secret in secrets:
            assert secret.encode() not in database_bytes

    asyncio.run(scenario())


def test_sqlite_authentication_rejects_pending_or_unenrolled_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "agent-auth-state.db")
        clock = MutableClock()
        service = _service(SQLiteAgentEnrollmentRepository(database), clock)
        issued = await service.issue_token(
            name="auth-state",
            expires_at=NOW + timedelta(minutes=30),
        )
        enrolled = await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=25_000),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )

        for status, enrollment_status in (
            (AgentStatus.PENDING.value, EnrollmentStatus.ENROLLED.value),
            (AgentStatus.OFFLINE.value, EnrollmentStatus.PENDING.value),
        ):
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE agents SET status = ?, enrollment_status = ? WHERE id = ?",
                    (status, enrollment_status, str(enrolled.agent.id)),
                )
            with pytest.raises(AgentAuthenticationFailedError):
                await service.authenticate(enrolled.agent.id, FIRST_CREDENTIAL)

        database.close()

    asyncio.run(scenario())


def test_sqlite_registry_lists_and_revokes_tokens_and_agents(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "agent-registry.db")
        repository = SQLiteAgentEnrollmentRepository(database)
        tokens = iter((ENROLLMENT_SECRET, f"lpe_{'f' * 48}"))
        service = AgentEnrollmentService(
            repository,
            clock=MutableClock(),
            token_factory=lambda: next(tokens),
            credential_factory=lambda: FIRST_CREDENTIAL,
        )
        first = await service.issue_token(
            name="first-agent",
            expires_at=NOW + timedelta(minutes=30),
        )
        second = await service.issue_token(
            name="second-agent",
            expires_at=NOW + timedelta(minutes=30),
        )

        listed_tokens = await service.list_tokens()
        assert {token.id for token in listed_tokens} == {first.token.id, second.token.id}
        revoked = await service.revoke_token(second.token.id)
        assert revoked.revoked_at == NOW
        assert await service.revoke_token(second.token.id) == revoked
        with pytest.raises(AgentEnrollmentTokenInvalidError):
            await service.revoke_token(UUID(int=99_999))

        enrolled = await service.enroll(
            plaintext_token=first.plaintext.get_secret_value(),
            request_id=UUID(int=25_500),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        assert await service.list_agents() == [enrolled.agent]
        with pytest.raises(ValueError, match="limit must be positive"):
            await repository.list_tokens(limit=0)
        with pytest.raises(ValueError, match="limit must be positive"):
            await repository.list_agents(limit=0)
        database.close()

    asyncio.run(scenario())


def test_agent_state_rolls_back_when_audit_event_id_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "agent-audit-conflict.db")
        repository = SQLiteAgentEnrollmentRepository(database)
        token_id = UUID(int=26_001)
        conflicting_event_id = UUID(int=26_002)
        identifiers = iter((token_id, conflicting_event_id))
        service = AgentEnrollmentService(
            repository,
            clock=MutableClock(),
            token_factory=lambda: ENROLLMENT_SECRET,
            credential_factory=lambda: FIRST_CREDENTIAL,
            id_factory=lambda: next(identifiers),
        )
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO events (id, timestamp, type, source, payload) "
                "VALUES (?, ?, 'UNRELATED_EVENT', 'test', '{}')",
                (str(conflicting_event_id), NOW.isoformat()),
            )

        with pytest.raises(sqlite3.IntegrityError, match="audit event conflicts"):
            await service.issue_token(
                name="must-roll-back",
                expires_at=NOW + timedelta(minutes=30),
            )

        with database.transaction() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM agent_enrollment_tokens").fetchone()[0]
                == 0
            )
            assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        database.close()

    asyncio.run(scenario())


def test_clock_rollback_cannot_commit_invalid_agent_timestamps(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "agent-clock-rollback.db")
        repository = SQLiteAgentEnrollmentRepository(database)
        clock = MutableClock()
        service = _service(repository, clock)
        issued = await service.issue_token(
            name="clock-test",
            expires_at=NOW + timedelta(minutes=30),
        )

        clock.value = NOW - timedelta(seconds=1)
        with pytest.raises(ValidationError, match="used_at cannot be earlier"):
            await service.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=UUID(int=27_000),
                agent_version="0.6.0-alpha",
                protocol_version="1.0",
            )
        with database.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
            assert (
                connection.execute("SELECT used_at FROM agent_enrollment_tokens").fetchone()[0]
                is None
            )

        clock.value = NOW
        enrolled = await service.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=UUID(int=27_001),
            agent_version="0.6.0-alpha",
            protocol_version="1.0",
        )
        clock.value = NOW + timedelta(seconds=10)
        await service.authenticate(enrolled.agent.id, enrolled.plaintext.get_secret_value())
        clock.value = NOW + timedelta(seconds=5)
        with pytest.raises(ValidationError, match="last_used_at cannot be later"):
            await service.revoke_agent(enrolled.agent.id)
        persisted = await repository.get_agent(enrolled.agent.id)
        assert persisted is not None
        assert persisted.status is AgentStatus.OFFLINE
        assert persisted.revoked_at is None
        database.close()

    asyncio.run(scenario())


def test_concurrent_same_token_enrollment_has_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "enrollment-race.db"
    seed_database = _database(path)
    seed_service = _service(
        SQLiteAgentEnrollmentRepository(seed_database),
        MutableClock(),
        credentials=(FIRST_CREDENTIAL,),
    )
    issued = asyncio.run(
        seed_service.issue_token(
            name="race-lab",
            expires_at=NOW + timedelta(minutes=30),
            allowed_labels={"purpose": "race-test"},
        )
    )
    seed_database.close()

    worker_count = 8
    barrier = Barrier(worker_count)
    databases = [_database(path) for _ in range(worker_count)]

    def enroll(index: int) -> EnrolledAgent | Exception:
        repository = BarrierEnrollmentRepository(databases[index], barrier)
        service = _service(
            repository,
            MutableClock(),
            credentials=(FIRST_CREDENTIAL,),
        )
        try:
            return asyncio.run(
                service.enroll(
                    plaintext_token=issued.plaintext.get_secret_value(),
                    request_id=UUID(int=30_000 + index),
                    agent_version="0.6.0-alpha",
                    protocol_version="1.0",
                )
            )
        except Exception as exc:  # losing domain errors are the result under test
            return exc

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(enroll, range(worker_count)))

        winners = [result for result in results if isinstance(result, EnrolledAgent)]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == 1
        assert len(losers) == worker_count - 1
        assert all(isinstance(result, AgentEnrollmentTokenUsedError) for result in losers)
        with databases[0].transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM agent_credentials").fetchone()[0] == 1
            used = connection.execute(
                "SELECT used_by_agent_id, enrollment_request_id "
                "FROM agent_enrollment_tokens WHERE used_at IS NOT NULL"
            ).fetchall()
            assert len(used) == 1
            assert used[0][0] == str(winners[0].agent.id)
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE type = 'AGENT_ENROLLED'"
                ).fetchone()[0]
                == 1
            )
    finally:
        for database in databases:
            database.close()
