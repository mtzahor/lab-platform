from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol, TypeAlias
from uuid import UUID

from lab_platform.core.errors import PlatformError
from lab_platform.models import ReservationLease


class ReservationLeaseInvalidError(PlatformError):
    code = "RESERVATION_LEASE_INVALID"


class ReservationLeaseExpiredError(PlatformError):
    code = "RESERVATION_LEASE_EXPIRED"


class ReservationLeaseVersionMismatchError(PlatformError):
    code = "RESERVATION_LEASE_VERSION_MISMATCH"


@dataclass(frozen=True, slots=True)
class ReservationLeaseTombstone:
    """The highest release observed for a bench, retained after the active lease is gone."""

    reservation_id: UUID
    agent_id: UUID
    bench_id: str
    lease_version: int
    released_at: datetime

    def __post_init__(self) -> None:
        if not self.bench_id.strip():
            raise ValueError("bench_id must be non-empty")
        if self.lease_version < 1:
            raise ValueError("lease_version must be at least one")
        object.__setattr__(
            self,
            "released_at",
            _as_utc(self.released_at, field="Reservation lease release timestamp"),
        )


StoredReservationLease: TypeAlias = ReservationLease | ReservationLeaseTombstone
Clock: TypeAlias = Callable[[], datetime]


class ReservationLeaseStore(Protocol):
    """Agent-local durable boundary for versioned reservation leases."""

    @property
    def agent_id(self) -> UUID: ...

    async def apply(
        self,
        lease: ReservationLease,
        *,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> StoredReservationLease: ...

    async def release(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        released_at: datetime,
    ) -> ReservationLeaseTombstone: ...

    async def validate(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> ReservationLease: ...

    async def get(self, bench_id: str) -> StoredReservationLease | None: ...

    async def list(self) -> list[StoredReservationLease]: ...


class InMemoryReservationLeaseStore:
    """Thread-safe reference store with release tombstones and strict version ordering.

    Methods are asynchronous to match a future SQLite adapter, but never await while the
    re-entrant lock is held. The same instance can therefore safely be used by concurrent tasks
    and by callers running event loops in different threads.
    """

    def __init__(self, agent_id: UUID, *, clock: Clock | None = None) -> None:
        self._agent_id = agent_id
        self._clock = clock or _utc_now
        self._records: dict[str, StoredReservationLease] = {}
        self._lock = RLock()

    @property
    def agent_id(self) -> UUID:
        return self._agent_id

    async def apply(
        self,
        lease: ReservationLease,
        *,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> StoredReservationLease:
        self._require_agent(lease.agent_id)
        observed = _as_utc(observed_at or self._clock(), field="Lease observation timestamp")
        skew = _clock_skew(maximum_clock_skew_seconds)

        if lease.released_at is None:
            _require_time_valid(lease, observed, skew)

        incoming: StoredReservationLease = (
            lease
            if lease.released_at is None
            else ReservationLeaseTombstone(
                reservation_id=lease.reservation_id,
                agent_id=lease.agent_id,
                bench_id=lease.bench_id,
                lease_version=lease.lease_version,
                released_at=lease.released_at,
            )
        )
        with self._lock:
            previous = self._records.get(lease.bench_id)
            if previous is not None:
                replay = _resolve_version(previous, incoming)
                if replay is not None:
                    return replay
            self._records[lease.bench_id] = incoming
            return incoming

    async def release(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        released_at: datetime,
    ) -> ReservationLeaseTombstone:
        self._require_agent(agent_id)
        tombstone = ReservationLeaseTombstone(
            reservation_id=reservation_id,
            agent_id=agent_id,
            bench_id=bench_id,
            lease_version=lease_version,
            released_at=released_at,
        )
        with self._lock:
            previous = self._records.get(bench_id)
            if previous is not None:
                replay = _resolve_version(previous, tombstone)
                if replay is not None and isinstance(replay, ReservationLeaseTombstone):
                    return replay
                # An equal-version release supersedes the active lease.
            self._records[bench_id] = tombstone
            return tombstone

    async def validate(
        self,
        *,
        agent_id: UUID,
        reservation_id: UUID,
        bench_id: str,
        lease_version: int,
        observed_at: datetime | None = None,
        maximum_clock_skew_seconds: int = 0,
    ) -> ReservationLease:
        self._require_agent(agent_id)
        if lease_version < 1:
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease version must be at least one.",
                received_lease_version=lease_version,
            )
        with self._lock:
            record = self._records.get(bench_id)
        if record is None:
            raise ReservationLeaseInvalidError(
                "No reservation lease is stored for this bench.",
                bench_id=bench_id,
                reservation_id=str(reservation_id),
            )
        if record.agent_id != agent_id or record.bench_id != bench_id:
            raise ReservationLeaseInvalidError(
                "Reservation lease Agent or bench identity does not match the command.",
                bench_id=bench_id,
                agent_id=str(agent_id),
            )
        if record.reservation_id != reservation_id:
            raise ReservationLeaseInvalidError(
                "Reservation lease does not belong to the command reservation.",
                bench_id=bench_id,
                reservation_id=str(reservation_id),
            )
        if record.lease_version != lease_version:
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease version does not match the latest stored version.",
                bench_id=bench_id,
                expected_lease_version=record.lease_version,
                received_lease_version=lease_version,
            )
        if isinstance(record, ReservationLeaseTombstone):
            raise ReservationLeaseInvalidError(
                "Reservation lease has been released.",
                bench_id=bench_id,
                reservation_id=str(reservation_id),
                lease_version=lease_version,
            )

        observed = _as_utc(observed_at or self._clock(), field="Lease validation timestamp")
        _require_time_valid(record, observed, _clock_skew(maximum_clock_skew_seconds))
        return record

    async def get(self, bench_id: str) -> StoredReservationLease | None:
        with self._lock:
            return self._records.get(bench_id)

    async def list(self) -> list[StoredReservationLease]:
        with self._lock:
            return [self._records[bench_id] for bench_id in sorted(self._records)]

    def _require_agent(self, agent_id: UUID) -> None:
        if agent_id != self._agent_id:
            raise ReservationLeaseInvalidError(
                "Reservation lease was issued to a different Agent.",
                expected_agent_id=str(self._agent_id),
                received_agent_id=str(agent_id),
            )


def _resolve_version(
    previous: StoredReservationLease,
    incoming: StoredReservationLease,
) -> StoredReservationLease | None:
    if incoming.lease_version < previous.lease_version:
        raise ReservationLeaseVersionMismatchError(
            "Reservation lease version is older than the latest stored version.",
            bench_id=incoming.bench_id,
            stored_lease_version=previous.lease_version,
            received_lease_version=incoming.lease_version,
        )
    if incoming.lease_version > previous.lease_version:
        return None
    if (
        previous.agent_id != incoming.agent_id
        or previous.bench_id != incoming.bench_id
        or previous.reservation_id != incoming.reservation_id
    ):
        raise ReservationLeaseInvalidError(
            "Reservation lease version was reused for a different identity.",
            bench_id=incoming.bench_id,
            lease_version=incoming.lease_version,
        )
    if isinstance(previous, ReservationLeaseTombstone):
        if isinstance(incoming, ReservationLeaseTombstone):
            return previous
        raise ReservationLeaseVersionMismatchError(
            "A released reservation lease cannot be reactivated at the same version.",
            bench_id=incoming.bench_id,
            lease_version=incoming.lease_version,
        )
    if isinstance(incoming, ReservationLeaseTombstone):
        return None
    if previous == incoming:
        return previous
    raise ReservationLeaseVersionMismatchError(
        "Reservation lease version was reused with different content.",
        bench_id=incoming.bench_id,
        lease_version=incoming.lease_version,
    )


def _require_time_valid(lease: ReservationLease, observed: datetime, skew: timedelta) -> None:
    if observed < lease.valid_from - skew:
        raise ReservationLeaseInvalidError(
            "Reservation lease is not valid yet.",
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            valid_from=lease.valid_from.isoformat(),
            observed_at=observed.isoformat(),
        )
    if observed > lease.valid_until + skew:
        raise ReservationLeaseExpiredError(
            "Reservation lease has expired.",
            bench_id=lease.bench_id,
            lease_version=lease.lease_version,
            valid_until=lease.valid_until.isoformat(),
            observed_at=observed.isoformat(),
        )


def _clock_skew(maximum_clock_skew_seconds: int) -> timedelta:
    if isinstance(maximum_clock_skew_seconds, bool) or maximum_clock_skew_seconds < 0:
        raise ValueError("maximum_clock_skew_seconds must be a non-negative integer")
    return timedelta(seconds=maximum_clock_skew_seconds)


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
