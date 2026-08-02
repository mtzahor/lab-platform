from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from lab_platform.agent_protocol import (
    CommandAcceptedPayload,
    CommandCancelPayload,
    CommandRejectedPayload,
    CommandRequestPayload,
    MessageType,
    OperationEventPayload,
)
from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    RemoteCommandDeliveryFailedError,
    RemoteCommandDuplicateError,
    RemoteCommandExpiredError,
    RemoteCommandNotFoundError,
)
from lab_platform.models import (
    DISTRIBUTED_OPERATION_TRANSITIONS,
    REMOTE_COMMAND_TRANSITIONS,
    AgentRecord,
    AgentStatus,
    DistributedOperation,
    DistributedOperationStatus,
    GlobalBenchRecord,
    GlobalBenchStatus,
    RemoteCommand,
    RemoteCommandAttempt,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
)

_MUTATING_COMMANDS = frozenset(
    {
        RemoteCommandType.FLASH,
        RemoteCommandType.RESET,
        RemoteCommandType.RUN_WORKFLOW,
        RemoteCommandType.CANCEL_OPERATION,
    }
)
_TRANSIENT_ARTIFACT_FIELDS = frozenset(
    {"transfer_id", "download_url", "transfer_token", "expires_at"}
)


@dataclass(frozen=True, slots=True)
class CommandDeliveryReceipt:
    connection_id: UUID
    sequence_number: int
    dispatched_at: datetime


