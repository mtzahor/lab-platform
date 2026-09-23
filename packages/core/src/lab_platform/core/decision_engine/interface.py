from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lab_platform.models.decisions import DecisionRecord, DiagnosticDecision, DiagnosticState


class DecisionEngine(Protocol):
    async def diagnose_run(self, run_state: DiagnosticState) -> DiagnosticDecision: ...


class DecisionRepository(Protocol):
    async def save(self, record: DecisionRecord) -> None: ...

    async def list_for_run(
        self,
        run_id: UUID,
        organisation_id: UUID,
        *,
        limit: int = 20,
    ) -> list[DecisionRecord]: ...


class DiagnosisUnavailable(Exception):
    """A stable error code, never a provider response or credential."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
