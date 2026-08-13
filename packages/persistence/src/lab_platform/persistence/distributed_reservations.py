from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_platform.control_plane_core.errors import ReservationLeaseInvalidError
from lab_platform.control_plane_core.reservations import (
    CoordinatedReservationLease,
    LeaseWriteDisposition,
    LeaseWriteResult,
    ReservationGrantRequest,
    ReservationLeaseState,
)
from lab_platform.models import (
    AgentStatus,
    GlobalBenchStatus,
    Reservation,
    ReservationLease,
)
from lab_platform.persistence.database import SQLiteDatabase

_TERMINAL_STATES = frozenset(
    {
        ReservationLeaseState.RELEASED,
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.REVOKED,
    }
)

_ALLOWED_TRANSITIONS: dict[
    ReservationLeaseState,
    frozenset[ReservationLeaseState],
] = {
    ReservationLeaseState.ACTIVATING: frozenset(
        {
            ReservationLeaseState.ACTIVE,
            ReservationLeaseState.UNKNOWN,
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }
    ),
    ReservationLeaseState.ACTIVE: frozenset(
        {
            ReservationLeaseState.RENEWING,
            ReservationLeaseState.UNKNOWN,
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }
    ),
    ReservationLeaseState.RENEWING: frozenset(
        {
            ReservationLeaseState.ACTIVE,
            ReservationLeaseState.UNKNOWN,
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }
    ),
    ReservationLeaseState.UNKNOWN: frozenset(
        {
            ReservationLeaseState.ACTIVE,
            ReservationLeaseState.RELEASED,
            ReservationLeaseState.EXPIRED,
            ReservationLeaseState.REVOKED,
        }
    ),
    ReservationLeaseState.RELEASED: frozenset(),
    ReservationLeaseState.EXPIRED: frozenset(),
    ReservationLeaseState.REVOKED: frozenset(),
}

_IMMUTABLE_RESERVATION_FIELDS = (
    "id",
    "organisation_id",
    "bench_id",
    "owner",
    "owner_principal_id",
    "owner_principal_type",
    "created_at",
    "requested_at",
    "starts_at",
    "ends_at",
    "source",
    "metadata",
    "idempotency_key",
)


