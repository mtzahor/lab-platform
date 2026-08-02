from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from lab_platform.models import (
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    DistributedCiWorkflowBinding,
    Reservation,
    ReservationSource,
    ReservationStatus,
)
from lab_platform.persistence.database import SQLiteDatabase

_ASSIGNABLE_SESSION_STATUSES = (
    CiSessionStatus.CREATED,
    CiSessionStatus.WAITING_FOR_BENCH,
)
_STALE_SESSION_STATUSES = (
    CiSessionStatus.CREATED,
    CiSessionStatus.WAITING_FOR_BENCH,
    CiSessionStatus.RESERVED,
    CiSessionStatus.RUNNING,
    CiSessionStatus.CANCEL_REQUESTED,
)


class SQLiteCiSessionRepository:
    """Durable CI coordination and atomic bench assignment."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def create(
        self,
        session: CiSession,
        *,
        idempotency_key: str | None = None,
        errors: Sequence[str] = (),
    ) -> CiSession:
        with self._database.transaction(immediate=True) as connection:
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM ci_sessions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return _session_from_row(existing)
            try:
                connection.execute(
                    "INSERT INTO ci_sessions "
                    "(id, provider, external_run_id, repository, ref, commit_sha, actor, "
                    "requested_by, bench_id, reservation_id, workflow_run_id, status, "
                    "created_at, started_at, completed_at, heartbeat_at, timeout_at, "
                    "cleanup_status, bench_request_json, idempotency_key, outcome, errors_json, "
                    "workflow_launch_idempotency_key, finalize_idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?)",
                    (
                        *_session_values(session),
                        idempotency_key,
                        session.outcome.value,
                        _dump_errors(errors),
                        None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError:
                if idempotency_key is None:
                    raise
                existing = connection.execute(
                    "SELECT * FROM ci_sessions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise
                return _session_from_row(existing)
        return session

    async def get(self, session_id: UUID) -> CiSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ci_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def get_by_idempotency_key(self, key: str) -> CiSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ci_sessions WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def get_by_workflow_launch_idempotency_key(self, key: str) -> CiSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ci_sessions WHERE workflow_launch_idempotency_key = ?", (key,)
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def get_by_finalize_idempotency_key(self, key: str) -> CiSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ci_sessions WHERE finalize_idempotency_key = ?", (key,)
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    async def update(
        self,
        session: CiSession,
        *,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        return await self.compare_and_set(session, expected_statuses=None, errors=errors)

    async def compare_and_set(
        self,
        session: CiSession,
        *,
        expected_statuses: Iterable[CiSessionStatus] | None,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        expected = tuple(expected_statuses or ())
        condition = ""
        values: list[object] = [
            *_session_update_values(session),
            session.outcome.value,
        ]
        if errors is not None:
            error_expression = "?"
            values.append(_dump_errors(errors))
        else:
            error_expression = "errors_json"
        values.append(str(session.id))
        if expected:
            placeholders = ", ".join("?" for _ in expected)
            condition = f" AND status IN ({placeholders})"
            values.extend(item.value for item in expected)
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE ci_sessions SET provider = ?, external_run_id = ?, repository = ?, "
                "ref = ?, commit_sha = ?, actor = ?, requested_by = ?, bench_id = ?, "
                "reservation_id = ?, workflow_run_id = ?, status = ?, created_at = ?, "
                "started_at = ?, completed_at = ?, heartbeat_at = ?, timeout_at = ?, "
                "cleanup_status = ?, bench_request_json = ?, outcome = ?, "
                f"errors_json = {error_expression} WHERE id = ?{condition}",
                values,
            )
        return session if cursor.rowcount == 1 else None

    async def list(
        self,
        *,
        status: CiSessionStatus | None = None,
        provider: CiProvider | None = None,
        limit: int = 500,
    ) -> builtins.list[CiSession]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        conditions: list[str] = []
        values: list[object] = []
        if status is not None:
            conditions.append("status = ?")
            values.append(status.value)
        if provider is not None:
            conditions.append("provider = ?")
            values.append(provider.value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self._database.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM ci_sessions{where} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT ?",
                values,
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    async def list_stale(
        self,
        *,
        heartbeat_before: datetime,
        now: datetime,
        limit: int = 500,
    ) -> builtins.list[CiSession]:
        """Return live sessions whose heartbeat or absolute timeout has elapsed."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        placeholders = ", ".join("?" for _ in _STALE_SESSION_STATUSES)
        values: builtins.list[object] = [item.value for item in _STALE_SESSION_STATUSES]
        values.extend(
            (
                now.isoformat(),
                heartbeat_before.isoformat(),
                heartbeat_before.isoformat(),
                limit,
            )
        )
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ci_sessions "
                f"WHERE status IN ({placeholders}) AND ("  # noqa: S608
                "(timeout_at IS NOT NULL AND timeout_at <= ?) OR "
                "(heartbeat_at IS NOT NULL AND heartbeat_at <= ?) OR "
                "(heartbeat_at IS NULL AND created_at <= ?)) "
                "ORDER BY COALESCE(heartbeat_at, created_at), id LIMIT ?",
                values,
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    async def errors(self, session_id: UUID) -> builtins.list[str]:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT errors_json FROM ci_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return list(json.loads(row["errors_json"])) if row is not None else []

    async def append_error(self, session_id: UUID, error: str) -> bool:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT errors_json FROM ci_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
            if row is None:
                return False
            errors: builtins.list[str] = list(json.loads(row["errors_json"]))
            errors.append(error)
            cursor = connection.execute(
                "UPDATE ci_sessions SET errors_json = ? WHERE id = ?",
                (_dump_errors(errors), str(session_id)),
            )
        return cursor.rowcount == 1

    async def attach_workflow_run(
        self,
        session_id: UUID,
        workflow_run_id: UUID,
        *,
        idempotency_key: str,
        started_at: datetime,
    ) -> CiSession | None:
        """Attach one workflow run and remember the launch idempotency key."""

        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM ci_sessions WHERE workflow_launch_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return _session_from_row(row)
            cursor = connection.execute(
                "UPDATE ci_sessions SET workflow_run_id = ?, "
                "workflow_launch_idempotency_key = ?, status = 'running', "
                "started_at = COALESCE(started_at, ?) WHERE id = ? "
                "AND workflow_run_id IS NULL AND status = 'reserved'",
                (str(workflow_run_id), idempotency_key, started_at.isoformat(), str(session_id)),
            )
            if cursor.rowcount != 1:
                return None
            updated = connection.execute(
                "SELECT * FROM ci_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return _session_from_row(updated)

    async def attach_distributed_workflow(
        self,
        session_id: UUID,
        remote_command_id: UUID,
        *,
        operation_id: UUID,
        agent_id: UUID,
        bench_id: str,
        reservation_id: UUID,
        workflow_name: str,
        workflow_version: int,
        idempotency_key: str,
        request_fingerprint: str,
        started_at: datetime,
    ) -> CiSession | None:
        """Atomically bind a CI session to its centrally dispatched workflow.

        Selection, lease activation, and command dispatch happen in the
        distributed workflow coordinator.  This transaction makes their CI
        projection visible as one state change, avoiding an intermediate
        ``RESERVED`` session that cannot be recovered after a process crash.
        """

        with self._database.transaction(immediate=True) as connection:
            claimed = connection.execute(
                "SELECT ci.* FROM distributed_ci_workflows binding "
                "JOIN ci_sessions ci ON ci.id = binding.ci_session_id "
                "WHERE binding.launch_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if claimed is not None:
                return _session_from_row(claimed)
            current = connection.execute(
                "SELECT * FROM ci_sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
            if (
                current is None
                or current["bench_id"] is not None
                or current["reservation_id"] is not None
                or current["workflow_run_id"] is not None
                or current["workflow_launch_idempotency_key"] is not None
                or CiSessionStatus(current["status"]) not in _ASSIGNABLE_SESSION_STATUSES
            ):
                return None
            connection.execute(
                "INSERT INTO distributed_ci_workflows "
                "(ci_session_id, remote_command_id, operation_id, agent_id, bench_id, "
                "reservation_id, workflow_name, workflow_version, launch_idempotency_key, "
                "request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(session_id),
                    str(remote_command_id),
                    str(operation_id),
                    str(agent_id),
                    bench_id,
                    str(reservation_id),
                    workflow_name,
                    workflow_version,
                    idempotency_key,
                    request_fingerprint,
                    started_at.isoformat(),
                ),
            )
            cursor = connection.execute(
                "UPDATE ci_sessions SET bench_id = ?, reservation_id = ?, "
                "workflow_launch_idempotency_key = ?, "
                "status = 'running', started_at = COALESCE(started_at, ?) "
                "WHERE id = ? AND bench_id IS NULL AND reservation_id IS NULL "
                "AND workflow_run_id IS NULL AND workflow_launch_idempotency_key IS NULL "
                "AND status IN ('created', 'waiting_for_bench')",
                (
                    bench_id,
                    str(reservation_id),
                    idempotency_key,
                    started_at.isoformat(),
                    str(session_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Distributed CI workflow attachment lost its transaction")
            updated = connection.execute(
                "SELECT * FROM ci_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return _session_from_row(updated)

    async def get_distributed_workflow(
        self,
        session_id: UUID,
    ) -> DistributedCiWorkflowBinding | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM distributed_ci_workflows WHERE ci_session_id = ?",
                (str(session_id),),
            ).fetchone()
        return _distributed_binding_from_row(row) if row is not None else None

    async def mark_finalized(
        self,
        session: CiSession,
        *,
        idempotency_key: str,
        errors: Sequence[str] | None = None,
    ) -> CiSession | None:
        """Persist final state exactly once for a retryable finalization request."""

        with self._database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM ci_sessions WHERE finalize_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return _session_from_row(existing)
            error_expression = "errors_json" if errors is None else "?"
            values: list[object] = [
                *_session_update_values(session),
                session.outcome.value,
            ]
            if errors is not None:
                values.append(_dump_errors(errors))
            values.extend((idempotency_key, str(session.id)))
            cursor = connection.execute(
                "UPDATE ci_sessions SET provider = ?, external_run_id = ?, repository = ?, "
                "ref = ?, commit_sha = ?, actor = ?, requested_by = ?, bench_id = ?, "
                "reservation_id = ?, workflow_run_id = ?, status = ?, created_at = ?, "
                "started_at = ?, completed_at = ?, heartbeat_at = ?, timeout_at = ?, "
                "cleanup_status = ?, bench_request_json = ?, outcome = ?, "
                f"errors_json = {error_expression}, finalize_idempotency_key = ? WHERE id = ? "
                "AND finalize_idempotency_key IS NULL",
                values,
            )
            if cursor.rowcount != 1:
                return None
            updated = connection.execute(
                "SELECT * FROM ci_sessions WHERE id = ?", (str(session.id),)
            ).fetchone()
        return _session_from_row(updated)

    async def save_cleanup(
        self,
        session_id: UUID,
        result: CleanupResult,
        *,
        recorded_at: datetime | None = None,
    ) -> CleanupResult:
        timestamp = (
            recorded_at.isoformat() if recorded_at is not None else datetime.now(UTC).isoformat()
        )
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO ci_cleanup_results "
                "(ci_session_id, reservation_released, workflow_stopped, locks_released, "
                "serial_closed, artifacts_finalized, errors_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ci_session_id) DO UPDATE SET "
                "reservation_released = excluded.reservation_released, "
                "workflow_stopped = excluded.workflow_stopped, "
                "locks_released = excluded.locks_released, "
                "serial_closed = excluded.serial_closed, "
                "artifacts_finalized = excluded.artifacts_finalized, "
                "errors_json = excluded.errors_json, recorded_at = excluded.recorded_at",
                (
                    str(session_id),
                    int(result.reservation_released),
                    int(result.workflow_stopped),
                    int(result.locks_released),
                    int(result.serial_closed),
                    int(result.artifacts_finalized),
                    _dump_errors(result.errors),
                    timestamp,
                ),
            )
        return result

    async def get_cleanup(self, session_id: UUID) -> CleanupResult | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ci_cleanup_results WHERE ci_session_id = ?", (str(session_id),)
            ).fetchone()
        return _cleanup_from_row(row) if row is not None else None

    async def assign_compatible_bench(
        self,
        session_id: UUID,
        request: BenchRequest,
        *,
        now: datetime,
        reservation_id: UUID | None = None,
        owner: str | None = None,
        reservation_idempotency_key: str | None = None,
    ) -> tuple[CiSession, Reservation] | None:
        """Select and reserve a compatible bench in one ``BEGIN IMMEDIATE``.

        Candidate availability is rechecked while the SQLite write lock is held.
        This makes the selected bench and reservation a single observable decision
        even when several CI workers race across independent database connections.
        """

        created_reservation_id = reservation_id or uuid4()
        try:
            with self._database.transaction(immediate=True) as connection:
                session_row = connection.execute(
                    "SELECT * FROM ci_sessions WHERE id = ?", (str(session_id),)
                ).fetchone()
                if session_row is None:
                    return None
                if session_row["reservation_id"] is not None:
                    reservation_row = connection.execute(
                        "SELECT * FROM reservations WHERE id = ?",
                        (session_row["reservation_id"],),
                    ).fetchone()
                    if reservation_row is None:
                        return None
                    return _session_from_row(session_row), _reservation_from_row(reservation_row)
                if CiSessionStatus(session_row["status"]) not in _ASSIGNABLE_SESSION_STATUSES:
                    return None

                candidates = _compatible_candidates(connection, request, now)
                selected = next(
                    (candidate for candidate in candidates if candidate.available), None
                )
                if selected is None:
                    return None

                requested_by = owner or session_row["requested_by"]
                ends_at = now + timedelta(seconds=request.reservation_duration_seconds)
                key = reservation_idempotency_key or f"ci-session:{session_id}"
                metadata = json.dumps({"ci_session_id": str(session_id)}, sort_keys=True)
                connection.execute(
                    "INSERT INTO reservations "
                    "(id, bench_id, owner, created_at, released_at, status, requested_at, "
                    "starts_at, ends_at, activated_at, expired_at, source, metadata, "
                    "idempotency_key, release_pending) "
                    "VALUES (?, ?, ?, ?, NULL, 'active', ?, ?, ?, ?, NULL, 'ci', ?, ?, 0)",
                    (
                        str(created_reservation_id),
                        selected.bench_id,
                        requested_by,
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        ends_at.isoformat(),
                        now.isoformat(),
                        metadata,
                        key,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE ci_sessions SET bench_id = ?, reservation_id = ?, status = 'reserved', "
                    "bench_request_json = COALESCE(bench_request_json, ?) WHERE id = ? "
                    "AND reservation_id IS NULL AND status IN ('created', 'waiting_for_bench')",
                    (
                        selected.bench_id,
                        str(created_reservation_id),
                        request.model_dump_json(),
                        str(session_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("ci_session_assignment_race")
                assigned_row = connection.execute(
                    "SELECT * FROM ci_sessions WHERE id = ?", (str(session_id),)
                ).fetchone()
                reservation_row = connection.execute(
                    "SELECT * FROM reservations WHERE id = ?", (str(created_reservation_id),)
                ).fetchone()
        except sqlite3.IntegrityError:
            return None
        return _session_from_row(assigned_row), _reservation_from_row(reservation_row)


class _Candidate:
    __slots__ = ("available", "bench_id", "last_used_at", "preferred_score")

    def __init__(
        self,
        bench_id: str,
        *,
        available: bool,
        preferred_score: int,
        last_used_at: str | None,
    ) -> None:
        self.bench_id = bench_id
        self.available = available
        self.preferred_score = preferred_score
        self.last_used_at = last_used_at


def _compatible_candidates(
    connection: sqlite3.Connection,
    request: BenchRequest,
    now: datetime,
) -> list[_Candidate]:
    rows = connection.execute(
        "SELECT catalog.id, catalog.capabilities_json, catalog.labels_json, backend.type "
        "AS backend_type FROM bench_catalog catalog "
        "JOIN backend_registrations backend ON backend.id = catalog.backend_id "
        "WHERE catalog.online = 1 ORDER BY catalog.id"
    ).fetchall()
    required_capabilities = {item.casefold() for item in request.required_capabilities}
    candidates: list[_Candidate] = []
    for row in rows:
        bench_id = str(row["id"])
        if request.explicit_bench_id is not None and bench_id != request.explicit_bench_id:
            continue
        backend_type = str(row["backend_type"]).casefold()
        if backend_type == "simlab" and not request.allow_simulated:
            continue
        if backend_type == "real" and not request.allow_physical:
            continue
        if backend_type not in {"simlab", "real"}:
            continue
        capabilities = {str(item).casefold() for item in json.loads(row["capabilities_json"])}
        if not required_capabilities.issubset(capabilities):
            continue
        labels = {str(key): str(value) for key, value in json.loads(row["labels_json"]).items()}
        if any(labels.get(key) != value for key, value in request.required_labels.items()):
            continue
        available = _bench_is_available(connection, bench_id, now, request)
        preferred_score = sum(
            1 for key, value in request.preferred_labels.items() if labels.get(key) == value
        )
        last_used_row = connection.execute(
            "SELECT MAX(activated_at) AS last_used_at FROM reservations "
            "WHERE bench_id = ? AND activated_at IS NOT NULL",
            (bench_id,),
        ).fetchone()
        candidates.append(
            _Candidate(
                bench_id,
                available=available,
                preferred_score=preferred_score,
                last_used_at=last_used_row["last_used_at"],
            )
        )
    candidates.sort(
        key=lambda candidate: (
            not candidate.available,
            -candidate.preferred_score,
            candidate.last_used_at is not None,
            candidate.last_used_at or "",
            candidate.bench_id,
        )
    )
    return candidates


def _bench_is_available(
    connection: sqlite3.Connection,
    bench_id: str,
    now: datetime,
    request: BenchRequest,
) -> bool:
    if (
        connection.execute(
            "SELECT 1 FROM operation_locks WHERE bench_id = ? LIMIT 1", (bench_id,)
        ).fetchone()
        is not None
    ):
        return False
    requested_end = now + timedelta(seconds=request.reservation_duration_seconds)
    conflict = connection.execute(
        "SELECT 1 FROM reservations WHERE bench_id = ? AND ("
        "status IN ('active', 'expired_pending_operation') OR ("
        "status = 'scheduled' AND (starts_at IS NULL OR ends_at IS NULL OR "
        "(starts_at < ? AND ends_at > ?)))) LIMIT 1",
        (bench_id, requested_end.isoformat(), now.isoformat()),
    ).fetchone()
    return conflict is None


def _session_values(session: CiSession) -> tuple[object, ...]:
    return (
        str(session.id),
        session.provider.value,
        session.external_run_id,
        session.repository,
        session.ref,
        session.commit_sha,
        session.actor,
        session.requested_by,
        session.bench_id,
        str(session.reservation_id) if session.reservation_id is not None else None,
        str(session.workflow_run_id) if session.workflow_run_id is not None else None,
        session.status.value,
        session.created_at.isoformat(),
        _datetime_value(session.started_at),
        _datetime_value(session.completed_at),
        _datetime_value(session.heartbeat_at),
        _datetime_value(session.timeout_at),
        session.cleanup_status.value,
        session.bench_request.model_dump_json() if session.bench_request is not None else None,
    )


def _session_update_values(session: CiSession) -> tuple[object, ...]:
    return _session_values(session)[1:]


def _session_from_row(row: sqlite3.Row) -> CiSession:
    bench_request = row["bench_request_json"]
    return CiSession(
        id=UUID(row["id"]),
        provider=CiProvider(row["provider"]),
        external_run_id=row["external_run_id"],
        repository=row["repository"],
        ref=row["ref"],
        commit_sha=row["commit_sha"],
        actor=row["actor"],
        requested_by=row["requested_by"],
        bench_id=row["bench_id"],
        reservation_id=UUID(row["reservation_id"]) if row["reservation_id"] else None,
        workflow_run_id=UUID(row["workflow_run_id"]) if row["workflow_run_id"] else None,
        status=CiSessionStatus(row["status"]),
        outcome=CiOutcome(row["outcome"] or CiOutcome.PENDING.value),
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        heartbeat_at=_parse_datetime(row["heartbeat_at"]),
        timeout_at=_parse_datetime(row["timeout_at"]),
        cleanup_status=CleanupStatus(row["cleanup_status"]),
        bench_request=(BenchRequest.model_validate_json(bench_request) if bench_request else None),
    )


def _distributed_binding_from_row(row: sqlite3.Row) -> DistributedCiWorkflowBinding:
    return DistributedCiWorkflowBinding(
        ci_session_id=UUID(row["ci_session_id"]),
        remote_command_id=UUID(row["remote_command_id"]),
        operation_id=UUID(row["operation_id"]),
        agent_id=UUID(row["agent_id"]),
        bench_id=row["bench_id"],
        reservation_id=UUID(row["reservation_id"]),
        workflow_name=row["workflow_name"],
        workflow_version=row["workflow_version"],
        launch_idempotency_key=row["launch_idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _reservation_from_row(row: sqlite3.Row) -> Reservation:
    return Reservation(
        id=UUID(row["id"]),
        bench_id=row["bench_id"],
        owner=row["owner"],
        created_at=datetime.fromisoformat(row["created_at"]),
        released_at=_parse_datetime(row["released_at"]),
        status=ReservationStatus(row["status"]),
        requested_at=_parse_datetime(row["requested_at"]),
        starts_at=_parse_datetime(row["starts_at"]),
        ends_at=_parse_datetime(row["ends_at"]),
        activated_at=_parse_datetime(row["activated_at"]),
        expired_at=_parse_datetime(row["expired_at"]),
        source=ReservationSource(row["source"]),
        metadata=json.loads(row["metadata"]),
        idempotency_key=row["idempotency_key"],
        release_pending=bool(row["release_pending"]),
    )


def _cleanup_from_row(row: sqlite3.Row) -> CleanupResult:
    return CleanupResult(
        reservation_released=bool(row["reservation_released"]),
        workflow_stopped=bool(row["workflow_stopped"]),
        locks_released=bool(row["locks_released"]),
        serial_closed=bool(row["serial_closed"]),
        artifacts_finalized=bool(row["artifacts_finalized"]),
        errors=json.loads(row["errors_json"]),
    )


def _dump_errors(errors: Sequence[str]) -> str:
    return json.dumps(list(errors), separators=(",", ":"))


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
