from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import UUID

from lab_platform.core.workflows import (
    WorkflowBenchBusyError,
    WorkflowInvalidError,
    WorkflowRunNotFoundError,
)
from lab_platform.models.workflows import (
    WorkflowAction,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepResult,
    WorkflowStepStatus,
)
from lab_platform.persistence.database import SQLiteDatabase

WORKFLOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflows (
    name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    bench_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed',
                   'cancel_requested', 'cancelled')
    ),
    current_step INTEGER CHECK (current_step IS NULL OR current_step >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY (workflow_name, workflow_version)
        REFERENCES workflows(name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_runs_one_active_per_bench
    ON workflow_runs(bench_id)
    WHERE status IN ('pending', 'running', 'cancel_requested');
CREATE INDEX IF NOT EXISTS workflow_runs_created
    ON workflow_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS workflow_step_results (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL CHECK (step_index >= 0),
    name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL CHECK (
        action IN ('flash', 'reset', 'read_serial', 'assert_serial', 'wait', 'probe')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')
    ),
    started_at TEXT,
    completed_at TEXT,
    output_json TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(artifact_ids_json) AND json_type(artifact_ids_json) = 'array'
    ),
    UNIQUE (workflow_run_id, step_index),
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS workflow_step_results_run
    ON workflow_step_results(workflow_run_id, step_index);
"""


def initialize_workflow_schema(database: SQLiteDatabase) -> None:
    """Install the independently versionable workflow tables.

    The Phase 3 database migration can execute ``WORKFLOW_SCHEMA_SQL`` centrally;
    calling this helper is idempotent and keeps standalone repository use safe.
    """

    with database.transaction(immediate=True) as connection:
        ensure_workflow_schema(connection)


def ensure_workflow_schema(connection: sqlite3.Connection) -> None:
    """Create workflow tables and upgrade pre-Phase-4 step-result storage.

    SQLite cannot relax a ``NOT NULL`` constraint or extend a ``CHECK`` in place,
    so the step table is rebuilt when the old Phase 3 shape is detected. The copy
    preserves every existing result and supplies deterministic defaults for the
    newly persisted display name and artifact references.
    """

    connection.executescript(WORKFLOW_SCHEMA_SQL)
    if not _workflow_step_results_need_rebuild(connection):
        return
    _rebuild_workflow_step_results(connection)


def _workflow_step_results_need_rebuild(connection: sqlite3.Connection) -> bool:
    columns = {
        row[1]: row for row in connection.execute("PRAGMA table_info(workflow_step_results)")
    }
    if "name" not in columns or "artifact_ids_json" not in columns:
        return True
    if columns["started_at"][3]:
        return True
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'workflow_step_results'"
    ).fetchone()
    table_sql = str(row[0]).casefold() if row is not None else ""
    return "'pending'" not in table_sql or "'skipped'" not in table_sql


def _rebuild_workflow_step_results(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_step_results)")}
    name_expression = "COALESCE(name, '')" if "name" in columns else "''"
    artifact_expression = (
        "COALESCE(artifact_ids_json, '[]')" if "artifact_ids_json" in columns else "'[]'"
    )
    connection.execute("DROP TABLE IF EXISTS workflow_step_results_phase4")
    connection.execute(
        """
        CREATE TABLE workflow_step_results_phase4 (
            id TEXT PRIMARY KEY,
            workflow_run_id TEXT NOT NULL,
            step_index INTEGER NOT NULL CHECK (step_index >= 0),
            name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL CHECK (
                action IN ('flash', 'reset', 'read_serial', 'assert_serial', 'wait', 'probe')
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'running', 'succeeded', 'failed', 'skipped', 'cancelled'
                )
            ),
            started_at TEXT,
            completed_at TEXT,
            output_json TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(artifact_ids_json) AND json_type(artifact_ids_json) = 'array'
            ),
            UNIQUE (workflow_run_id, step_index),
            FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "INSERT INTO workflow_step_results_phase4 "
        "(id, workflow_run_id, step_index, name, action, status, started_at, completed_at, "
        "output_json, error_code, error_message, artifact_ids_json) "
        f"SELECT id, workflow_run_id, step_index, {name_expression}, action, status, "  # noqa: S608
        "started_at, completed_at, output_json, error_code, error_message, "
        f"{artifact_expression} FROM workflow_step_results"  # noqa: S608
    )
    connection.execute("DROP TABLE workflow_step_results")
    connection.execute("ALTER TABLE workflow_step_results_phase4 RENAME TO workflow_step_results")
    connection.execute(
        "CREATE INDEX workflow_step_results_run "
        "ON workflow_step_results(workflow_run_id, step_index)"
    )


