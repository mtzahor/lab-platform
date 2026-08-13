from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import datetime
from uuid import UUID

from lab_platform.control_plane_core.enrollment import (
    EnrollmentConsumeResult,
    EnrollmentConsumeStatus,
)
from lab_platform.models import (
    AgentCredential,
    AgentCredentialKind,
    AgentEnrollmentToken,
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
    EventRecord,
)
from lab_platform.persistence.database import SQLiteDatabase, insert_event


class _EnrollmentWriteConflict(Exception):
    """Roll back a consume transaction whose final compare-and-set lost."""


class SQLiteAgentEnrollmentRepository:
    """SQLite Agent registry with atomic, replay-safe enrollment operations."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create_token(
        self,
        token: AgentEnrollmentToken,
        audit_event: EventRecord,
    ) -> AgentEnrollmentToken:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO agent_enrollment_tokens "
                "(id, organisation_id, name, token_hash, created_at, expires_at, "
                "used_at, revoked_at, "
                "allowed_labels_json, used_by_agent_id, enrollment_request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _token_values(token),
            )
            _insert_agent_event(connection, audit_event)
        return token

    async def get_token(
        self,
        token_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentEnrollmentToken | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(token_id), str(organisation_id))
            if organisation_id is not None
            else (str(token_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM agent_enrollment_tokens WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def get_token_by_hash(self, token_hash: str) -> AgentEnrollmentToken | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_enrollment_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return _token_from_row(row) if row is not None else None

    async def list_tokens(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 500,
    ) -> list[AgentEnrollmentToken]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._database.transaction() as connection:
            scope = "WHERE organisation_id = ? " if organisation_id is not None else ""
            values: tuple[object, ...] = (
                (str(organisation_id), limit) if organisation_id is not None else (limit,)
            )
            rows = connection.execute(
                f"SELECT * FROM agent_enrollment_tokens {scope}"  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_token_from_row(row) for row in rows]

    async def revoke_token(
        self,
        token_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentEnrollmentToken | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(token_id), str(organisation_id))
            if organisation_id is not None
            else (str(token_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                f"SELECT * FROM agent_enrollment_tokens WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
            if row is None:
                return None
            token = _token_from_row(row)
            if token.revoked_at is not None:
                return token
            updated_token = AgentEnrollmentToken.model_validate(
                {**token.model_dump(), "revoked_at": revoked_at}
            )
            connection.execute(
                "UPDATE agent_enrollment_tokens SET revoked_at = ? WHERE id = ?",
                (revoked_at.isoformat(), str(token_id)),
            )
            _insert_agent_event(connection, audit_event)
        return updated_token

    async def consume_token(
        self,
        *,
        token_hash: str,
        now: datetime,
        request_id: UUID,
        agent: AgentRecord,
        credential: AgentCredential,
        audit_event: EventRecord,
    ) -> EnrollmentConsumeResult:
        try:
            with self._database.transaction(immediate=True) as connection:
                replay_row = connection.execute(
                    "SELECT * FROM agent_enrollment_tokens WHERE enrollment_request_id = ?",
                    (str(request_id),),
                ).fetchone()
                if replay_row is not None:
                    return _replay_result(
                        replay_row,
                        token_hash=token_hash,
                    )

                row = connection.execute(
                    "SELECT * FROM agent_enrollment_tokens WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.INVALID)
                token = _token_from_row(row)
                if token.revoked_at is not None:
                    return EnrollmentConsumeResult(
                        status=EnrollmentConsumeStatus.REVOKED,
                        token=token,
                    )
                if token.used_at is not None:
                    return EnrollmentConsumeResult(
                        status=EnrollmentConsumeStatus.USED,
                        token=token,
                    )
                if token.expires_at <= now:
                    return EnrollmentConsumeResult(
                        status=EnrollmentConsumeStatus.EXPIRED,
                        token=token,
                    )
                consumed_token = AgentEnrollmentToken.model_validate(
                    {
                        **token.model_dump(),
                        "used_at": now,
                        "used_by_agent_id": agent.id,
                        "enrollment_request_id": request_id,
                    }
                )
                if not _enrollment_is_bound(token, agent, credential):
                    return EnrollmentConsumeResult(
                        status=EnrollmentConsumeStatus.CONFLICT,
                        token=token,
                    )

                _insert_agent(connection, agent)
                _insert_credential(connection, credential)
                consumed = connection.execute(
                    "UPDATE agent_enrollment_tokens "
                    "SET used_at = ?, used_by_agent_id = ?, enrollment_request_id = ? "
                    "WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL "
                    "AND expires_at > ?",
                    (
                        now.isoformat(),
                        str(agent.id),
                        str(request_id),
                        str(token.id),
                        now.isoformat(),
                    ),
                )
                if consumed.rowcount != 1:
                    raise _EnrollmentWriteConflict
                _insert_agent_event(connection, audit_event)
                return EnrollmentConsumeResult(
                    status=EnrollmentConsumeStatus.ENROLLED,
                    token=consumed_token,
                    agent=agent,
                    credential=credential,
                )
        except _EnrollmentWriteConflict:
            return EnrollmentConsumeResult(status=EnrollmentConsumeStatus.CONFLICT)

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(agent_id), str(organisation_id))
            if organisation_id is not None
            else (str(agent_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM agents WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _agent_from_row(row) if row is not None else None

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
        limit: int = 500,
    ) -> list[AgentRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._database.transaction() as connection:
            scope = "WHERE organisation_id = ? " if organisation_id is not None else ""
            values: tuple[object, ...] = (
                (str(organisation_id), limit) if organisation_id is not None else (limit,)
            )
            rows = connection.execute(
                f"SELECT * FROM agents {scope}"  # noqa: S608
                "ORDER BY name COLLATE NOCASE, slug, id LIMIT ?",
                values,
            ).fetchall()
        return [_agent_from_row(row) for row in rows]

    async def authenticate_agent(
        self,
        agent_id: UUID,
        credential_hash: str,
        used_at: datetime,
    ) -> tuple[AgentRecord, AgentCredential] | None:
        """Authenticate and record credential use as one serialized operation."""

        timestamp = used_at.isoformat()
        with self._database.transaction(immediate=True) as connection:
            agent_row = connection.execute(
                "SELECT * FROM agents WHERE id = ? "
                "AND status NOT IN ('PENDING', 'REVOKED') "
                "AND enrollment_status = 'ENROLLED'",
                (str(agent_id),),
            ).fetchone()
            credential_row = connection.execute(
                "SELECT * FROM agent_credentials WHERE agent_id = ? "
                "AND revoked_at IS NULL ORDER BY version DESC LIMIT 1",
                (str(agent_id),),
            ).fetchone()
            if agent_row is None or credential_row is None:
                return None

            agent = _agent_from_row(agent_row)
            credential = _credential_from_row(credential_row)
            if (
                used_at < credential.created_at
                or credential.expires_at is not None
                and credential.expires_at <= used_at
                or not hmac.compare_digest(credential.credential_hash, credential_hash)
            ):
                return None

            updated = connection.execute(
                "UPDATE agent_credentials "
                "SET last_used_at = CASE "
                "WHEN last_used_at IS NULL OR last_used_at < ? THEN ? ELSE last_used_at END "
                "WHERE id = ? AND agent_id = ? AND revoked_at IS NULL",
                (
                    timestamp,
                    timestamp,
                    str(credential.id),
                    str(agent_id),
                ),
            )
            if updated.rowcount != 1:
                return None
            updated_row = connection.execute(
                "SELECT * FROM agent_credentials WHERE id = ?",
                (str(credential.id),),
            ).fetchone()
        assert updated_row is not None
        return agent, _credential_from_row(updated_row)

    async def rotate_credential(
        self,
        *,
        agent_id: UUID,
        expected_credential_id: UUID,
        replacement: AgentCredential,
        rotated_at: datetime,
        audit_event: EventRecord,
        organisation_id: UUID | None = None,
    ) -> AgentCredential | None:
        try:
            with self._database.transaction(immediate=True) as connection:
                scope = " AND organisation_id = ?" if organisation_id is not None else ""
                agent_values: tuple[object, ...] = (
                    (str(agent_id), str(organisation_id))
                    if organisation_id is not None
                    else (str(agent_id),)
                )
                agent_row = connection.execute(
                    f"SELECT * FROM agents WHERE id = ?{scope}",  # noqa: S608
                    agent_values,
                ).fetchone()
                current_row = connection.execute(
                    "SELECT * FROM agent_credentials "
                    "WHERE id = ? AND agent_id = ? AND revoked_at IS NULL",
                    (str(expected_credential_id), str(agent_id)),
                ).fetchone()
                if agent_row is None or current_row is None:
                    return None
                registered_agent = _agent_from_row(agent_row)
                current = _credential_from_row(current_row)
                if (
                    registered_agent.status in {AgentStatus.PENDING, AgentStatus.REVOKED}
                    or registered_agent.enrollment_status is not EnrollmentStatus.ENROLLED
                    or current.expires_at is not None
                    and current.expires_at <= rotated_at
                    or replacement.agent_id != agent_id
                    or replacement.organisation_id != registered_agent.organisation_id
                    or current.organisation_id != registered_agent.organisation_id
                    or replacement.version != current.version + 1
                    or replacement.created_at != rotated_at
                    or replacement.revoked_at is not None
                ):
                    return None

                AgentCredential.model_validate({**current.model_dump(), "revoked_at": rotated_at})

                revoked = connection.execute(
                    "UPDATE agent_credentials SET revoked_at = ? "
                    "WHERE id = ? AND agent_id = ? AND revoked_at IS NULL",
                    (rotated_at.isoformat(), str(expected_credential_id), str(agent_id)),
                )
                if revoked.rowcount != 1:
                    raise _EnrollmentWriteConflict
                _insert_credential(connection, replacement)
                _insert_agent_event(connection, audit_event)
                return replacement
        except _EnrollmentWriteConflict:
            return None

    async def revoke_agent(
        self,
        agent_id: UUID,
        revoked_at: datetime,
        audit_event: EventRecord,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord | None:
        timestamp = revoked_at.isoformat()
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(agent_id), str(organisation_id))
            if organisation_id is not None
            else (str(agent_id),)
        )
        with self._database.transaction(immediate=True) as connection:
            agent_row = connection.execute(
                f"SELECT * FROM agents WHERE id = ?{scope}",  # noqa: S608
                values,
            ).fetchone()
            if agent_row is None:
                return None
            agent = _agent_from_row(agent_row)
            if agent.status is AgentStatus.REVOKED:
                return agent
            revoked_agent = AgentRecord.model_validate(
                {
                    **agent.model_dump(),
                    "status": AgentStatus.REVOKED,
                    "enrollment_status": EnrollmentStatus.REVOKED,
                    "revoked_at": revoked_at,
                }
            )
            credential_rows = connection.execute(
                "SELECT * FROM agent_credentials WHERE agent_id = ? AND revoked_at IS NULL",
                (str(agent_id),),
            ).fetchall()
            for credential_row in credential_rows:
                credential = _credential_from_row(credential_row)
                AgentCredential.model_validate(
                    {**credential.model_dump(), "revoked_at": revoked_at}
                )
            connection.execute(
                "UPDATE agents SET status = ?, enrollment_status = ?, "
                "revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (
                    AgentStatus.REVOKED.value,
                    EnrollmentStatus.REVOKED.value,
                    timestamp,
                    str(agent_id),
                ),
            )
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE agent_id = ?",
                (timestamp, str(agent_id)),
            )
            _insert_agent_event(connection, audit_event)
        return revoked_agent


def _insert_agent_event(connection: sqlite3.Connection, event: EventRecord) -> None:
    """Insert an audit event, accepting only a semantically identical deduplicated replay."""

    insert_event(connection, event)
    row = connection.execute(
        "SELECT * FROM events WHERE id = ?",
        (str(event.id),),
    ).fetchone()
    if row is not None and _event_row_matches(row, event, exact_identity=True):
        return
    if event.deduplication_key is not None:
        row = connection.execute(
            "SELECT * FROM events WHERE deduplication_key = ?",
            (event.deduplication_key,),
        ).fetchone()
        if row is not None and _event_row_matches(row, event, exact_identity=False):
            return
    raise sqlite3.IntegrityError("Agent audit event conflicts with an existing event")


def _event_row_matches(
    row: sqlite3.Row,
    event: EventRecord,
    *,
    exact_identity: bool,
) -> bool:
    if exact_identity and (
        row["id"] != str(event.id) or row["timestamp"] != event.timestamp.isoformat()
    ):
        return False
    return bool(
        row["type"] == event.type
        and row["source"] == event.source
        and row["bench_id"] == event.bench_id
        and row["reservation_id"]
        == (str(event.reservation_id) if event.reservation_id is not None else None)
        and row["operation_id"]
        == (str(event.operation_id) if event.operation_id is not None else None)
        and row["actor"] == event.actor
        and json.loads(row["payload"]) == event.payload
        and row["deduplication_key"] == event.deduplication_key
    )


def _replay_result(
    token_row: sqlite3.Row,
    *,
    token_hash: str,
) -> EnrollmentConsumeResult:
    """Report an already committed request without returning secret-bound state."""

    token = _token_from_row(token_row)
    if (
        not hmac.compare_digest(token.token_hash, token_hash)
        or token.used_by_agent_id is None
        or token.used_at is None
    ):
        return EnrollmentConsumeResult(
            status=EnrollmentConsumeStatus.CONFLICT,
            token=token,
        )
    return EnrollmentConsumeResult(
        status=EnrollmentConsumeStatus.REPLAYED,
        token=token,
    )


def _enrollment_is_bound(
    token: AgentEnrollmentToken,
    agent: AgentRecord,
    credential: AgentCredential,
) -> bool:
    return (
        agent.organisation_id == token.organisation_id
        and credential.organisation_id == token.organisation_id
        and agent.name == token.name
        and agent.labels == token.allowed_labels
        and agent.enrollment_status is EnrollmentStatus.ENROLLED
        and agent.status is not AgentStatus.REVOKED
        and agent.revoked_at is None
        and credential.agent_id == agent.id
        and credential.kind is AgentCredentialKind.OPAQUE_TOKEN
        and credential.version == 1
        and credential.revoked_at is None
    )


def _insert_agent(connection: sqlite3.Connection, agent: AgentRecord) -> None:
    connection.execute(
        "INSERT INTO agents "
        "(id, organisation_id, slug, name, status, version, protocol_version, "
        "location, labels_json, "
        "registered_at, last_connected_at, last_seen_at, disconnected_at, "
        "certificate_fingerprint, enrollment_status, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _agent_values(agent),
    )


def _insert_credential(
    connection: sqlite3.Connection,
    credential: AgentCredential,
) -> None:
    connection.execute(
        "INSERT INTO agent_credentials "
        "(id, organisation_id, agent_id, kind, credential_hash, version, created_at, expires_at, "
        "revoked_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _credential_values(credential),
    )


def _token_values(token: AgentEnrollmentToken) -> tuple[object, ...]:
    return (
        str(token.id),
        str(token.organisation_id),
        token.name,
        token.token_hash,
        token.created_at.isoformat(),
        token.expires_at.isoformat(),
        _datetime_value(token.used_at),
        _datetime_value(token.revoked_at),
        json.dumps(token.allowed_labels, sort_keys=True, separators=(",", ":")),
        str(token.used_by_agent_id) if token.used_by_agent_id is not None else None,
        str(token.enrollment_request_id) if token.enrollment_request_id is not None else None,
    )


def _agent_values(agent: AgentRecord) -> tuple[object, ...]:
    return (
        str(agent.id),
        str(agent.organisation_id),
        agent.slug,
        agent.name,
        agent.status.value,
        agent.version,
        agent.protocol_version,
        agent.location,
        json.dumps(agent.labels, sort_keys=True, separators=(",", ":")),
        agent.registered_at.isoformat(),
        _datetime_value(agent.last_connected_at),
        _datetime_value(agent.last_seen_at),
        _datetime_value(agent.disconnected_at),
        agent.certificate_fingerprint,
        agent.enrollment_status.value,
        _datetime_value(agent.revoked_at),
    )


def _credential_values(credential: AgentCredential) -> tuple[object, ...]:
    return (
        str(credential.id),
        str(credential.organisation_id),
        str(credential.agent_id),
        credential.kind.value,
        credential.credential_hash,
        credential.version,
        credential.created_at.isoformat(),
        _datetime_value(credential.expires_at),
        _datetime_value(credential.revoked_at),
        _datetime_value(credential.last_used_at),
    )


def _token_from_row(row: sqlite3.Row) -> AgentEnrollmentToken:
    return AgentEnrollmentToken(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        name=row["name"],
        token_hash=row["token_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        used_at=_parse_datetime(row["used_at"]),
        revoked_at=_parse_datetime(row["revoked_at"]),
        allowed_labels=json.loads(row["allowed_labels_json"]),
        used_by_agent_id=(
            UUID(row["used_by_agent_id"]) if row["used_by_agent_id"] is not None else None
        ),
        enrollment_request_id=(
            UUID(row["enrollment_request_id"]) if row["enrollment_request_id"] is not None else None
        ),
    )


def _agent_from_row(row: sqlite3.Row) -> AgentRecord:
    return AgentRecord(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        slug=row["slug"],
        name=row["name"],
        status=AgentStatus(row["status"]),
        version=row["version"],
        protocol_version=row["protocol_version"],
        location=row["location"],
        labels=json.loads(row["labels_json"]),
        registered_at=datetime.fromisoformat(row["registered_at"]),
        last_connected_at=_parse_datetime(row["last_connected_at"]),
        last_seen_at=_parse_datetime(row["last_seen_at"]),
        disconnected_at=_parse_datetime(row["disconnected_at"]),
        certificate_fingerprint=row["certificate_fingerprint"],
        enrollment_status=EnrollmentStatus(row["enrollment_status"]),
        revoked_at=_parse_datetime(row["revoked_at"]),
    )


def _credential_from_row(row: sqlite3.Row) -> AgentCredential:
    return AgentCredential(
        id=UUID(row["id"]),
        organisation_id=UUID(row["organisation_id"]),
        agent_id=UUID(row["agent_id"]),
        kind=AgentCredentialKind(row["kind"]),
        credential_hash=row["credential_hash"],
        version=row["version"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        revoked_at=_parse_datetime(row["revoked_at"]),
        last_used_at=_parse_datetime(row["last_used_at"]),
    )


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