class AgentCommandTransport(Protocol):
    async def is_connected(self, agent_id: UUID) -> bool: ...

    async def send_command(
        self,
        agent_id: UUID,
        payload: CommandRequestPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt: ...

    async def send_cancel(
        self,
        agent_id: UUID,
        payload: CommandCancelPayload,
        *,
        correlation_id: UUID,
    ) -> CommandDeliveryReceipt: ...


class RemoteCommandPayloadHydrator(Protocol):
    """Build delivery-attempt payload fields without changing durable command state."""

    async def hydrate_payload(self, command: RemoteCommand) -> Mapping[str, Any]: ...


class DistributedDirectory(Protocol):
    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...

    async def get_bench(self, bench_id: str) -> GlobalBenchRecord | None: ...


class RemoteCommandRepository(Protocol):
    async def create_bundle(
        self,
        command: RemoteCommand,
        operation: DistributedOperation | None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]: ...

    async def get_command(self, command_id: UUID) -> RemoteCommand | None: ...

    async def get_by_idempotency_key(
        self,
        agent_id: UUID,
        idempotency_key: str,
    ) -> RemoteCommand | None: ...

    async def update_command(
        self,
        command: RemoteCommand,
        *,
        expected_statuses: Iterable[RemoteCommandStatus],
    ) -> RemoteCommand | None: ...

    async def get_operation_for_command(self, command_id: UUID) -> DistributedOperation | None: ...

    async def update_operation(
        self,
        operation: DistributedOperation,
        *,
        expected_statuses: Iterable[DistributedOperationStatus],
    ) -> DistributedOperation | None: ...

    async def record_attempt(self, attempt: RemoteCommandAttempt) -> RemoteCommandAttempt: ...

    async def acknowledge_latest_attempt(
        self,
        command_id: UUID,
        acknowledged_at: datetime,
    ) -> None: ...

    async def list_commands(
        self,
        *,
        agent_id: UUID | None = None,
        statuses: Iterable[RemoteCommandStatus] | None = None,
        limit: int = 500,
    ) -> list[RemoteCommand]: ...


class InMemoryRemoteCommandRepository:
    """Atomic reference repository used by demos and distributed contract tests."""

    def __init__(self) -> None:
        self._commands: dict[UUID, RemoteCommand] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._operations: dict[UUID, DistributedOperation] = {}
        self._operation_by_command: dict[UUID, UUID] = {}
        self._attempts: dict[tuple[UUID, int], RemoteCommandAttempt] = {}
        self._lock = asyncio.Lock()

    async def create_bundle(
        self,
        command: RemoteCommand,
        operation: DistributedOperation | None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        async with self._lock:
            key = (command.agent_id, command.idempotency_key)
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                existing = self._commands[existing_id]
                return existing, self._operation_for_command(existing.id)
            if command.id in self._commands:
                existing = self._commands[command.id]
                return existing, self._operation_for_command(existing.id)
            if operation is not None and operation.remote_command_id != command.id:
                raise ValueError("Distributed operation is not bound to its remote command")
            self._commands[command.id] = command
            self._idempotency[key] = command.id
            if operation is not None:
                self._operations[operation.id] = operation
                self._operation_by_command[command.id] = operation.id
            return command, operation

    async def get_command(self, command_id: UUID) -> RemoteCommand | None:
        async with self._lock:
            return self._commands.get(command_id)

    async def get_by_idempotency_key(
        self,
        agent_id: UUID,
        idempotency_key: str,
    ) -> RemoteCommand | None:
        async with self._lock:
            command_id = self._idempotency.get((agent_id, idempotency_key))
            return self._commands.get(command_id) if command_id is not None else None

    async def update_command(
        self,
        command: RemoteCommand,
        *,
        expected_statuses: Iterable[RemoteCommandStatus],
    ) -> RemoteCommand | None:
        expected = frozenset(expected_statuses)
        async with self._lock:
            current = self._commands.get(command.id)
            if current is None or current.status not in expected:
                return None
            self._commands[command.id] = command
            return command

    async def get_operation_for_command(self, command_id: UUID) -> DistributedOperation | None:
        async with self._lock:
            return self._operation_for_command(command_id)

    async def update_operation(
        self,
        operation: DistributedOperation,
        *,
        expected_statuses: Iterable[DistributedOperationStatus],
    ) -> DistributedOperation | None:
        expected = frozenset(expected_statuses)
        async with self._lock:
            current = self._operations.get(operation.id)
            if current is None or current.status not in expected:
                return None
            if (
                current.last_agent_update_at is not None
                and operation.last_agent_update_at is not None
                and operation.last_agent_update_at < current.last_agent_update_at
            ):
                return None
            self._operations[operation.id] = operation
            return operation

    async def record_attempt(self, attempt: RemoteCommandAttempt) -> RemoteCommandAttempt:
        async with self._lock:
            key = (attempt.command_id, attempt.attempt_number)
            existing = self._attempts.get(key)
            if existing is not None:
                if existing != attempt:
                    raise RemoteCommandDuplicateError(
                        "Command attempt number was reused with different content.",
                        command_id=str(attempt.command_id),
                        attempt_number=attempt.attempt_number,
                    )
                return existing
            self._attempts[key] = attempt
            current = self._commands.get(attempt.command_id)
            if current is None:
                raise RemoteCommandNotFoundError("Remote command does not exist.")
            if current.attempt_count != attempt.attempt_number - 1:
                raise RemoteCommandDuplicateError("Remote command attempt sequence is invalid.")
            self._commands[current.id] = _command_copy(
                current,
                attempt_count=attempt.attempt_number,
            )
            return attempt

    async def acknowledge_latest_attempt(
        self,
        command_id: UUID,
        acknowledged_at: datetime,
    ) -> None:
        async with self._lock:
            attempts = [
                attempt
                for (stored_command_id, _), attempt in self._attempts.items()
                if stored_command_id == command_id
            ]
            if not attempts:
                return
            latest = max(attempts, key=lambda item: item.attempt_number)
            if latest.failed_at is not None or latest.acknowledged_at is not None:
                return
            self._attempts[(command_id, latest.attempt_number)] = (
                RemoteCommandAttempt.model_validate(
                    {**latest.model_dump(), "acknowledged_at": acknowledged_at}
                )
            )

    async def list_commands(
        self,
        *,
        agent_id: UUID | None = None,
        statuses: Iterable[RemoteCommandStatus] | None = None,
        limit: int = 500,
    ) -> list[RemoteCommand]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        selected = frozenset(statuses) if statuses is not None else None
        async with self._lock:
            commands = [
                command
                for command in self._commands.values()
                if (agent_id is None or command.agent_id == agent_id)
                and (selected is None or command.status in selected)
            ]
        return sorted(commands, key=lambda item: (item.created_at, str(item.id)))[:limit]

    def _operation_for_command(self, command_id: UUID) -> DistributedOperation | None:
        operation_id = self._operation_by_command.get(command_id)
        return self._operations.get(operation_id) if operation_id is not None else None


class RemoteCommandService:
    """Durable at-least-once dispatch with Agent-confirmed operation state."""

    def __init__(
        self,
        repository: RemoteCommandRepository,
        directory: DistributedDirectory,
        transport: AgentCommandTransport,
        *,
        queue_commands_for_offline_agents: bool = False,
        reconciliation_timeout_seconds: int = 600,
        payload_hydrator: RemoteCommandPayloadHydrator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if reconciliation_timeout_seconds <= 0:
            raise ValueError("reconciliation_timeout_seconds must be positive")
        self._repository = repository
        self._directory = directory
        self._transport = transport
        self._queue_offline = queue_commands_for_offline_agents
        self._reconciliation_timeout = timedelta(seconds=reconciliation_timeout_seconds)
        self._payload_hydrator = payload_hydrator
        self._clock = clock or _utc_now

    async def create(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        command_type: RemoteCommandType,
        payload: Mapping[str, Any],
        expires_at: datetime,
        idempotency_key: str,
        reservation_lease: ReservationLease | None = None,
        operation_type: str | None = None,
        dispatch: bool = True,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        now = _as_utc(self._clock())
        expiry = _as_utc(expires_at)
        if expiry <= now:
            raise RemoteCommandExpiredError(
                "Remote command expiry must be in the future.",
                expires_at=expiry.isoformat(),
            )
        _validate_durable_artifact_payload(command_type, payload)
        agent, bench = await self._require_route(agent_id, bench_id)
        existing = await self._repository.get_by_idempotency_key(agent_id, idempotency_key)
        if existing is not None:
            self._validate_idempotent_replay(
                existing,
                bench_id=bench.id,
                command_type=command_type,
                payload=payload,
                lease=reservation_lease,
            )
            operation = await self._repository.get_operation_for_command(existing.id)
            return existing, operation

        agent_is_offline = agent.status is AgentStatus.OFFLINE
        self._validate_agent_accepts_new_work(
            agent,
            allow_offline_queue=self._queue_offline,
        )
        if bench.status is GlobalBenchStatus.OFFLINE and not (
            agent_is_offline and self._queue_offline
        ):
            raise AgentOfflineError(
                "Owning bench is offline.",
                agent_id=str(agent_id),
                bench_id=bench_id,
            )
        self._validate_lease(
            agent_id=agent_id,
            bench_id=bench_id,
            command_type=command_type,
            lease=reservation_lease,
            now=now,
        )

        command = RemoteCommand(
            agent_id=agent_id,
            bench_id=bench.id,
            command_type=command_type,
            payload=dict(payload),
            status=(
                RemoteCommandStatus.QUEUED if agent_is_offline else RemoteCommandStatus.CREATED
            ),
            created_at=now,
            expires_at=expiry,
            idempotency_key=idempotency_key,
            reservation_id=(reservation_lease.reservation_id if reservation_lease else None),
            lease_version=(reservation_lease.lease_version if reservation_lease else None),
        )
        operation = (
            DistributedOperation(
                remote_command_id=command.id,
                agent_id=agent_id,
                bench_id=bench.id,
                reservation_id=command.reservation_id,
                operation_type=operation_type or command_type.value,
                created_at=now,
            )
            if operation_type is not None or command_type is not RemoteCommandType.REFRESH_INVENTORY
            else None
        )
        if operation is not None:
            command = _command_copy(command, operation_id=operation.id)
        created_command, created_operation = await self._repository.create_bundle(
            command,
            operation,
        )
        if created_command.id != command.id:
            self._validate_idempotent_replay(
                created_command,
                bench_id=bench.id,
                command_type=command_type,
                payload=payload,
                lease=reservation_lease,
            )
            return created_command, created_operation
        if not dispatch or agent_is_offline:
            return created_command, created_operation
        return await self.dispatch(created_command.id, reservation_lease=reservation_lease)

    async def dispatch(
        self,
        command_id: UUID,
        *,
        reservation_lease: ReservationLease | None = None,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        command = await self._require_command(command_id)
        operation = await self._repository.get_operation_for_command(command.id)
        now = _as_utc(self._clock())
        if command.expires_at <= now:
            return await self._expire(command, operation, now)
        agent, _ = await self._require_route(command.agent_id, command.bench_id)
        self._validate_agent_accepts_new_work(agent)
        if not await self._transport.is_connected(command.agent_id):
            if self._queue_offline and command.status in {
                RemoteCommandStatus.CREATED,
                RemoteCommandStatus.QUEUED,
            }:
                queued = _command_copy(command, status=RemoteCommandStatus.QUEUED)
                persisted = await self._repository.update_command(
                    queued,
                    expected_statuses={command.status},
                )
                return persisted or await self._require_command(command.id), operation
            raise AgentOfflineError(
                "Owning Agent has no active command channel.",
                agent_id=str(command.agent_id),
            )

        self._validate_dispatch_lease(command, reservation_lease, now)
        previous_status = command.status
        if previous_status in {RemoteCommandStatus.CREATED, RemoteCommandStatus.QUEUED}:
            command = _command_copy(
                command,
                status=RemoteCommandStatus.DISPATCHED,
                dispatched_at=now,
            )
            updated = await self._repository.update_command(
                command,
                expected_statuses={previous_status},
            )
            if updated is None:
                return await self.dispatch(command_id, reservation_lease=reservation_lease)
            command = updated
            if operation is not None and operation.status is DistributedOperationStatus.CREATED:
                candidate = _operation_copy(
                    operation,
                    status=DistributedOperationStatus.DISPATCHED,
                    dispatched_at=now,
                )
                operation = (
                    await self._repository.update_operation(
                        candidate,
                        expected_statuses={DistributedOperationStatus.CREATED},
                    )
                    or operation
                )
        elif previous_status is not RemoteCommandStatus.UNKNOWN:
            return command, operation

        attempt_number = command.attempt_count + 1
        try:
            # UNKNOWN is durable control-plane uncertainty, not a valid first-seen Agent dispatch
            # status. A transient DISPATCHED view lets an Agent that never journaled the lost
            # attempt accept the replay, while an Agent that did journal it still deduplicates by
            # immutable execution content.
            outbound_command = (
                _command_copy(command, status=RemoteCommandStatus.DISPATCHED)
                if command.status is RemoteCommandStatus.UNKNOWN
                else command
            )
            if self._payload_hydrator is not None:
                transient_payload = await self._payload_hydrator.hydrate_payload(command)
                outbound_command = _command_copy(
                    outbound_command,
                    payload=dict(transient_payload),
                )
            receipt = await self._transport.send_command(
                command.agent_id,
                CommandRequestPayload(
                    command=outbound_command,
                    reservation_lease=reservation_lease,
                ),
                correlation_id=command.id,
            )
        except Exception as exc:
            failed_attempt = RemoteCommandAttempt(
                command_id=command.id,
                attempt_number=attempt_number,
                dispatched_at=now,
                failed_at=_as_utc(self._clock()),
                error_code=RemoteCommandDeliveryFailedError.code,
            )
            await self._repository.record_attempt(failed_attempt)
            await self.mark_unknown(command.agent_id, observed_at=_as_utc(self._clock()))
            raise RemoteCommandDeliveryFailedError(
                "Remote command delivery failed; state requires reconciliation.",
                command_id=str(command.id),
                agent_id=str(command.agent_id),
            ) from exc

        await self._repository.record_attempt(
            RemoteCommandAttempt(
                command_id=command.id,
                attempt_number=attempt_number,
                connection_id=receipt.connection_id,
                sequence_number=receipt.sequence_number,
                dispatched_at=receipt.dispatched_at,
            )
        )
        command = await self._require_command(command.id)
        return command, operation

    async def request_cancel(
        self,
        command_id: UUID,
        *,
        reason: str | None = None,
    ) -> RemoteCommand:
        """Request cancellation of the original durable Agent command.

        Cancellation is a protocol control message, not a second remote operation.  The
        command remains non-terminal until the Agent confirms ``OPERATION_CANCELLED``;
        callers persist their own cancellation intent and may safely retry this method.
        Commands that were never dispatched can be cancelled atomically without involving
        an Agent that has never journaled them.
        """

        command = await self._require_command(command_id)
        if command.status in {
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.EXPIRED,
        }:
            return command

        if command.status in {RemoteCommandStatus.CREATED, RemoteCommandStatus.QUEUED}:
            now = _as_utc(self._clock())
            cancelled = _command_copy(
                command,
                status=RemoteCommandStatus.CANCELLED,
                completed_at=now,
                error_message=reason,
            )
            current = await self._repository.update_command(
                cancelled,
                expected_statuses={command.status},
            )
            if current is None:
                raced = await self._require_command(command.id)
                if raced.status not in {
                    RemoteCommandStatus.SUCCEEDED,
                    RemoteCommandStatus.FAILED,
                    RemoteCommandStatus.CANCELLED,
                    RemoteCommandStatus.EXPIRED,
                }:
                    return await self.request_cancel(raced.id, reason=reason)
                return raced
            command = current
            operation = await self._repository.get_operation_for_command(command.id)
            if operation is not None and operation.status is DistributedOperationStatus.CREATED:
                await self._repository.update_operation(
                    _operation_copy(
                        operation,
                        status=DistributedOperationStatus.CANCELLED,
                        completed_at=now,
                        error_message=reason,
                    ),
                    expected_statuses={DistributedOperationStatus.CREATED},
                )
            return command

        await self._transport.send_cancel(
            command.agent_id,
            CommandCancelPayload(command_id=command.id, reason=reason),
            correlation_id=command.id,
        )
        return command

    async def accepted(
        self,
        agent_id: UUID,
        payload: CommandAcceptedPayload,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        command = await self._require_owned_command(agent_id, payload.command_id)
        if command.status in {
            RemoteCommandStatus.ACCEPTED,
            RemoteCommandStatus.RUNNING,
            RemoteCommandStatus.SUCCEEDED,
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
        }:
            return command, await self._repository.get_operation_for_command(command.id)
        updated = _command_copy(
            command,
            status=RemoteCommandStatus.ACCEPTED,
            acknowledged_at=payload.accepted_at,
        )
        command = await self._repository.update_command(
            updated,
            expected_statuses={RemoteCommandStatus.DISPATCHED, RemoteCommandStatus.UNKNOWN},
        ) or await self._require_command(command.id)
        await self._repository.acknowledge_latest_attempt(command.id, payload.accepted_at)
        operation = await self._repository.get_operation_for_command(command.id)
        if operation is not None and operation.status in {
            DistributedOperationStatus.DISPATCHED,
            DistributedOperationStatus.UNKNOWN,
            DistributedOperationStatus.RECONCILING,
        }:
            candidate = _operation_copy(
                operation,
                status=DistributedOperationStatus.ACCEPTED,
                last_agent_update_at=payload.accepted_at,
            )
            operation = (
                await self._repository.update_operation(
                    candidate,
                    expected_statuses={operation.status},
                )
                or operation
            )
        return command, operation

    async def rejected(
        self,
        agent_id: UUID,
        payload: CommandRejectedPayload,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        command = await self._require_owned_command(agent_id, payload.command_id)
        if command.status in {
            RemoteCommandStatus.FAILED,
            RemoteCommandStatus.CANCELLED,
            RemoteCommandStatus.EXPIRED,
        }:
            return command, await self._repository.get_operation_for_command(command.id)
        rejectable_statuses = {
            RemoteCommandStatus.DISPATCHED,
            RemoteCommandStatus.ACCEPTED,
            RemoteCommandStatus.RUNNING,
            RemoteCommandStatus.UNKNOWN,
        }
        if command.status not in rejectable_statuses:
            return command, await self._repository.get_operation_for_command(command.id)
        updated = _command_copy(
            command,
            status=RemoteCommandStatus.FAILED,
            completed_at=payload.rejected_at,
            error_code=payload.error_code,
            error_message=payload.error_message,
        )
        persisted = await self._repository.update_command(
            updated,
            expected_statuses=rejectable_statuses,
        )
        if persisted is None:
            command = await self._require_command(command.id)
            return command, await self._repository.get_operation_for_command(command.id)
        command = persisted
        operation = await self._finish_operation(
            command.id,
            status=DistributedOperationStatus.FAILED,
            occurred_at=payload.rejected_at,
            error_code=payload.error_code,
            error_message=payload.error_message,
        )
        return command, operation

    async def operation_event(
        self,
        agent_id: UUID,
        message_type: MessageType,
        payload: OperationEventPayload,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        command = await self._require_owned_command(agent_id, payload.command_id)
        operation = await self._repository.get_operation_for_command(command.id)
        if (
            operation is not None
            and operation.last_agent_update_at is not None
            and payload.occurred_at <= operation.last_agent_update_at
        ):
            return command, operation
        target_command, target_operation = _event_targets(message_type)
        if command.status != target_command:
            if target_command not in REMOTE_COMMAND_TRANSITIONS.get(
                command.status,
                frozenset(),
            ):
                return command, operation
            command_updates: dict[str, object] = {"status": target_command}
            if target_command is RemoteCommandStatus.RUNNING:
                command_updates["started_at"] = payload.occurred_at
                if command.acknowledged_at is None:
                    command_updates["acknowledged_at"] = payload.occurred_at
            elif target_command in {
                RemoteCommandStatus.SUCCEEDED,
                RemoteCommandStatus.FAILED,
                RemoteCommandStatus.CANCELLED,
            }:
                command_updates["completed_at"] = payload.occurred_at
                command_updates["error_code"] = payload.error_code
                command_updates["error_message"] = payload.error_message
            updated = _command_copy(command, **command_updates)
            command = await self._repository.update_command(
                updated,
                expected_statuses={command.status},
            ) or await self._require_command(command.id)

        if operation is None:
            return command, None
        same_running_state = (
            operation.status is DistributedOperationStatus.RUNNING
            and target_operation is DistributedOperationStatus.RUNNING
        )
        if not same_running_state and target_operation not in DISTRIBUTED_OPERATION_TRANSITIONS.get(
            operation.status, frozenset()
        ):
            return command, operation
        operation_updates: dict[str, object] = {
            "status": target_operation,
            "progress": payload.progress,
            "message": payload.message,
            "last_agent_update_at": payload.occurred_at,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
        }
        if payload.result is not None:
            operation_updates["result"] = payload.result
        if (
            target_operation is DistributedOperationStatus.RUNNING
            and operation.status is not DistributedOperationStatus.RUNNING
        ):
            operation_updates["started_at"] = payload.occurred_at
        elif target_operation in {
            DistributedOperationStatus.SUCCEEDED,
            DistributedOperationStatus.FAILED,
            DistributedOperationStatus.CANCELLED,
        }:
            operation_updates["completed_at"] = payload.occurred_at
        candidate = _operation_copy(operation, **operation_updates)
        persisted = await self._repository.update_operation(
            candidate,
            expected_statuses={operation.status},
        )
        operation = (
            persisted or await self._repository.get_operation_for_command(command.id) or operation
        )
        return command, operation

    async def mark_unknown(
        self,
        agent_id: UUID,
        *,
        observed_at: datetime | None = None,
    ) -> int:
        now = _as_utc(observed_at or self._clock())
        commands = await self._repository.list_commands(
            agent_id=agent_id,
            statuses={
                RemoteCommandStatus.DISPATCHED,
                RemoteCommandStatus.ACCEPTED,
                RemoteCommandStatus.RUNNING,
            },
        )
        changed = 0
        for command in commands:
            candidate = _command_copy(command, status=RemoteCommandStatus.UNKNOWN)
            persisted = await self._repository.update_command(
                candidate,
                expected_statuses={command.status},
            )
            if persisted is None:
                continue
            changed += 1
            operation = await self._repository.get_operation_for_command(command.id)
            if operation is None or operation.status not in {
                DistributedOperationStatus.DISPATCHED,
                DistributedOperationStatus.ACCEPTED,
                DistributedOperationStatus.RUNNING,
            }:
                continue
            unknown = _operation_copy(
                operation,
                status=DistributedOperationStatus.UNKNOWN,
                reconciliation_deadline=now + self._reconciliation_timeout,
            )
            await self._repository.update_operation(
                unknown,
                expected_statuses={operation.status},
            )
        return changed

    async def expire_due(self, *, limit: int = 500) -> int:
        now = _as_utc(self._clock())
        commands = await self._repository.list_commands(
            statuses={
                RemoteCommandStatus.CREATED,
                RemoteCommandStatus.QUEUED,
                RemoteCommandStatus.DISPATCHED,
                RemoteCommandStatus.UNKNOWN,
            },
            limit=limit,
        )
        expired = 0
        for command in commands:
            if command.expires_at > now:
                continue
            operation = await self._repository.get_operation_for_command(command.id)
            final_command, _ = await self._expire(command, operation, now)
            if final_command.status is RemoteCommandStatus.EXPIRED:
                expired += 1
        return expired

    async def _expire(
        self,
        command: RemoteCommand,
        operation: DistributedOperation | None,
        now: datetime,
    ) -> tuple[RemoteCommand, DistributedOperation | None]:
        candidate = _command_copy(
            command,
            status=RemoteCommandStatus.EXPIRED,
            completed_at=now,
        )
        command = await self._repository.update_command(
            candidate,
            expected_statuses={command.status},
        ) or await self._require_command(command.id)
        if operation is not None and operation.status in {
            DistributedOperationStatus.CREATED,
            DistributedOperationStatus.DISPATCHED,
            DistributedOperationStatus.UNKNOWN,
        }:
            failed = _operation_copy(
                operation,
                status=DistributedOperationStatus.FAILED,
                completed_at=now,
                error_code=RemoteCommandExpiredError.code,
                error_message="Remote command expired before completion",
            )
            operation = (
                await self._repository.update_operation(
                    failed,
                    expected_statuses={operation.status},
                )
                or operation
            )
        return command, operation

    async def _finish_operation(
        self,
        command_id: UUID,
        *,
        status: DistributedOperationStatus,
        occurred_at: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> DistributedOperation | None:
        operation = await self._repository.get_operation_for_command(command_id)
        if operation is None or operation.status == status:
            return operation
        if status not in DISTRIBUTED_OPERATION_TRANSITIONS.get(operation.status, frozenset()):
            return operation
        candidate = _operation_copy(
            operation,
            status=status,
            completed_at=occurred_at,
            last_agent_update_at=occurred_at,
            error_code=error_code,
            error_message=error_message,
        )
        return (
            await self._repository.update_operation(
                candidate,
                expected_statuses={operation.status},
            )
            or operation
        )

    async def _require_route(
        self,
        agent_id: UUID,
        bench_id: str,
    ) -> tuple[AgentRecord, GlobalBenchRecord]:
        agent = await self._directory.get_agent(agent_id)
        if agent is None:
            raise AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))
        bench = await self._directory.get_bench(bench_id)
        if bench is None or bench.agent_id != agent_id:
            raise BenchAgentMismatchError(
                "Bench is not owned by the requested Agent.",
                agent_id=str(agent_id),
                bench_id=bench_id,
            )
        return agent, bench

    @staticmethod
    def _validate_agent_accepts_new_work(
        agent: AgentRecord,
        *,
        allow_offline_queue: bool = False,
    ) -> None:
        if agent.status is AgentStatus.REVOKED:
            raise AgentRevokedError("Agent is revoked.", agent_id=str(agent.id))
        if agent.status in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
            raise AgentDrainingError("Agent is draining.", agent_id=str(agent.id))
        if agent.status is AgentStatus.DEGRADED:
            raise AgentDegradedError("Agent is degraded.", agent_id=str(agent.id))
        if agent.status is AgentStatus.INCOMPATIBLE:
            raise AgentIncompatibleError("Agent is incompatible.", agent_id=str(agent.id))
        if agent.status is not AgentStatus.ONLINE and not (
            allow_offline_queue and agent.status is AgentStatus.OFFLINE
        ):
            raise AgentOfflineError("Agent is offline.", agent_id=str(agent.id))

    @staticmethod
    def _validate_lease(
        *,
        agent_id: UUID,
        bench_id: str,
        command_type: RemoteCommandType,
        lease: ReservationLease | None,
        now: datetime,
    ) -> None:
        if command_type not in _MUTATING_COMMANDS:
            if lease is not None and (lease.agent_id != agent_id or lease.bench_id != bench_id):
                raise BenchAgentMismatchError("Reservation lease route does not match command.")
            return
        if lease is None:
            raise ValueError("Mutating remote command requires a reservation lease")
        if lease.agent_id != agent_id or lease.bench_id != bench_id:
            raise BenchAgentMismatchError("Reservation lease route does not match command.")
        if not lease.is_valid_at(now):
            raise RemoteCommandExpiredError("Reservation lease is not currently valid.")

    @staticmethod
    def _validate_dispatch_lease(
        command: RemoteCommand,
        lease: ReservationLease | None,
        now: datetime,
    ) -> None:
        if command.reservation_id is None:
            if lease is not None:
                raise ValueError("Unreserved command cannot be dispatched with a lease")
            return
        if (
            lease is None
            or lease.reservation_id != command.reservation_id
            or lease.lease_version != command.lease_version
            or lease.agent_id != command.agent_id
            or lease.bench_id != command.bench_id
        ):
            raise ValueError("Dispatch reservation lease does not match persisted command")
        if not lease.is_valid_at(now):
            raise RemoteCommandExpiredError("Reservation lease expired before dispatch.")

    @staticmethod
    def _validate_idempotent_replay(
        existing: RemoteCommand,
        *,
        bench_id: str,
        command_type: RemoteCommandType,
        payload: Mapping[str, Any],
        lease: ReservationLease | None,
    ) -> None:
        expected_reservation = lease.reservation_id if lease is not None else None
        expected_version = lease.lease_version if lease is not None else None
        if (
            existing.bench_id != bench_id
            or existing.command_type is not command_type
            or existing.payload != dict(payload)
            or existing.reservation_id != expected_reservation
            or existing.lease_version != expected_version
        ):
            raise RemoteCommandDuplicateError(
                "Remote command idempotency key was reused with different content.",
                command_id=str(existing.id),
                idempotency_key=existing.idempotency_key,
            )

    async def _require_command(self, command_id: UUID) -> RemoteCommand:
        command = await self._repository.get_command(command_id)
        if command is None:
            raise RemoteCommandNotFoundError(
                "Remote command does not exist.",
                command_id=str(command_id),
            )
        return command

    async def _require_owned_command(self, agent_id: UUID, command_id: UUID) -> RemoteCommand:
        command = await self._require_command(command_id)
        if command.agent_id != agent_id:
            raise RemoteCommandNotFoundError(
                "Remote command does not belong to the authenticated Agent.",
                command_id=str(command_id),
                agent_id=str(agent_id),
            )
        return command


def _command_copy(command: RemoteCommand, **updates: object) -> RemoteCommand:
    return RemoteCommand.model_validate({**command.model_dump(), **updates})


def _operation_copy(operation: DistributedOperation, **updates: object) -> DistributedOperation:
    return DistributedOperation.model_validate({**operation.model_dump(), **updates})


def _validate_durable_artifact_payload(
    command_type: RemoteCommandType,
    payload: Mapping[str, Any],
) -> None:
    """Reject delivery-attempt credentials before a command can reach persistence."""

    descriptors: list[object] = []
    if command_type is RemoteCommandType.RUN_WORKFLOW:
        raw = payload.get("artifact_transfers", [])
        if not isinstance(raw, list):
            raise ValueError("Workflow artifact_transfers must be a list")
        descriptors.extend(raw)
    elif command_type is RemoteCommandType.FLASH and "artifact" in payload:
        descriptors.append(payload["artifact"])
    for descriptor in descriptors:
        if not isinstance(descriptor, Mapping):
            raise ValueError("Durable artifact descriptor must be an object")
        transient = _TRANSIENT_ARTIFACT_FIELDS.intersection(descriptor)
        if transient:
            raise ValueError(
                "Artifact transfer capabilities are transient and cannot be persisted: "
                + ", ".join(sorted(transient))
            )


def _event_targets(
    message_type: MessageType,
) -> tuple[RemoteCommandStatus, DistributedOperationStatus]:
    targets = {
        MessageType.OPERATION_STARTED: (
            RemoteCommandStatus.RUNNING,
            DistributedOperationStatus.RUNNING,
        ),
        MessageType.OPERATION_PROGRESS: (
            RemoteCommandStatus.RUNNING,
            DistributedOperationStatus.RUNNING,
        ),
        MessageType.OPERATION_SUCCEEDED: (
            RemoteCommandStatus.SUCCEEDED,
            DistributedOperationStatus.SUCCEEDED,
        ),
        MessageType.OPERATION_FAILED: (
            RemoteCommandStatus.FAILED,
            DistributedOperationStatus.FAILED,
        ),
        MessageType.OPERATION_CANCELLED: (
            RemoteCommandStatus.CANCELLED,
            DistributedOperationStatus.CANCELLED,
        ),
    }
    try:
        return targets[message_type]
    except KeyError as exc:
        raise ValueError(f"{message_type.value} is not an operation event") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Distributed command timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