class SQLiteWorkflowRepository:
    def __init__(self, database: SQLiteDatabase, *, initialize_schema: bool = True) -> None:
        self._database = database
        if initialize_schema:
            initialize_workflow_schema(database)

    async def save_definition(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        serialized = definition.model_dump_json()
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT definition_json FROM workflows WHERE name = ? AND version = ?",
                (definition.name, definition.version),
            ).fetchone()
            if row is not None:
                existing = WorkflowDefinition.model_validate_json(row["definition_json"])
                if existing != definition:
                    raise WorkflowInvalidError(
                        f"Workflow {definition.name!r} version {definition.version} already exists "
                        "with different content.",
                        workflow_name=definition.name,
                        workflow_version=definition.version,
                    )
                return existing
            connection.execute(
                "INSERT INTO workflows(name, version, definition_json) VALUES (?, ?, ?)",
                (definition.name, definition.version, serialized),
            )
        return definition

    async def get_definition(
        self, name: str, version: int | None = None
    ) -> WorkflowDefinition | None:
        with self._database.transaction() as connection:
            if version is None:
                row = connection.execute(
                    "SELECT definition_json FROM workflows WHERE name = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (name,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT definition_json FROM workflows WHERE name = ? AND version = ?",
                    (name, version),
                ).fetchone()
        if row is None:
            return None
        return WorkflowDefinition.model_validate_json(row["definition_json"])

    async def list_definitions(self) -> list[WorkflowDefinition]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT definition_json FROM workflows ORDER BY name, version DESC"
            ).fetchall()
        return [WorkflowDefinition.model_validate_json(row["definition_json"]) for row in rows]

    async def create_run(self, run: WorkflowRun) -> WorkflowRun:
        try:
            with self._database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_name, workflow_version, bench_id, owner, reservation_id, "
                    "status, current_step, created_at, started_at, completed_at, error_code, "
                    "error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _run_values(run),
                )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "workflow_runs.bench_id" in message:
                raise WorkflowBenchBusyError(
                    f"Bench {run.bench_id} already has an active workflow.",
                    bench_id=run.bench_id,
                ) from exc
            raise WorkflowInvalidError(
                f"Workflow run {run.id} could not be persisted: {exc}",
                workflow_run_id=str(run.id),
            ) from exc
        return run

    async def get_run(self, run_id: UUID) -> WorkflowRun | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    async def update_run(self, run: WorkflowRun) -> WorkflowRun:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_runs SET workflow_name = ?, workflow_version = ?, bench_id = ?, "
                "owner = ?, reservation_id = ?, status = ?, current_step = ?, created_at = ?, "
                "started_at = ?, completed_at = ?, error_code = ?, error_message = ? WHERE id = ?",
                (
                    run.workflow_name,
                    run.workflow_version,
                    run.bench_id,
                    run.owner,
                    str(run.reservation_id),
                    run.status.value,
                    run.current_step,
                    run.created_at.isoformat(),
                    _datetime_value(run.started_at),
                    _datetime_value(run.completed_at),
                    run.error_code,
                    run.error_message,
                    str(run.id),
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowRunNotFoundError(
                    f"Workflow run {run.id} does not exist.", workflow_run_id=str(run.id)
                )
        return run

    async def create_step_result(self, result: WorkflowStepResult) -> WorkflowStepResult:
        try:
            with self._database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO workflow_step_results "
                    "(id, workflow_run_id, step_index, name, action, status, started_at, "
                    "completed_at, output_json, error_code, error_message, artifact_ids_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _step_result_values(result),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowInvalidError(
                f"Workflow step {result.step_index} could not be persisted: {exc}",
                workflow_run_id=str(result.workflow_run_id),
                step_index=result.step_index,
            ) from exc
        return result

    async def update_step_result(self, result: WorkflowStepResult) -> WorkflowStepResult:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_step_results SET name = ?, action = ?, status = ?, "
                "started_at = ?, completed_at = ?, output_json = ?, error_code = ?, "
                "error_message = ?, artifact_ids_json = ? WHERE id = ?",
                (
                    result.name,
                    result.action.value,
                    result.status.value,
                    _datetime_value(result.started_at),
                    _datetime_value(result.completed_at),
                    json.dumps(result.output, sort_keys=True, default=str),
                    result.error_code,
                    result.error_message,
                    json.dumps([str(item) for item in result.artifact_ids]),
                    str(result.id),
                ),
            )
            if cursor.rowcount != 1:
                raise WorkflowRunNotFoundError(
                    f"Workflow step result {result.id} does not exist.",
                    workflow_step_result_id=str(result.id),
                )
        return result

    async def list_step_results(self, run_id: UUID) -> list[WorkflowStepResult]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_step_results WHERE workflow_run_id = ? ORDER BY step_index",
                (str(run_id),),
            ).fetchall()
        return [_step_result_from_row(row) for row in rows]

    async def recover_interrupted(self, now: datetime) -> list[WorkflowRun]:
        timestamp = now.isoformat()
        with self._database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM workflow_runs "
                "WHERE status IN ('pending', 'running', 'cancel_requested') "
                "ORDER BY created_at"
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            if not run_ids:
                return []
            placeholders = ", ".join("?" for _ in run_ids)
            connection.execute(
                f"UPDATE workflow_step_results SET status = 'failed', completed_at = ?, "
                f"error_code = 'AGENT_RESTARTED', "
                f"error_message = 'Agent restarted while the workflow was running' "
                f"WHERE status = 'running' AND workflow_run_id IN ({placeholders})",
                (timestamp, *run_ids),
            )
            connection.execute(
                f"UPDATE workflow_runs SET status = 'failed', completed_at = ?, "
                f"error_code = 'AGENT_RESTARTED', "
                f"error_message = 'Agent restarted while the workflow was running' "
                f"WHERE id IN ({placeholders})",
                (timestamp, *run_ids),
            )
            recovered_rows = connection.execute(
                f"SELECT * FROM workflow_runs WHERE id IN ({placeholders}) ORDER BY created_at",
                run_ids,
            ).fetchall()
        return [_run_from_row(row) for row in recovered_rows]


