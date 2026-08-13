from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lab_platform.agent_protocol.errors import ProtocolSequenceError
from lab_platform.control_plane_core.errors import (
    AgentAuthenticationFailedError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentRevokedError,
)
from lab_platform.core.errors import PlatformError
from lab_platform.models import (
    AgentConnectionRecord,
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
)


class AgentIdentityConflictError(PlatformError):
    code = "AGENT_IDENTITY_CONFLICT"


class AgentConnectionConflictError(PlatformError):
    code = "AGENT_CONNECTION_CONFLICT"


class StaleAgentConnectionError(PlatformError):
    code = "AGENT_CONNECTION_STALE"


@dataclass(frozen=True, slots=True)
class ActiveAgentConnection:
    connection: AgentConnectionRecord
    last_activity_monotonic: float


@dataclass(frozen=True, slots=True)
class ConnectionRegistration:
    agent: AgentRecord
    connection: AgentConnectionRecord
    superseded_connection_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PresenceTransition:
    agent: AgentRecord
    connection: AgentConnectionRecord
    previous_status: AgentStatus


class AgentOfflineInventory(Protocol):
    async def mark_agent_offline(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime,
    ) -> object: ...


class AgentPresenceRepository(Protocol):
    """Atomic persistence boundary for one active connection per Agent."""

    async def add_agent(self, agent: AgentRecord) -> AgentRecord: ...

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...

    async def list_agents(self) -> list[AgentRecord]: ...

    async def get_connection(self, connection_id: UUID) -> ActiveAgentConnection | None: ...

    async def get_active_connection(self, agent_id: UUID) -> ActiveAgentConnection | None: ...

    async def list_active_connections(self) -> list[ActiveAgentConnection]: ...

    async def activate(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> ActiveAgentConnection | None: ...

    async def update_if_current(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> bool: ...

    async def disconnect_if_current(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> bool: ...


class InMemoryAgentPresenceRepository:
    """Deterministic repository double with the same fencing expected from SQL."""

    def __init__(self, agents: Iterable[AgentRecord] = ()) -> None:
        self._agents: dict[UUID, AgentRecord] = {}
        self._agent_ids_by_slug: dict[str, UUID] = {}
        self._connections: dict[UUID, ActiveAgentConnection] = {}
        self._active_connection_ids: dict[UUID, UUID] = {}
        for agent in agents:
            self._insert_agent(agent)

    async def add_agent(self, agent: AgentRecord) -> AgentRecord:
        existing = self._agents.get(agent.id)
        if existing is not None:
            _require_same_agent_identity(existing, agent)
            return existing
        self._insert_agent(agent)
        return agent

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        return self._agents.get(agent_id)

    async def list_agents(self) -> list[AgentRecord]:
        return sorted(self._agents.values(), key=lambda agent: (agent.slug, str(agent.id)))

    async def get_connection(self, connection_id: UUID) -> ActiveAgentConnection | None:
        return self._connections.get(connection_id)

    async def get_active_connection(self, agent_id: UUID) -> ActiveAgentConnection | None:
        connection_id = self._active_connection_ids.get(agent_id)
        return self._connections.get(connection_id) if connection_id is not None else None

    async def list_active_connections(self) -> list[ActiveAgentConnection]:
        return [
            self._connections[connection_id]
            for agent_id, connection_id in sorted(
                self._active_connection_ids.items(),
                key=lambda item: str(item[0]),
            )
        ]

    async def activate(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> ActiveAgentConnection | None:
        canonical = self._agents.get(agent.id)
        if canonical is None:
            raise AgentNotFoundError("The Agent does not exist.", agent_id=str(agent.id))
        _require_same_agent_identity(canonical, agent)
        _require_connection_owner(connection, agent.id)

        existing = self._connections.get(connection.connection.id)
        if existing is not None:
            if (
                existing.connection.agent_id != agent.id
                or existing.connection.boot_id != connection.connection.boot_id
            ):
                raise AgentConnectionConflictError(
                    "The connection ID is already bound to another Agent process.",
                    connection_id=str(connection.connection.id),
                )
            active_id = self._active_connection_ids.get(agent.id)
            if (
                active_id == connection.connection.id
                and existing.connection.disconnected_at is None
            ):
                return None
            raise AgentConnectionConflictError(
                "A disconnected connection ID cannot be reused.",
                connection_id=str(connection.connection.id),
            )

        previous = await self.get_active_connection(agent.id)
        if previous is not None:
            disconnected_at = max(
                connection.connection.connected_at,
                previous.connection.last_heartbeat_at,
            )
            self._connections[previous.connection.id] = ActiveAgentConnection(
                connection=previous.connection.model_copy(
                    update={"disconnected_at": disconnected_at}
                ),
                last_activity_monotonic=previous.last_activity_monotonic,
            )

        self._agents[agent.id] = agent
        self._connections[connection.connection.id] = connection
        self._active_connection_ids[agent.id] = connection.connection.id
        return previous

    async def update_if_current(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> bool:
        stored = self._current(connection)
        if stored is None:
            return False
        if (
            connection.connection.last_sequence_number < stored.connection.last_sequence_number
            or connection.last_activity_monotonic < stored.last_activity_monotonic
        ):
            return False
        canonical = self._agents.get(agent.id)
        if canonical is None:
            return False
        _require_same_agent_identity(canonical, agent)
        _require_connection_owner(connection, agent.id)
        self._agents[agent.id] = agent
        self._connections[connection.connection.id] = connection
        return True

    async def disconnect_if_current(
        self,
        agent: AgentRecord,
        connection: ActiveAgentConnection,
    ) -> bool:
        stored = self._current(connection)
        if stored is None or connection.last_activity_monotonic < stored.last_activity_monotonic:
            return False
        if connection.connection.disconnected_at is None:
            raise ValueError("Disconnected connection record must include disconnected_at")
        canonical = self._agents.get(agent.id)
        if canonical is None:
            return False
        _require_same_agent_identity(canonical, agent)
        _require_connection_owner(connection, agent.id)
        self._agents[agent.id] = agent
        self._connections[connection.connection.id] = connection
        self._active_connection_ids.pop(agent.id, None)
        return True

    def _insert_agent(self, agent: AgentRecord) -> None:
        existing = self._agents.get(agent.id)
        if existing is not None:
            _require_same_agent_identity(existing, agent)
            raise AgentIdentityConflictError(
                "The Agent ID is already registered.",
                agent_id=str(agent.id),
            )
        slug_owner = self._agent_ids_by_slug.get(agent.slug)
        if slug_owner is not None and slug_owner != agent.id:
            raise AgentIdentityConflictError(
                "The Agent slug is already bound to another Agent.",
                agent_slug=agent.slug,
                existing_agent_id=str(slug_owner),
                received_agent_id=str(agent.id),
            )
        self._agents[agent.id] = agent
        self._agent_ids_by_slug[agent.slug] = agent.id

    def _current(
        self,
        connection: ActiveAgentConnection,
    ) -> ActiveAgentConnection | None:
        active_id = self._active_connection_ids.get(connection.connection.agent_id)
        if active_id != connection.connection.id:
            return None
        stored = self._connections.get(connection.connection.id)
        if stored is None or stored.connection.boot_id != connection.connection.boot_id:
            return None
        return stored


class AgentPresenceService:
    """Track authenticated Agent connections using fenced, monotonic liveness."""

    def __init__(
        self,
        repository: AgentPresenceRepository,
        *,
        heartbeat_timeout_seconds: float = 45,
        offline_timeout_seconds: float = 90,
        offline_inventory: AgentOfflineInventory | None = None,
    ) -> None:
        if not math.isfinite(heartbeat_timeout_seconds) or heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive and finite")
        if (
            not math.isfinite(offline_timeout_seconds)
            or offline_timeout_seconds <= heartbeat_timeout_seconds
        ):
            raise ValueError("offline_timeout_seconds must be longer than heartbeat timeout")
        self._repository = repository
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._offline_timeout_seconds = offline_timeout_seconds
        self._offline_inventory = offline_inventory

    async def register_authenticated_connection(
        self,
        agent: AgentRecord,
        *,
        connection_id: UUID,
        boot_id: UUID,
        protocol_version: str,
        observed_at: datetime,
        observed_monotonic: float,
        sequence_number: int = 1,
        agent_version: str | None = None,
        observed_clock_offset_seconds: float = 0,
    ) -> ConnectionRegistration:
        now = _as_utc(observed_at, field="connection timestamp")
        _validate_monotonic(observed_monotonic)
        _validate_sequence(sequence_number, minimum=1)
        if not math.isfinite(observed_clock_offset_seconds):
            raise ValueError("observed clock offset must be finite")

        canonical = await self._repository.get_agent(agent.id)
        if canonical is None:
            raise AgentNotFoundError("The Agent does not exist.", agent_id=str(agent.id))
        _require_same_agent_identity(canonical, agent)
        _require_connectable_agent(canonical)

        existing = await self._repository.get_connection(connection_id)
        if existing is not None:
            if (
                existing.connection.agent_id == agent.id
                and existing.connection.boot_id == boot_id
                and existing.connection.protocol_version == protocol_version
                and existing.connection.last_sequence_number == sequence_number
                and existing.connection.disconnected_at is None
                and (await self._repository.get_active_connection(agent.id)) == existing
            ):
                return ConnectionRegistration(
                    agent=canonical,
                    connection=existing.connection,
                )
            raise AgentConnectionConflictError(
                "The connection ID is already bound and cannot be reused.",
                connection_id=str(connection_id),
            )

        effective_now = _non_regressing_agent_time(canonical, now)
        connected_agent = canonical.model_copy(
            update={
                "status": _connected_status(canonical.status),
                "version": agent_version or canonical.version,
                "protocol_version": protocol_version,
                "last_connected_at": effective_now,
                "last_seen_at": effective_now,
                "disconnected_at": None,
            }
        )
        connection_record = AgentConnectionRecord(
            id=connection_id,
            agent_id=agent.id,
            boot_id=boot_id,
            protocol_version=protocol_version,
            connected_at=effective_now,
            last_heartbeat_at=effective_now,
            last_sequence_number=sequence_number,
            observed_clock_offset_seconds=observed_clock_offset_seconds,
        )
        active = ActiveAgentConnection(
            connection=connection_record,
            last_activity_monotonic=observed_monotonic,
        )
        superseded = await self._repository.activate(connected_agent, active)
        return ConnectionRegistration(
            agent=connected_agent,
            connection=connection_record,
            superseded_connection_id=(superseded.connection.id if superseded is not None else None),
        )

    async def record_heartbeat(
        self,
        agent_id: UUID,
        *,
        connection_id: UUID,
        boot_id: UUID,
        sequence_number: int,
        observed_at: datetime,
        observed_monotonic: float,
        observed_clock_offset_seconds: float | None = None,
    ) -> ConnectionRegistration:
        now = _as_utc(observed_at, field="heartbeat timestamp")
        _validate_monotonic(observed_monotonic)
        current = await self.require_current_connection(
            agent_id,
            connection_id=connection_id,
            boot_id=boot_id,
        )
        _validate_sequence(
            sequence_number,
            minimum=current.connection.last_sequence_number + 1,
        )
        if observed_monotonic < current.last_activity_monotonic:
            raise ProtocolSequenceError(
                "Agent heartbeat monotonic receipt time moved backwards.",
                previous_monotonic=current.last_activity_monotonic,
                received_monotonic=observed_monotonic,
            )
        if observed_clock_offset_seconds is not None and not math.isfinite(
            observed_clock_offset_seconds
        ):
            raise ValueError("observed clock offset must be finite")

        agent = await self.get_agent(agent_id)
        effective_now = max(
            now,
            current.connection.last_heartbeat_at,
            agent.last_seen_at or agent.registered_at,
        )
        updated_agent = agent.model_copy(
            update={
                "status": _connected_status(agent.status),
                "last_seen_at": effective_now,
                "disconnected_at": None,
            }
        )
        updated_record = current.connection.model_copy(
            update={
                "last_heartbeat_at": effective_now,
                "last_sequence_number": sequence_number,
                "observed_clock_offset_seconds": (
                    observed_clock_offset_seconds
                    if observed_clock_offset_seconds is not None
                    else current.connection.observed_clock_offset_seconds
                ),
            }
        )
        updated = ActiveAgentConnection(
            connection=updated_record,
            last_activity_monotonic=observed_monotonic,
        )
        if not await self._repository.update_if_current(updated_agent, updated):
            raise StaleAgentConnectionError(
                "The Agent connection was superseded while recording its heartbeat.",
                agent_id=str(agent_id),
                connection_id=str(connection_id),
            )
        return ConnectionRegistration(agent=updated_agent, connection=updated_record)

    async def require_current_connection(
        self,
        agent_id: UUID,
        *,
        connection_id: UUID,
        boot_id: UUID,
    ) -> ActiveAgentConnection:
        current = await self._repository.get_active_connection(agent_id)
        if (
            current is None
            or current.connection.id != connection_id
            or current.connection.boot_id != boot_id
            or current.connection.disconnected_at is not None
        ):
            raise StaleAgentConnectionError(
                "The Agent connection is no longer current.",
                agent_id=str(agent_id),
                connection_id=str(connection_id),
                boot_id=str(boot_id),
            )
        return current

    async def active_connection(self, agent_id: UUID) -> ActiveAgentConnection | None:
        return await self._repository.get_active_connection(agent_id)

    async def mark_degraded(
        self,
        agent_id: UUID,
        *,
        connection_id: UUID,
        boot_id: UUID,
    ) -> AgentRecord:
        current = await self.require_current_connection(
            agent_id,
            connection_id=connection_id,
            boot_id=boot_id,
        )
        agent = await self.get_agent(agent_id)
        if agent.status in {
            AgentStatus.REVOKED,
            AgentStatus.DRAINING,
            AgentStatus.DRAINED,
            AgentStatus.DEGRADED,
        }:
            return agent
        degraded = agent.model_copy(update={"status": AgentStatus.DEGRADED})
        if not await self._repository.update_if_current(degraded, current):
            raise StaleAgentConnectionError(
                "The Agent connection was superseded while marking it degraded.",
                agent_id=str(agent_id),
                connection_id=str(connection_id),
            )
        return degraded

    async def disconnect(
        self,
        agent_id: UUID,
        *,
        connection_id: UUID,
        boot_id: UUID,
        observed_at: datetime,
    ) -> bool:
        current = await self._repository.get_active_connection(agent_id)
        if (
            current is None
            or current.connection.id != connection_id
            or current.connection.boot_id != boot_id
        ):
            return False
        agent = await self.get_agent(agent_id)
        now = _as_utc(observed_at, field="disconnect timestamp")
        transition = _offline_transition(agent, current, now)
        # Fail safe: make remote benches unavailable before publishing the Agent
        # as disconnected. If the connection is superseded during this call, the
        # replacement snapshot can restore them; they are never left falsely online.
        await self._cascade_agent_offline(agent_id, transition.agent.disconnected_at)
        changed = await self._repository.disconnect_if_current(
            transition.agent,
            ActiveAgentConnection(
                transition.connection,
                current.last_activity_monotonic,
            ),
        )
        return changed

    async def check_timeouts(
        self,
        *,
        observed_at: datetime,
        observed_monotonic: float,
    ) -> tuple[PresenceTransition, ...]:
        now = _as_utc(observed_at, field="presence timeout timestamp")
        _validate_monotonic(observed_monotonic)
        transitions: list[PresenceTransition] = []
        for current in await self._repository.list_active_connections():
            if observed_monotonic < current.last_activity_monotonic:
                raise ValueError("Presence timeout monotonic clock moved backwards")
            elapsed = observed_monotonic - current.last_activity_monotonic
            agent = await self.get_agent(current.connection.agent_id)
            if elapsed >= self._offline_timeout_seconds:
                transition = _offline_transition(agent, current, now)
                await self._cascade_agent_offline(
                    agent.id,
                    transition.agent.disconnected_at,
                )
                changed = await self._repository.disconnect_if_current(
                    transition.agent,
                    ActiveAgentConnection(
                        transition.connection,
                        current.last_activity_monotonic,
                    ),
                )
                if changed:
                    transitions.append(transition)
                continue
            if elapsed < self._heartbeat_timeout_seconds or agent.status is AgentStatus.DEGRADED:
                continue
            degraded = agent.model_copy(update={"status": AgentStatus.DEGRADED})
            if await self._repository.update_if_current(degraded, current):
                transitions.append(
                    PresenceTransition(
                        agent=degraded,
                        connection=current.connection,
                        previous_status=agent.status,
                    )
                )
        return tuple(transitions)

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        organisation_id: UUID | None = None,
    ) -> AgentRecord:
        agent = (
            await self._repository.get_agent(agent_id)
            if organisation_id is None
            else await self._repository.get_agent(  # type: ignore[call-arg]
                agent_id,
                organisation_id=organisation_id,
            )
        )
        if agent is None:
            raise AgentNotFoundError("The Agent does not exist.", agent_id=str(agent_id))
        return agent

    async def list_agents(
        self,
        *,
        organisation_id: UUID | None = None,
        status: AgentStatus | None = None,
        location: str | None = None,
        labels: Mapping[str, str] | None = None,
        version: str | None = None,
    ) -> list[AgentRecord]:
        agents = (
            await self._repository.list_agents()
            if organisation_id is None
            else await self._repository.list_agents(  # type: ignore[call-arg]
                organisation_id=organisation_id
            )
        )
        if status is not None:
            agents = [agent for agent in agents if agent.status is status]
        if location is not None:
            normalized_location = location.strip().casefold()
            agents = [
                agent
                for agent in agents
                if agent.location is not None and agent.location.casefold() == normalized_location
            ]
        for key, value in (labels or {}).items():
            agents = [agent for agent in agents if agent.labels.get(key) == value]
        if version is not None:
            agents = [agent for agent in agents if agent.version == version]
        return sorted(agents, key=lambda agent: (agent.slug, str(agent.id)))

    async def _cascade_agent_offline(
        self,
        agent_id: UUID,
        observed_at: datetime | None,
    ) -> None:
        if self._offline_inventory is None:
            return
        if observed_at is None:  # pragma: no cover - offline transition always sets it
            raise RuntimeError("Offline Agent transition did not set disconnected_at")
        await self._offline_inventory.mark_agent_offline(
            agent_id,
            observed_at=observed_at,
        )


def _require_connectable_agent(agent: AgentRecord) -> None:
    if agent.status is AgentStatus.REVOKED:
        raise AgentRevokedError("The Agent has been revoked.", agent_id=str(agent.id))
    if agent.status is AgentStatus.INCOMPATIBLE:
        raise AgentIncompatibleError("The Agent protocol is incompatible.", agent_id=str(agent.id))
    if (
        agent.enrollment_status is not EnrollmentStatus.ENROLLED
        or agent.status is AgentStatus.PENDING
    ):
        raise AgentAuthenticationFailedError("Agent authentication failed.")


def _connected_status(status: AgentStatus) -> AgentStatus:
    if status in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
        return status
    return AgentStatus.ONLINE


def _require_same_agent_identity(expected: AgentRecord, received: AgentRecord) -> None:
    if expected.id != received.id or expected.slug != received.slug:
        raise AgentIdentityConflictError(
            "Authenticated Agent identity does not match the registered identity.",
            expected_agent_id=str(expected.id),
            received_agent_id=str(received.id),
            expected_slug=expected.slug,
            received_slug=received.slug,
        )


def _require_connection_owner(connection: ActiveAgentConnection, agent_id: UUID) -> None:
    if connection.connection.agent_id != agent_id:
        raise AgentConnectionConflictError(
            "The connection belongs to another Agent.",
            connection_id=str(connection.connection.id),
            expected_agent_id=str(agent_id),
            received_agent_id=str(connection.connection.agent_id),
        )


def _offline_transition(
    agent: AgentRecord,
    current: ActiveAgentConnection,
    observed_at: datetime,
) -> PresenceTransition:
    disconnected_at = max(
        observed_at,
        current.connection.last_heartbeat_at,
        agent.last_seen_at or agent.registered_at,
    )
    disconnected_status = (
        agent.status
        if agent.status in {AgentStatus.DRAINING, AgentStatus.DRAINED}
        else AgentStatus.OFFLINE
    )
    offline_agent = agent.model_copy(
        update={
            "status": disconnected_status,
            "disconnected_at": disconnected_at,
        }
    )
    disconnected_connection = current.connection.model_copy(
        update={"disconnected_at": disconnected_at}
    )
    return PresenceTransition(
        agent=offline_agent,
        connection=disconnected_connection,
        previous_status=agent.status,
    )


def _non_regressing_agent_time(agent: AgentRecord, observed_at: datetime) -> datetime:
    candidates = [agent.registered_at, observed_at]
    if agent.last_connected_at is not None:
        candidates.append(agent.last_connected_at)
    if agent.last_seen_at is not None:
        candidates.append(agent.last_seen_at)
    if agent.disconnected_at is not None:
        candidates.append(agent.disconnected_at)
    return max(candidates)


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_monotonic(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Monotonic receipt time must be finite and non-negative")


def _validate_sequence(value: int, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ProtocolSequenceError(
            "Agent connection sequence did not advance monotonically.",
            minimum_sequence=minimum,
            received_sequence=value,
        )
