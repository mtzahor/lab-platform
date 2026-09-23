from __future__ import annotations

from uuid import UUID

from lab_platform.models.decisions import DecisionRecord, FeedbackRecord
from lab_platform.persistence.database import SQLiteDatabase

DECISION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS diagnostic_decisions (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('shadow', 'recommend')),
    record_json TEXT NOT NULL,
    UNIQUE (organisation_id, run_id, id)
);
CREATE INDEX IF NOT EXISTS diagnostic_decisions_run
    ON diagnostic_decisions(organisation_id, run_id, timestamp DESC);
CREATE TABLE IF NOT EXISTS diagnostic_feedback (
    id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    record_json TEXT NOT NULL,
    FOREIGN KEY (organisation_id, run_id, decision_id)
        REFERENCES diagnostic_decisions(organisation_id, run_id, id)
);
CREATE TABLE IF NOT EXISTS diagnostic_shadow_claims (
    organisation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    PRIMARY KEY (organisation_id, run_id, schema_version)
);
"""


class SQLiteDecisionRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    async def save(self, record: DecisionRecord) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO diagnostic_decisions "
                "(id, organisation_id, run_id, timestamp, schema_version, mode, record_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.id),
                    str(record.organisation_id),
                    str(record.run_id),
                    record.timestamp.isoformat(),
                    record.schema_version,
                    record.mode,
                    record.model_dump_json(),
                ),
            )

    async def list_for_run(
        self, run_id: UUID, organisation_id: UUID, *, limit: int = 20
    ) -> list[DecisionRecord]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT record_json FROM diagnostic_decisions WHERE organisation_id = ?"
                " AND run_id = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (str(organisation_id), str(run_id), min(max(limit, 1), 100)),
            ).fetchall()
        return [DecisionRecord.model_validate_json(row["record_json"]) for row in rows]

    async def get(
        self, decision_id: UUID, run_id: UUID, organisation_id: UUID
    ) -> DecisionRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM diagnostic_decisions WHERE id = ? AND "
                "organisation_id = ? AND run_id = ?",
                (str(decision_id), str(organisation_id), str(run_id)),
            ).fetchone()
        return DecisionRecord.model_validate_json(row["record_json"]) if row else None

    async def save_feedback(self, record: FeedbackRecord) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO diagnostic_feedback "
                "(id, organisation_id, run_id, decision_id, timestamp, record_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(record.id),
                    str(record.organisation_id),
                    str(record.run_id),
                    str(record.decision_id),
                    record.timestamp.isoformat(),
                    record.model_dump_json(),
                ),
            )

    async def list_feedback(
        self, decision_id: UUID, run_id: UUID, organisation_id: UUID
    ) -> list[FeedbackRecord]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT record_json FROM diagnostic_feedback WHERE decision_id = ? AND run_id = ? "
                "AND organisation_id = ? ORDER BY timestamp, id LIMIT 100",
                (str(decision_id), str(run_id), str(organisation_id)),
            ).fetchall()
        return [FeedbackRecord.model_validate_json(row["record_json"]) for row in rows]

    async def shadow_candidates(self, schema_version: str) -> list[tuple[UUID, UUID]]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT o.id, o.organisation_id FROM distributed_operations o "
                "JOIN remote_commands c ON c.id = o.remote_command_id "
                "WHERE o.status = 'FAILED' AND c.command_type = 'RUN_WORKFLOW' "
                "AND NOT EXISTS (SELECT 1 FROM diagnostic_shadow_claims d "
                "WHERE d.organisation_id = o.organisation_id AND d.run_id = o.id AND "
                "d.schema_version = ?) "
                "ORDER BY o.created_at, o.id LIMIT 5",
                (schema_version,),
            ).fetchall()
        return [(UUID(row["id"]), UUID(row["organisation_id"])) for row in rows]

    async def claim_shadow(self, run_id: UUID, organisation_id: UUID, schema_version: str) -> bool:
        with self._database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO diagnostic_shadow_claims (organisation_id, run_id, schema_version) "
                "VALUES (?, ?, ?) ON CONFLICT (organisation_id, run_id, schema_version) DO NOTHING",
                (str(organisation_id), str(run_id), schema_version),
            )
        return bool(cursor.rowcount == 1)