class SQLiteCentralReservationLeaseRepository:
    """Durable, transactionally fenced global reservation coordination.

    SQLite's ``BEGIN IMMEDIATE`` serializes grant and replacement decisions across
    every connection to the database. Eligibility, ownership, version allocation,
    base reservation state, the coordination CAS, and its idempotency result are
    consequently committed as one unit.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def get(
        self,
        reservation_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> CoordinatedReservationLease | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (str(reservation_id), str(organisation_id))
            if organisation_id is not None
            else (str(reservation_id),)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM coordinated_reservation_leases WHERE reservation_id = ?"
                f"{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _record_from_json(row["record_json"]) if row is not None else None

    async def get_current_for_bench(
        self,
        bench_id: str,
        *,
        organisation_id: UUID | None = None,
    ) -> CoordinatedReservationLease | None:
        scope = " AND organisation_id = ?" if organisation_id is not None else ""
        values: tuple[object, ...] = (
            (bench_id, str(organisation_id)) if organisation_id is not None else (bench_id,)
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM coordinated_reservation_leases "
                "WHERE bench_id = ? AND state NOT IN ('RELEASED', 'EXPIRED', 'REVOKED')"
                f"{scope}",  # noqa: S608
                values,
            ).fetchone()
        return _record_from_json(row["record_json"]) if row is not None else None

    async def get_mutation_result(
        self,
        mutation_key: str,
        *,
        organisation_id: UUID,
        request_fingerprint: str,
    ) -> LeaseWriteResult | None:
        with self._database.transaction() as connection:
            return _mutation_result(
                connection,
                mutation_key,
                organisation_id=organisation_id,
                request_fingerprint=request_fingerprint,
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
        try:
            with self._database.transaction(immediate=True) as connection:
                organisation_id = _route_organisation_id(
                    connection,
                    request.agent_id,
                    request.reservation.bench_id,
                )
                if organisation_id is None:
                    return None
                replay = _mutation_result(
                    connection,
                    mutation_key,
                    organisation_id=organisation_id,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                if not _route_is_eligible(
                    connection,
                    request.agent_id,
                    request.reservation.bench_id,
                    expected_agent_status=expected_agent_status,
                    expected_bench_status=expected_bench_status,
                ):
                    return None
                reservation = request.reservation.model_copy(
                    update={"organisation_id": organisation_id}
                )
                if _has_current_coordination(
                    connection,
                    reservation.bench_id,
                ) or _has_uncoordinated_current_reservation(
                    connection,
                    reservation.bench_id,
                ):
                    return None

                maximum_row = connection.execute(
                    "SELECT MAX(lease_version) AS maximum_version "
                    "FROM reservation_leases WHERE bench_id = ?",
                    (reservation.bench_id,),
                ).fetchone()
                maximum_version = maximum_row["maximum_version"]
                lease_version = (int(maximum_version) if maximum_version is not None else 0) + 1
                valid_from = reservation.starts_at or reservation.created_at
                lease = ReservationLease(
                    reservation_id=reservation.id,
                    agent_id=request.agent_id,
                    bench_id=reservation.bench_id,
                    owner=reservation.owner,
                    valid_from=valid_from,
                    valid_until=request.lease_valid_until,
                    lease_version=lease_version,
                )
                record = CoordinatedReservationLease(
                    reservation=reservation,
                    lease=lease,
                    state=ReservationLeaseState.ACTIVATING,
                    revision=1,
                )
                _insert_reservation(connection, reservation)
                _insert_lease(connection, lease)
                _insert_coordination(connection, record)
                _insert_mutation(
                    connection,
                    mutation_key,
                    request_fingerprint=request_fingerprint,
                    record=record,
                )
        except sqlite3.IntegrityError:
            # Unique indexes and foreign keys are the final guard against a writer
            # using another database connection between application-level checks.
            return None
        return LeaseWriteResult(record, LeaseWriteDisposition.APPLIED)

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
        try:
            with self._database.transaction(immediate=True) as connection:
                replay = _mutation_result(
                    connection,
                    mutation_key,
                    organisation_id=record.reservation.organisation_id,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                row = connection.execute(
                    "SELECT * FROM coordinated_reservation_leases WHERE reservation_id = ?",
                    (str(record.reservation.id),),
                ).fetchone()
                if row is None or int(row["revision"]) != expected_revision:
                    return None
                current = _record_from_json(row["record_json"])
                _validate_replacement(current, record, expected_revision=expected_revision)
                if not _optional_route_predicates_match(
                    connection,
                    record.lease.agent_id,
                    record.lease.bench_id,
                    expected_agent_status=expected_agent_status,
                    expected_bench_status=expected_bench_status,
                ):
                    return None

                cursor = connection.execute(
                    "UPDATE coordinated_reservation_leases SET agent_id = ?, bench_id = ?, "
                    "state = ?, revision = ?, lease_version = ?, record_json = ?, "
                    "updated_at = ? WHERE reservation_id = ? AND revision = ? AND state = ?",
                    (
                        str(record.lease.agent_id),
                        record.lease.bench_id,
                        record.state.value,
                        record.revision,
                        record.lease.lease_version,
                        _record_json(record),
                        _record_timestamp(record).isoformat(),
                        str(record.reservation.id),
                        expected_revision,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                _replace_lease_history(connection, current, record)
                _update_reservation(connection, record.reservation)
                _insert_mutation(
                    connection,
                    mutation_key,
                    request_fingerprint=request_fingerprint,
                    record=record,
                )
        except sqlite3.IntegrityError:
            return None
        return LeaseWriteResult(record, LeaseWriteDisposition.APPLIED)

    async def list(
        self,
        *,
        organisation_id: UUID | None = None,
        agent_id: UUID | None = None,
        states: Iterable[ReservationLeaseState] | None = None,
        limit: int = 10_000,
    ) -> list[CoordinatedReservationLease]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        conditions: list[str] = []
        values: list[object] = []
        if organisation_id is not None:
            conditions.append("organisation_id = ?")
            values.append(str(organisation_id))
        if agent_id is not None:
            conditions.append("agent_id = ?")
            values.append(str(agent_id))
        if states is not None:
            selected = tuple(sorted({state.value for state in states}))
            if not selected:
                return []
            conditions.append(f"state IN ({','.join('?' for _ in selected)})")
            values.extend(selected)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM coordinated_reservation_leases{where} "  # noqa: S608
                "ORDER BY updated_at DESC, reservation_id LIMIT ?",
                values,
            ).fetchall()
        return [_record_from_json(row["record_json"]) for row in rows]


def _route_is_eligible(
    connection: sqlite3.Connection,
    agent_id: UUID,
    bench_id: str,
    *,
    expected_agent_status: AgentStatus,
    expected_bench_status: GlobalBenchStatus,
) -> bool:
    row = connection.execute(
        "SELECT agents.status AS agent_status, global_benches.status AS bench_status, "
        "global_benches.agent_id AS bench_agent_id, "
        "agents.organisation_id AS agent_organisation_id, "
        "global_benches.organisation_id AS bench_organisation_id FROM agents "
        "JOIN global_benches ON global_benches.id = ? "
        "WHERE agents.id = ?",
        (bench_id, str(agent_id)),
    ).fetchone()
    return bool(
        row is not None
        and row["agent_status"] == expected_agent_status.value
        and row["bench_status"] == expected_bench_status.value
        and row["bench_agent_id"] == str(agent_id)
        and row["agent_organisation_id"] == row["bench_organisation_id"]
    )


def _route_organisation_id(
    connection: sqlite3.Connection,
    agent_id: UUID,
    bench_id: str,
) -> UUID | None:
    row = connection.execute(
        "SELECT agents.organisation_id AS agent_organisation_id, "
        "global_benches.organisation_id AS bench_organisation_id "
        "FROM agents JOIN global_benches ON global_benches.id = ? "
        "AND global_benches.agent_id = agents.id WHERE agents.id = ?",
        (bench_id, str(agent_id)),
    ).fetchone()
    if row is None or row["agent_organisation_id"] != row["bench_organisation_id"]:
        return None
    return UUID(row["bench_organisation_id"])


def _optional_route_predicates_match(
    connection: sqlite3.Connection,
    agent_id: UUID,
    bench_id: str,
    *,
    expected_agent_status: AgentStatus | None,
    expected_bench_status: GlobalBenchStatus | None,
) -> bool:
    if expected_agent_status is None and expected_bench_status is None:
        return True
    row = connection.execute(
        "SELECT agents.status AS agent_status, global_benches.status AS bench_status, "
        "global_benches.agent_id AS bench_agent_id, "
        "agents.organisation_id AS agent_organisation_id, "
        "global_benches.organisation_id AS bench_organisation_id FROM agents "
        "JOIN global_benches ON global_benches.id = ? "
        "WHERE agents.id = ?",
        (bench_id, str(agent_id)),
    ).fetchone()
    if row is None or row["bench_agent_id"] != str(agent_id):
        return False
    return bool(
        (expected_agent_status is None or row["agent_status"] == expected_agent_status.value)
        and (expected_bench_status is None or row["bench_status"] == expected_bench_status.value)
        and row["agent_organisation_id"] == row["bench_organisation_id"]
    )


def _has_current_coordination(connection: sqlite3.Connection, bench_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM coordinated_reservation_leases WHERE bench_id = ? "
            "AND state NOT IN ('RELEASED', 'EXPIRED', 'REVOKED')",
            (bench_id,),
        ).fetchone()
        is not None
    )


def _has_uncoordinated_current_reservation(
    connection: sqlite3.Connection,
    bench_id: str,
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM reservations AS reservation WHERE reservation.bench_id = ? "
            "AND reservation.status IN ('scheduled', 'active', 'expired_pending_operation') "
            "AND NOT EXISTS (SELECT 1 FROM coordinated_reservation_leases AS coordinated "
            "WHERE coordinated.reservation_id = reservation.id)",
            (bench_id,),
        ).fetchone()
        is not None
    )


def _validate_replacement(
    current: CoordinatedReservationLease,
    candidate: CoordinatedReservationLease,
    *,
    expected_revision: int,
) -> None:
    if candidate.revision != expected_revision + 1:
        raise ReservationLeaseInvalidError(
            "Replacement revision must advance the current reservation exactly once.",
            reservation_id=str(candidate.reservation.id),
            expected_revision=expected_revision + 1,
            received_revision=candidate.revision,
        )
    if candidate.state not in _ALLOWED_TRANSITIONS[current.state]:
        raise ReservationLeaseInvalidError(
            "Reservation lease state transition is not permitted.",
            reservation_id=str(candidate.reservation.id),
            current_state=current.state.value,
            requested_state=candidate.state.value,
        )
    for field in _IMMUTABLE_RESERVATION_FIELDS:
        if getattr(current.reservation, field) != getattr(candidate.reservation, field):
            raise ReservationLeaseInvalidError(
                "Replacement changed immutable reservation identity.",
                reservation_id=str(candidate.reservation.id),
                field=field,
            )
    if (
        current.lease.reservation_id != candidate.lease.reservation_id
        or current.lease.agent_id != candidate.lease.agent_id
        or current.lease.bench_id != candidate.lease.bench_id
        or current.lease.owner != candidate.lease.owner
    ):
        raise ReservationLeaseInvalidError(
            "Replacement changed immutable lease routing identity.",
            reservation_id=str(candidate.reservation.id),
        )
    if candidate.lease.lease_version < current.lease.lease_version:
        raise ReservationLeaseInvalidError(
            "Replacement cannot lower the bench lease version.",
            reservation_id=str(candidate.reservation.id),
        )
    if candidate.lease.lease_version == current.lease.lease_version:
        current_content = current.lease.model_copy(update={"released_at": None})
        candidate_content = candidate.lease.model_copy(update={"released_at": None})
        if current_content != candidate_content:
            raise ReservationLeaseInvalidError(
                "An existing lease version cannot be rebound to different content.",
                reservation_id=str(candidate.reservation.id),
                lease_version=candidate.lease.lease_version,
            )
    elif (
        current.state is not ReservationLeaseState.ACTIVE
        or candidate.state is not ReservationLeaseState.RENEWING
        or candidate.lease.released_at is not None
    ):
        raise ReservationLeaseInvalidError(
            "A new lease version is only valid for an active reservation renewal.",
            reservation_id=str(candidate.reservation.id),
        )


def _replace_lease_history(
    connection: sqlite3.Connection,
    current: CoordinatedReservationLease,
    candidate: CoordinatedReservationLease,
) -> None:
    row = connection.execute(
        "SELECT * FROM reservation_leases WHERE reservation_id = ? AND lease_version = ?",
        (str(current.reservation.id), current.lease.lease_version),
    ).fetchone()
    if row is None or _lease_from_row(row) != current.lease:
        raise ReservationLeaseInvalidError(
            "Current coordinated lease does not match durable lease history.",
            reservation_id=str(current.reservation.id),
            lease_version=current.lease.lease_version,
        )
    if candidate.lease.lease_version > current.lease.lease_version:
        maximum_row = connection.execute(
            "SELECT MAX(lease_version) AS maximum_version FROM reservation_leases "
            "WHERE bench_id = ?",
            (candidate.lease.bench_id,),
        ).fetchone()
        maximum_version = int(maximum_row["maximum_version"])
        if candidate.lease.lease_version != maximum_version + 1:
            raise ReservationLeaseInvalidError(
                "Renewal did not advance the bench-wide lease high-water mark exactly once.",
                reservation_id=str(candidate.reservation.id),
                current_high_water_mark=maximum_version,
                requested_lease_version=candidate.lease.lease_version,
            )
        cursor = connection.execute(
            "UPDATE reservation_leases SET released_at = ? WHERE reservation_id = ? "
            "AND lease_version = ? AND released_at IS NULL",
            (
                candidate.lease.valid_from.isoformat(),
                str(current.reservation.id),
                current.lease.lease_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ReservationLeaseInvalidError(
                "Current lease was no longer available for renewal.",
                reservation_id=str(current.reservation.id),
            )
        _insert_lease(connection, candidate.lease)
        return
    if candidate.lease.released_at is not None:
        cursor = connection.execute(
            "UPDATE reservation_leases SET released_at = ? WHERE reservation_id = ? "
            "AND lease_version = ? AND released_at IS NULL",
            (
                candidate.lease.released_at.isoformat(),
                str(candidate.reservation.id),
                candidate.lease.lease_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ReservationLeaseInvalidError(
                "Current lease was no longer available for release.",
                reservation_id=str(candidate.reservation.id),
            )


def _insert_reservation(
    connection: sqlite3.Connection,
    reservation: Reservation,
) -> None:
    connection.execute(
        "INSERT INTO reservations "
        "(id, bench_id, owner, owner_principal_id, owner_principal_type, "
        "organisation_id, created_at, released_at, status, requested_at, "
        "starts_at, ends_at, activated_at, expired_at, source, metadata, "
        "idempotency_key, release_pending) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _reservation_values(reservation),
    )


def _update_reservation(
    connection: sqlite3.Connection,
    reservation: Reservation,
) -> None:
    cursor = connection.execute(
        "UPDATE reservations SET bench_id = ?, owner = ?, owner_principal_id = ?, "
        "owner_principal_type = ?, organisation_id = ?, created_at = ?, released_at = ?, "
        "status = ?, "
        "requested_at = ?, starts_at = ?, ends_at = ?, activated_at = ?, expired_at = ?, "
        "source = ?, metadata = ?, idempotency_key = ?, release_pending = ? "
        "WHERE id = ?",
        (*_reservation_values(reservation)[1:], str(reservation.id)),
    )
    if cursor.rowcount != 1:
        raise ReservationLeaseInvalidError(
            "Coordinated reservation has no durable base reservation.",
            reservation_id=str(reservation.id),
        )


def _insert_lease(connection: sqlite3.Connection, lease: ReservationLease) -> None:
    connection.execute(
        "INSERT INTO reservation_leases "
        "(reservation_id, organisation_id, agent_id, bench_id, owner, valid_from, valid_until, "
        "lease_version, released_at) VALUES (?, (SELECT organisation_id FROM reservations "
        "WHERE id = ?), ?, ?, ?, ?, ?, ?, ?)",
        (
            str(lease.reservation_id),
            str(lease.reservation_id),
            str(lease.agent_id),
            lease.bench_id,
            lease.owner,
            lease.valid_from.isoformat(),
            lease.valid_until.isoformat(),
            lease.lease_version,
            _datetime_value(lease.released_at),
        ),
    )


def _insert_coordination(
    connection: sqlite3.Connection,
    record: CoordinatedReservationLease,
) -> None:
    connection.execute(
        "INSERT INTO coordinated_reservation_leases "
        "(reservation_id, organisation_id, agent_id, bench_id, state, revision, lease_version, "
        "record_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(record.reservation.id),
            str(record.reservation.organisation_id),
            str(record.lease.agent_id),
            record.lease.bench_id,
            record.state.value,
            record.revision,
            record.lease.lease_version,
            _record_json(record),
            _record_timestamp(record).isoformat(),
        ),
    )


def _insert_mutation(
    connection: sqlite3.Connection,
    mutation_key: str,
    *,
    request_fingerprint: str,
    record: CoordinatedReservationLease,
) -> None:
    connection.execute(
        "INSERT INTO reservation_lease_mutations "
        "(mutation_key, organisation_id, request_fingerprint, reservation_id, revision, "
        "result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mutation_key,
            str(record.reservation.organisation_id),
            request_fingerprint,
            str(record.reservation.id),
            record.revision,
            _result_json(record),
            _record_timestamp(record).isoformat(),
        ),
    )


def _mutation_result(
    connection: sqlite3.Connection,
    mutation_key: str,
    *,
    organisation_id: UUID,
    request_fingerprint: str,
) -> LeaseWriteResult | None:
    row = connection.execute(
        "SELECT * FROM reservation_lease_mutations WHERE organisation_id = ? AND mutation_key = ?",
        (str(organisation_id), mutation_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != request_fingerprint:
        raise ReservationLeaseInvalidError(
            "Idempotency key was reused with different lease content.",
            idempotency_key=mutation_key,
        )
    payload = json.loads(row["result_json"])
    record = _record_from_payload(payload["record"])
    if str(record.reservation.id) != row["reservation_id"] or record.revision != int(
        row["revision"]
    ):
        raise ReservationLeaseInvalidError(
            "Durable reservation mutation result is inconsistent.",
            idempotency_key=mutation_key,
        )
    return LeaseWriteResult(record, LeaseWriteDisposition.REPLAY)


def _reservation_values(reservation: Reservation) -> tuple[object, ...]:
    return (
        str(reservation.id),
        reservation.bench_id,
        reservation.owner,
        str(reservation.owner_principal_id) if reservation.owner_principal_id else None,
        reservation.owner_principal_type,
        str(reservation.organisation_id),
        reservation.created_at.isoformat(),
        _datetime_value(reservation.released_at),
        reservation.status.value,
        _datetime_value(reservation.requested_at),
        _datetime_value(reservation.starts_at),
        _datetime_value(reservation.ends_at),
        _datetime_value(reservation.activated_at),
        _datetime_value(reservation.expired_at),
        reservation.source.value,
        json.dumps(reservation.metadata, sort_keys=True, separators=(",", ":")),
        reservation.idempotency_key,
        int(reservation.release_pending),
    )


def _record_payload(record: CoordinatedReservationLease) -> dict[str, Any]:
    return {
        "reservation": record.reservation.model_dump(mode="json"),
        "lease": record.lease.model_dump(mode="json"),
        "state": record.state.value,
        "revision": record.revision,
        "unknown_since": _datetime_value(record.unknown_since),
        "reconciliation_deadline": _datetime_value(record.reconciliation_deadline),
    }


def _record_json(record: CoordinatedReservationLease) -> str:
    return json.dumps(_record_payload(record), sort_keys=True, separators=(",", ":"))


def _result_json(record: CoordinatedReservationLease) -> str:
    return json.dumps(
        {"record": _record_payload(record)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_from_json(value: str) -> CoordinatedReservationLease:
    return _record_from_payload(json.loads(value))


def _record_from_payload(payload: dict[str, Any]) -> CoordinatedReservationLease:
    return CoordinatedReservationLease(
        reservation=Reservation.model_validate(payload["reservation"]),
        lease=ReservationLease.model_validate(payload["lease"]),
        state=ReservationLeaseState(payload["state"]),
        revision=int(payload["revision"]),
        unknown_since=_parse_datetime(payload.get("unknown_since")),
        reconciliation_deadline=_parse_datetime(payload.get("reconciliation_deadline")),
    )


def _lease_from_row(row: sqlite3.Row) -> ReservationLease:
    return ReservationLease(
        reservation_id=UUID(row["reservation_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        valid_from=datetime.fromisoformat(row["valid_from"]),
        valid_until=datetime.fromisoformat(row["valid_until"]),
        lease_version=int(row["lease_version"]),
        released_at=_parse_datetime(row["released_at"]),
    )


def _record_timestamp(record: CoordinatedReservationLease) -> datetime:
    candidates = [record.reservation.created_at, record.lease.valid_from]
    candidates.extend(
        value
        for value in (
            record.reservation.activated_at,
            record.reservation.released_at,
            record.reservation.expired_at,
            record.lease.released_at,
            record.unknown_since,
        )
        if value is not None
    )
    return max(candidates).astimezone(UTC)


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