def _run_values(run: WorkflowRun) -> tuple[object, ...]:
    return (
        str(run.id),
        run.workflow_name,
        run.workflow_version,
        run.bench_id,
        run.owner,
        str(run.reservation_id),
        run.status.value,
        run.current_step,
        run.created_at.isoformat(),
        _datetime_value(run.started_at),
        _datetime_value(run.completed_at),
        run.error_code,
        run.error_message,
    )


def _step_result_values(result: WorkflowStepResult) -> tuple[object, ...]:
    return (
        str(result.id),
        str(result.workflow_run_id),
        result.step_index,
        result.name,
        result.action.value,
        result.status.value,
        _datetime_value(result.started_at),
        _datetime_value(result.completed_at),
        json.dumps(result.output, sort_keys=True, default=str),
        result.error_code,
        result.error_message,
        json.dumps([str(item) for item in result.artifact_ids]),
    )


def _run_from_row(row: sqlite3.Row) -> WorkflowRun:
    return WorkflowRun(
        id=UUID(row["id"]),
        workflow_name=row["workflow_name"],
        workflow_version=row["workflow_version"],
        bench_id=row["bench_id"],
        owner=row["owner"],
        reservation_id=UUID(row["reservation_id"]),
        status=WorkflowRunStatus(row["status"]),
        current_step=row["current_step"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _step_result_from_row(row: sqlite3.Row) -> WorkflowStepResult:
    return WorkflowStepResult(
        id=UUID(row["id"]),
        workflow_run_id=UUID(row["workflow_run_id"]),
        step_index=row["step_index"],
        name=row["name"],
        action=WorkflowAction(row["action"]),
        status=WorkflowStepStatus(row["status"]),
        started_at=_parse_datetime(row["started_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        output=json.loads(row["output_json"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        artifact_ids=[UUID(item) for item in json.loads(row["artifact_ids_json"])],
    )


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
