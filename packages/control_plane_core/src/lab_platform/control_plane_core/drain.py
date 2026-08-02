from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, Protocol
from uuid import UUID

from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
)
from lab_platform.models import AgentRecord, AgentStatus


@dataclass(frozen=True, slots=True)
class AgentWorkload:
    active_operations: int = 0
    active_reservations: int = 0
    queued_ci_sessions: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.active_operations,
                self.active_reservations,
                self.queued_ci_sessions,
            )
            < 0
        ):
            raise ValueError("Agent workload counts cannot be negative")

    @property
    def idle(self) -> bool:
        return (
            self.active_operations == 0
            and self.active_reservations == 0
            and self.queued_ci_sessions == 0
        )


@dataclass(frozen=True, slots=True)
class AgentDrainSnapshot:
    agent: AgentRecord
    workload: AgentWorkload
    has_active_connection: bool


@dataclass(frozen=True, slots=True)
class DrainResult:
    snapshot: AgentDrainSnapshot
    cancelled_queued_work: int = 0

    @property
    def agent(self) -> AgentRecord:
        return self.snapshot.agent

    @property
    def workload(self) -> AgentWorkload:
        return self.snapshot.workload


class AgentDrainRepository(Protocol):
    """Atomic status, connection, and workload policy boundary."""

    async def get_drain_snapshot(self, agent_id: UUID) -> AgentDrainSnapshot | None: ...

    async def compare_and_set_status(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
        target_status: AgentStatus,
        require_idle: bool = False,
        require_active_connection: bool = False,
    ) -> AgentDrainSnapshot | None: ...

    async def cancel_queued_work(
        self,
        agent_id: UUID,
        *,
        expected_status: AgentStatus,
    ) -> int | None: ...


