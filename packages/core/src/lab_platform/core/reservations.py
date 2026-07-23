from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from lab_platform.core.clock import Clock, UtcClock, as_utc
from lab_platform.core.errors import (
    BenchAlreadyReservedError,
    BenchNotReservedError,
    BenchOfflineError,
    BenchOperationInProgressError,
    QueueDisabledError,
    QueueEntryNotFoundError,
    QueueOwnerMismatchError,
    ReservationAlreadyExpiredError,
    ReservationExtensionConflictError,
    ReservationMaxDurationExceededError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
    ReservationTimeConflictError,
)
from lab_platform.core.reservation_ports import (
    BenchAvailability,
    OperationLockRepository,
    QueueRepository,
    ReservationEventRepository,
    TimedReservationRepository,
)
from lab_platform.models import (
    EventRecord,
    QueueEntry,
    QueueEntryStatus,
    Reservation,
    ReservationSource,
    ReservationStatus,
)


class ReservationService:
    """Owner-scoped timed reservation and queue application service."""

    def __init__(
        self,
        reservations: TimedReservationRepository,
        queues: QueueRepository,
        events: ReservationEventRepository,
        *,
        clock: Clock | None = None,
        availability: BenchAvailability | None = None,
        operation_locks: OperationLockRepository | None = None,
        default_duration_seconds: int = 30 * 60,
        maximum_duration_seconds: int = 4 * 60 * 60,
        queue_enabled: bool = True,
    ) -> None:
        if default_duration_seconds <= 0:
            raise ValueError("default_duration_seconds must be positive")
        if maximum_duration_seconds < default_duration_seconds:
            raise ValueError("maximum_duration_seconds cannot be shorter than the default")
        self._reservations = reservations
        self._queues = queues
        self._events = events
        self._clock = clock or UtcClock()
        self._availability = availability
        self._operation_locks = operation_locks
        self._default_duration_seconds = default_duration_seconds
        self._maximum_duration_seconds = maximum_duration_seconds
        self._queue_enabled = queue_enabled

    async def create(
        self,
        bench_id: str,
        owner: str,
        *,
        duration_seconds: int | None = None,
        starts_at: datetime | None = None,
        queue_if_busy: bool = False,
        idempotency_key: str | None = None,
        source: ReservationSource = ReservationSource.API,
        metadata: dict[str, str] | None = None,
        _event_type_override: str | None = None,
    ) -> Reservation | QueueEntry:
        duration = self._default_duration_seconds if duration_seconds is None else duration_seconds
        self._validate_duration(duration)
        self._validate_identity(bench_id, owner)
        now = self._clock.now()
        start = now if starts_at is None else as_utc(starts_at)
        if start < now:
            raise ReservationTimeConflictError(
                "A reservation cannot start in the past.", bench_id=bench_id
            )
        if idempotency_key:
            existing = await self._reservations.get_by_idempotency_key(bench_id, idempotency_key)
            if existing is not None:
                return existing
            if queue_if_busy:
                queued = await self._queues.get_by_idempotency_key(bench_id, idempotency_key)
                if queued is not None:
                    return queued

        immediate = start == now
        if immediate and not self._is_online(bench_id):
            if queue_if_busy:
                return await self.enqueue(
                    bench_id,
                    owner,
                    duration_seconds=duration,
                    idempotency_key=idempotency_key,
                )
            raise BenchOfflineError(f"Bench {bench_id} is offline.", bench_id=bench_id)

        end = start + timedelta(seconds=duration)
        conflict = await self._reservations.find_conflict(bench_id, start, end)
        if conflict is not None:
            if immediate and queue_if_busy:
                return await self.enqueue(
                    bench_id,
                    owner,
                    duration_seconds=duration,
                    idempotency_key=idempotency_key,
                )
            if immediate and conflict.status in {
                ReservationStatus.ACTIVE,
                ReservationStatus.EXPIRED_PENDING_OPERATION,
            }:
                raise BenchAlreadyReservedError(
                    f"Bench {bench_id} already has an active reservation.",
                    bench_id=bench_id,
                )
            raise ReservationTimeConflictError(
                f"Reservation overlaps {conflict.id}.",
                bench_id=bench_id,
                conflicting_reservation_id=str(conflict.id),
            )

        if immediate and self._operation_locks is not None:
            lock = await self._operation_locks.get(bench_id)
            if lock is not None:
                if queue_if_busy:
                    return await self.enqueue(
                        bench_id,
                        owner,
                        duration_seconds=duration,
                        idempotency_key=idempotency_key,
                    )
                raise BenchOperationInProgressError(
                    f"Bench {bench_id} has a mutating operation in progress.",
                    bench_id=bench_id,
                    operation_id=str(lock.operation_id),
                )

        status = ReservationStatus.ACTIVE if immediate else ReservationStatus.SCHEDULED
        reservation = Reservation(
            id=uuid4(),
            bench_id=bench_id,
            owner=owner,
            created_at=now,
            requested_at=now,
            starts_at=start,
            ends_at=end,
            activated_at=now if immediate else None,
            status=status,
            source=source,
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )
        try:
            created = await self._reservations.create(reservation)
        except (ReservationTimeConflictError, BenchOperationInProgressError):
            if immediate and queue_if_busy:
                return await self.enqueue(
                    bench_id,
                    owner,
                    duration_seconds=duration,
                    idempotency_key=idempotency_key,
                )
            raise
        event_type = _event_type_override or (
            "RESERVATION_ACTIVATED"
            if status is ReservationStatus.ACTIVE
            else "RESERVATION_SCHEDULED"
        )
        await self._emit(created, event_type, actor=owner)
        return created

    async def reserve(self, bench_id: str, owner: str) -> Reservation:
        """Phase 1 immediate-reservation alias with same-owner idempotency."""

        existing = await self._reservations.get_active(bench_id)
        if existing is not None:
            if existing.owner == owner and existing.status is ReservationStatus.ACTIVE:
                return existing
            raise BenchAlreadyReservedError(
                f"Bench {bench_id} is reserved by another owner.", bench_id=bench_id
            )
        created = await self.create(bench_id, owner, _event_type_override="BENCH_RESERVED")
        if isinstance(created, QueueEntry):
            raise AssertionError("Immediate reservation unexpectedly entered the queue")
        return created

    async def schedule(self, *args: object, **kwargs: object) -> Reservation | QueueEntry:
        return await self.create(*args, **kwargs)  # type: ignore[arg-type]

    async def enqueue(
        self,
        bench_id: str,
        owner: str,
        *,
        duration_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> QueueEntry:
        if not self._queue_enabled:
            raise QueueDisabledError("Reservation queueing is disabled.")
        duration = self._default_duration_seconds if duration_seconds is None else duration_seconds
        self._validate_duration(duration)
        self._validate_identity(bench_id, owner)
        if idempotency_key:
            existing = await self._queues.get_by_idempotency_key(bench_id, idempotency_key)
            if existing is not None:
                return existing
        entry = QueueEntry(
            bench_id=bench_id,
            owner=owner,
            requested_duration_seconds=duration,
            created_at=self._clock.now(),
            idempotency_key=idempotency_key,
        )
        created = await self._queues.create(entry)
        await self._events.create(
            EventRecord(
                timestamp=self._clock.now(),
                type="QUEUE_ENTRY_CREATED",
                source="platform",
                bench_id=bench_id,
                actor=owner,
                payload={"queue_entry_id": str(created.id)},
                deduplication_key=f"queue:{created.id}:created",
            )
        )
        return created

    async def cancel_queue_entry(self, entry_id: UUID, owner: str) -> QueueEntry:
        entry = await self._queues.get(entry_id)
        if entry is None:
            raise QueueEntryNotFoundError(f"Queue entry {entry_id} was not found.")
        if entry.owner != owner:
            raise QueueOwnerMismatchError(
                "Only the queue entry owner may cancel it.", queue_entry_id=str(entry_id)
            )
        if entry.status is QueueEntryStatus.CANCELLED:
            return entry
        cancelled = await self._queues.cancel(entry_id, owner, self._clock.now())
        await self._events.create(
            EventRecord(
                timestamp=self._clock.now(),
                type="QUEUE_ENTRY_CANCELLED",
                source="platform",
                bench_id=entry.bench_id,
                actor=owner,
                payload={"queue_entry_id": str(entry.id)},
                deduplication_key=f"queue:{entry.id}:cancelled",
            )
        )
        return cancelled

    async def get(self, reservation_id: UUID) -> Reservation:
        reservation = await self._reservations.get(reservation_id)
        if reservation is None:
            raise ReservationNotFoundError(f"Reservation {reservation_id} was not found.")
        return reservation

    async def get_active(self, bench_id: str) -> Reservation | None:
        return await self._reservations.get_active(bench_id)

    async def get_reservation(self, bench_id: str) -> Reservation | None:
        """Phase 1 active-reservation lookup alias."""

        return await self.get_active(bench_id)

    async def require_active(self, bench_id: str, owner: str) -> UUID:
        reservation = await self._reservations.get_active(bench_id)
        if (
            reservation is None
            or reservation.status is not ReservationStatus.ACTIVE
            or reservation.ends_at is not None
            and reservation.ends_at <= self._clock.now()
        ):
            raise ReservationNotActiveError(
                f"Bench {bench_id} requires an active reservation.", bench_id=bench_id
            )
        if reservation.owner != owner:
            raise ReservationOwnerMismatchError(
                "Only the reservation owner may operate this bench.", bench_id=bench_id
            )
        return reservation.id

    async def require_owner(self, bench_id: str, owner: str) -> UUID:
        """Phase 1 guard name retained for existing BenchService wiring."""

        try:
            return await self.require_active(bench_id, owner)
        except ReservationNotActiveError as exc:
            raise BenchNotReservedError(
                f"Bench {bench_id} must be reserved before it can be controlled.",
                bench_id=bench_id,
            ) from exc

    async def release(self, reservation_id: UUID | str, owner: str) -> Reservation | None:
        legacy_release = False
        if isinstance(reservation_id, str):
            legacy_release = True
            active = await self._reservations.get_active(reservation_id)
            if active is None:
                return None
            reservation_id = active.id
        reservation = await self.get(reservation_id)
        self._require_owner(reservation, owner)
        if reservation.status is ReservationStatus.RELEASED:
            return reservation
        if reservation.status is not ReservationStatus.ACTIVE:
            raise ReservationNotActiveError(
                f"Reservation {reservation_id} is not active.",
                reservation_id=str(reservation_id),
            )
        if self._operation_locks is not None:
            lock = await self._operation_locks.get(reservation.bench_id)
            if lock is not None:
                raise BenchOperationInProgressError(
                    "The reservation cannot be released during a mutating operation.",
                    bench_id=reservation.bench_id,
                    operation_id=str(lock.operation_id),
                )
        released = reservation.model_copy(
            update={
                "status": ReservationStatus.RELEASED,
                "released_at": self._clock.now(),
                "release_pending": False,
            }
        )
        updated = await self._reservations.update(
            released,
            expected_status=ReservationStatus.ACTIVE,
            require_unlocked=True,
        )
        await self._emit(
            updated,
            "BENCH_RELEASED" if legacy_release else "RESERVATION_RELEASED",
            actor=owner,
        )
        return updated

    async def cancel(self, reservation_id: UUID, owner: str) -> Reservation:
        reservation = await self.get(reservation_id)
        self._require_owner(reservation, owner)
        if reservation.status is ReservationStatus.CANCELLED:
            return reservation
        if reservation.status not in {ReservationStatus.SCHEDULED, ReservationStatus.ACTIVE}:
            raise ReservationNotActiveError(
                f"Reservation {reservation_id} cannot be cancelled from {reservation.status}."
            )
        if reservation.status is ReservationStatus.ACTIVE and self._operation_locks is not None:
            lock = await self._operation_locks.get(reservation.bench_id)
            if lock is not None:
                raise BenchOperationInProgressError(
                    "The reservation cannot be cancelled during a mutating operation.",
                    bench_id=reservation.bench_id,
                    operation_id=str(lock.operation_id),
                )
        cancelled = reservation.model_copy(
            update={
                "status": ReservationStatus.CANCELLED,
                "released_at": self._clock.now(),
            }
        )
        updated = await self._reservations.update(
            cancelled,
            expected_status=reservation.status,
            require_unlocked=reservation.status is ReservationStatus.ACTIVE,
        )
        await self._emit(updated, "RESERVATION_CANCELLED", actor=owner)
        return updated

    async def extend(self, reservation_id: UUID, owner: str, duration_seconds: int) -> Reservation:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        reservation = await self.get(reservation_id)
        self._require_owner(reservation, owner)
        if reservation.status is not ReservationStatus.ACTIVE or reservation.ends_at is None:
            raise ReservationNotActiveError(f"Reservation {reservation_id} is not active.")
        if reservation.ends_at <= self._clock.now():
            raise ReservationAlreadyExpiredError(
                f"Reservation {reservation_id} has already expired.",
                reservation_id=str(reservation_id),
            )
        starts_at = reservation.starts_at or reservation.requested_at or reservation.created_at
        new_end = reservation.ends_at + timedelta(seconds=duration_seconds)
        if (new_end - starts_at).total_seconds() > self._maximum_duration_seconds:
            raise ReservationMaxDurationExceededError(
                "The extension exceeds the configured maximum reservation duration.",
                maximum_duration_seconds=self._maximum_duration_seconds,
            )
        conflict = await self._reservations.find_conflict(
            reservation.bench_id,
            reservation.ends_at,
            new_end,
            exclude_id=reservation.id,
        )
        if conflict is not None:
            raise ReservationExtensionConflictError(
                f"The extension overlaps reservation {conflict.id}.",
                conflicting_reservation_id=str(conflict.id),
            )
        extended = reservation.model_copy(update={"ends_at": new_end})
        try:
            updated = await self._reservations.update(
                extended,
                expected_status=ReservationStatus.ACTIVE,
                expected_ends_at=reservation.ends_at,
            )
        except (ReservationTimeConflictError, ReservationNotActiveError) as exc:
            raise ReservationExtensionConflictError(str(exc)) from exc
        await self._emit(
            updated,
            "RESERVATION_EXTENDED",
            actor=owner,
            payload={"duration_seconds": duration_seconds, "ends_at": new_end.isoformat()},
        )
        return updated

    async def list(self, **filters: object) -> list[Reservation]:
        return await self._reservations.list(**filters)  # type: ignore[arg-type]

    def _is_online(self, bench_id: str) -> bool:
        return self._availability is None or self._availability.is_online(bench_id)

    def _validate_duration(self, duration_seconds: int) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if duration_seconds > self._maximum_duration_seconds:
            raise ReservationMaxDurationExceededError(
                "The requested duration exceeds the configured maximum.",
                maximum_duration_seconds=self._maximum_duration_seconds,
            )

    @staticmethod
    def _validate_identity(bench_id: str, owner: str) -> None:
        if not bench_id.strip() or not owner.strip():
            raise ValueError("bench_id and owner must be non-empty")

    @staticmethod
    def _require_owner(reservation: Reservation, owner: str) -> None:
        if reservation.owner != owner:
            raise ReservationOwnerMismatchError(
                "Only the reservation owner may change it.",
                reservation_id=str(reservation.id),
            )

    async def _emit(
        self,
        reservation: Reservation,
        event_type: str,
        *,
        actor: str | None,
        payload: dict[str, object] | None = None,
    ) -> None:
        event_payload: dict[str, object] = {
            "reservation_id": str(reservation.id),
            "status": reservation.status.value,
        }
        event_payload.update(payload or {})
        await self._events.create(
            EventRecord(
                timestamp=self._clock.now(),
                type=event_type,
                source="platform",
                bench_id=reservation.bench_id,
                reservation_id=reservation.id,
                actor=actor,
                payload=event_payload,
                deduplication_key=f"reservation:{reservation.id}:{event_type}",
            )
        )
