from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from lab_platform.core.decision_engine.schema import SCHEMA_VERSION
from lab_platform.core.decision_engine.serializer import serialize_operation

if TYPE_CHECKING:
    from lab_platform.control_plane.runtime import ControlPlaneRuntime

_LOGGER = logging.getLogger("lab-platform.decision-engine")


async def shadow_once(runtime: ControlPlaneRuntime) -> None:
    if not runtime.decision_settings.enabled or runtime.decision_settings.mode != "shadow":
        return
    candidates = await runtime.decision_repository.shadow_candidates(SCHEMA_VERSION)
    for run_id, organisation_id in candidates:
        operation = await runtime.operation_records.get(run_id, organisation_id=organisation_id)
        if operation is None:
            continue
        if not await runtime.decision_repository.claim_shadow(
            run_id, organisation_id, SCHEMA_VERSION
        ):
            continue
        await runtime.decision_service.diagnose(
            run_id,
            organisation_id,
            partial(
                serialize_operation, operation, max_bytes=runtime.decision_settings.max_state_bytes
            ),
        )


async def shadow_loop(runtime: ControlPlaneRuntime) -> None:
    # Separate from Agent heartbeats, lease maintenance, test execution and cleanup.
    while True:
        try:
            await shadow_once(runtime)
        except Exception:
            _LOGGER.warning(
                "Decision shadow worker unavailable",
                extra={"event_type": "decision_engine.worker_unavailable"},
            )
        await asyncio.sleep(30)