class AgentDrainService:
    """Close the assignment gate, then atomically mark an idle Agent drained."""

    def __init__(self, repository: AgentDrainRepository) -> None:
        self._repository = repository

    async def drain(
        self,
        agent_id: UUID,
        *,
        cancel_queued_work: bool = False,
    ) -> DrainResult:
        snapshot = await self._require_snapshot(agent_id)
        status = snapshot.agent.status
        if status in {AgentStatus.ONLINE, AgentStatus.DEGRADED}:
            transitioned = await self._repository.compare_and_set_status(
                agent_id,
                expected_status=status,
                target_status=AgentStatus.DRAINING,
            )
            if transitioned is None:
                snapshot = await self._require_snapshot(agent_id)
                if snapshot.agent.status not in {
                    AgentStatus.DRAINING,
                    AgentStatus.DRAINED,
                }:
                    _raise_drain_conflict(snapshot.agent, expected=status)
            else:
                snapshot = transitioned
        elif status not in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
            _raise_unavailable(snapshot.agent)

        if snapshot.agent.status is AgentStatus.DRAINED:
            if not snapshot.workload.idle:
                raise AgentDrainingError(
                    "DRAINED Agent has acquired work unexpectedly.",
                    agent_id=str(agent_id),
                )
            return DrainResult(snapshot=snapshot)

        cancelled = 0
        if cancel_queued_work:
            cancellation = await self._repository.cancel_queued_work(
                agent_id,
                expected_status=AgentStatus.DRAINING,
            )
            if cancellation is None:
                current = await self._require_snapshot(agent_id)
                if current.agent.status is AgentStatus.DRAINED and current.workload.idle:
                    return DrainResult(snapshot=current)
                _raise_drain_conflict(current.agent, expected=AgentStatus.DRAINING)
            cancelled = cancellation

        return await self._finish_if_idle(agent_id, cancelled_queued_work=cancelled)

    async def refresh(self, agent_id: UUID) -> DrainResult:
        snapshot = await self._require_snapshot(agent_id)
        if snapshot.agent.status is AgentStatus.DRAINED:
            if not snapshot.workload.idle:
                raise AgentDrainingError(
                    "DRAINED Agent has acquired work unexpectedly.",
                    agent_id=str(agent_id),
                )
            return DrainResult(snapshot=snapshot)
        if snapshot.agent.status is not AgentStatus.DRAINING:
            raise AgentDrainingError(
                "Agent is not draining.",
                agent_id=str(agent_id),
                status=snapshot.agent.status.value,
            )
        return await self._finish_if_idle(agent_id)

    async def undrain(self, agent_id: UUID) -> AgentRecord:
        snapshot = await self._require_snapshot(agent_id)
        status = snapshot.agent.status
        if status is AgentStatus.ONLINE:
            if not snapshot.has_active_connection:
                raise AgentOfflineError(
                    "Agent cannot be online without an active connection.",
                    agent_id=str(agent_id),
                )
            return snapshot.agent
        if status not in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
            _raise_unavailable(snapshot.agent)
        if not snapshot.has_active_connection:
            raise AgentOfflineError(
                "Agent cannot be undrained without an active connection.",
                agent_id=str(agent_id),
            )
        transitioned = await self._repository.compare_and_set_status(
            agent_id,
            expected_status=status,
            target_status=AgentStatus.ONLINE,
            require_active_connection=True,
        )
        if transitioned is not None:
            return transitioned.agent
        current = await self._require_snapshot(agent_id)
        if current.agent.status is AgentStatus.ONLINE and current.has_active_connection:
            return current.agent
        if not current.has_active_connection:
            raise AgentOfflineError(
                "Agent connection closed during undrain.",
                agent_id=str(agent_id),
            )
        _raise_drain_conflict(current.agent, expected=status)

    async def require_accepting_new_work(self, agent_id: UUID) -> AgentRecord:
        agent = (await self._require_snapshot(agent_id)).agent
        if agent.status is AgentStatus.ONLINE:
            return agent
        if agent.status in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
            raise AgentDrainingError(
                "Agent is draining and cannot accept new work.",
                agent_id=str(agent_id),
            )
        _raise_unavailable(agent)

    async def _finish_if_idle(
        self,
        agent_id: UUID,
        *,
        cancelled_queued_work: int = 0,
    ) -> DrainResult:
        transitioned = await self._repository.compare_and_set_status(
            agent_id,
            expected_status=AgentStatus.DRAINING,
            target_status=AgentStatus.DRAINED,
            require_idle=True,
        )
        if transitioned is not None:
            return DrainResult(
                snapshot=transitioned,
                cancelled_queued_work=cancelled_queued_work,
            )
        current = await self._require_snapshot(agent_id)
        if current.agent.status is AgentStatus.DRAINING:
            return DrainResult(
                snapshot=current,
                cancelled_queued_work=cancelled_queued_work,
            )
        if current.agent.status is AgentStatus.DRAINED and current.workload.idle:
            return DrainResult(
                snapshot=current,
                cancelled_queued_work=cancelled_queued_work,
            )
        _raise_drain_conflict(current.agent, expected=AgentStatus.DRAINING)

    async def _require_snapshot(self, agent_id: UUID) -> AgentDrainSnapshot:
        snapshot = await self._repository.get_drain_snapshot(agent_id)
        if snapshot is None:
            raise AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))
        return snapshot


def _raise_unavailable(agent: AgentRecord) -> NoReturn:
    if agent.status is AgentStatus.REVOKED:
        raise AgentRevokedError("Agent is revoked.", agent_id=str(agent.id))
    if agent.status is AgentStatus.INCOMPATIBLE:
        raise AgentIncompatibleError("Agent is incompatible.", agent_id=str(agent.id))
    if agent.status is AgentStatus.DEGRADED:
        raise AgentDegradedError("Agent is degraded.", agent_id=str(agent.id))
    raise AgentOfflineError(
        "Agent is offline and cannot accept this transition.",
        agent_id=str(agent.id),
        status=agent.status.value,
    )


def _raise_drain_conflict(agent: AgentRecord, *, expected: AgentStatus) -> NoReturn:
    raise AgentDrainingError(
        "Agent drain state changed concurrently.",
        agent_id=str(agent.id),
        expected_status=expected.value,
        current_status=agent.status.value,
    )
