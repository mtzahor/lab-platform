from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import monotonic
from uuid import UUID

from lab_platform.core.decision_engine.interface import (
    DecisionEngine,
    DecisionRepository,
    DiagnosisUnavailable,
)
from lab_platform.core.decision_engine.policy import apply_policy
from lab_platform.core.decision_engine.schema import SCHEMA_VERSION, questions_hash
from lab_platform.core.decision_engine.settings import DecisionSettings
from lab_platform.models.decisions import DecisionRecord, DiagnosticState

_LOGGER = logging.getLogger("lab-platform.decision-engine")


class DecisionService:
    def __init__(
        self, engine: DecisionEngine, repository: DecisionRepository, settings: DecisionSettings
    ) -> None:
        self.engine = engine
        self.repository = repository
        self.settings = settings
        self._busy = False

    async def diagnose(
        self, run_id: UUID, organisation_id: UUID, serialize: Callable[[], DiagnosticState]
    ) -> DecisionRecord:
        start = monotonic()
        record = DecisionRecord(
            run_id=run_id,
            organisation_id=organisation_id,
            schema_version=SCHEMA_VERSION,
            questions_hash=questions_hash(),
            mode=self.settings.mode,
            requested_model=self.settings.model,
            policy_config={
                "diagnosis_min_confidence": self.settings.diagnosis_min_confidence,
                "action_min_confidence": self.settings.action_min_confidence,
                "retry_safe_min_probability": self.settings.retry_safe_min_probability,
            },
        )
        acquired = False
        try:
            if not self.settings.enabled:
                raise DiagnosisUnavailable(self.settings.configuration_error or "disabled")
            if self._busy:
                raise DiagnosisUnavailable("busy")
            self._busy = acquired = True
            state = serialize()
            record = record.model_copy(update={"state": state})
            async with asyncio.timeout(self.settings.timeout_ms / 1000):
                decision = await self.engine.diagnose_run(state)
            policy, reason = apply_policy(
                decision, self.settings, incomplete_state=state.incomplete
            )
            record = record.model_copy(
                update={
                    "decision": decision,
                    "policy": policy,
                    "policy_reason": reason,
                    "status": "available",
                }
            )
        except asyncio.CancelledError:
            record = record.model_copy(update={"error_code": "cancelled"})
            await self._save(record, start)
            raise
        except TimeoutError:
            record = record.model_copy(update={"error_code": "timeout"})
        except DiagnosisUnavailable as exc:
            record = record.model_copy(update={"error_code": exc.code})
        except Exception:
            # Provider/serializer exception text can contain state or credentials.
            record = record.model_copy(update={"error_code": "diagnosis_error"})
        finally:
            if acquired:
                self._busy = False
        return await self._save(record, start)

    async def _save(self, record: DecisionRecord, start: float) -> DecisionRecord:
        record = record.model_copy(update={"latency_ms": round((monotonic() - start) * 1000, 3)})
        try:
            await self.repository.save(record)
        except Exception:
            # Never expose an unaudited recommendation as usable.
            record = record.model_copy(
                update={
                    "decision": None,
                    "policy": None,
                    "status": "unavailable",
                    "error_code": "audit_unavailable",
                }
            )
        _LOGGER.info(
            "Decision engine attempt",
            extra={
                "event_type": "decision_engine.attempt",
                "workflow_run_id": str(record.run_id),
                "organisation_id": str(record.organisation_id),
                "event_payload": {
                    "decision_id": str(record.id),
                    "status": record.status,
                    "error_code": record.error_code,
                },
            },
        )
        return record
