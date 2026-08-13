from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn, Protocol
from uuid import UUID, uuid4

from lab_platform.control_plane_core.errors import (
    AgentDegradedError,
    AgentDrainingError,
    AgentIncompatibleError,
    AgentNotFoundError,
    AgentOfflineError,
    AgentRevokedError,
    BenchAgentMismatchError,
    ReservationLeaseExpiredError,
    ReservationLeaseInvalidError,
    ReservationLeaseVersionMismatchError,
)
from lab_platform.core.authorisation import AuthorisationService
from lab_platform.core.errors import (
    AuthenticationRequiredError,
    BenchAlreadyReservedError,
    ReservationNotActiveError,
    ReservationNotFoundError,
    ReservationOwnerMismatchError,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    AuthenticationContext,
    AuthorisationResource,
    GlobalBenchRecord,
    GlobalBenchStatus,
    Reservation,
    ReservationLease,
    ReservationOwner,
    ReservationSource,
    ReservationStatus,
    ResourceType,
)


class ReservationLeaseState(StrEnum):
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    RENEWING = "RENEWING"
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


_TERMINAL_LEASE_STATES = frozenset(
    {
        ReservationLeaseState.RELEASED,
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.REVOKED,
    }
)


@dataclass(frozen=True, slots=True)
class CoordinatedReservationLease:
    """Global reservation and its current Agent-side fencing lease."""

    reservation: Reservation
    lease: ReservationLease
    state: ReservationLeaseState
    revision: int
    unknown_since: datetime | None = None
    reconciliation_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("Reservation lease revision must be positive")
        if self.reservation.id != self.lease.reservation_id:
            raise ValueError("Reservation and lease IDs must match")
        if self.reservation.bench_id != self.lease.bench_id:
            raise ValueError("Reservation and lease bench IDs must match")
        if self.reservation.owner != self.lease.owner:
            raise ValueError("Reservation and lease owners must match")
        _validate_coordination_state(self)

    @property
    def accepts_new_work(self) -> bool:
        return self.state is ReservationLeaseState.ACTIVE


@dataclass(frozen=True, slots=True)
class ReservationGrantRequest:
    reservation: Reservation
    agent_id: UUID
    lease_valid_until: datetime


@dataclass(frozen=True, slots=True)
class LeaseApplicationReceipt:
    reservation_id: UUID
    agent_id: UUID
    bench_id: str
    lease_version: int
    confirmed_at: datetime


class LeaseWriteDisposition(StrEnum):
    APPLIED = "APPLIED"
    REPLAY = "REPLAY"


@dataclass(frozen=True, slots=True)
class LeaseWriteResult:
    record: CoordinatedReservationLease
    disposition: LeaseWriteDisposition


class DistributedReservationDirectory(Protocol):
    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...

    async def get_bench(self, bench_id: str) -> GlobalBenchRecord | None: ...


class ReservationLeaseSynchronizer(Protocol):
    """Agent transport that semantically confirms lease application/release."""

    async def apply_lease(self, lease: ReservationLease) -> LeaseApplicationReceipt: ...

    async def release_lease(self, lease: ReservationLease) -> None: ...


class CentralReservationLeaseRepository(Protocol):
    """Transactional persistence boundary for global ownership and lease fencing.

    ``grant_if_eligible`` returns an ACTIVATING record and must check Agent status,
    bench status and ownership, the
    active-bench uniqueness constraint, idempotency, and the next bench-wide lease
    version in one transaction. ``replace_if_current`` must apply its optional
    eligibility predicates in the same transaction as the revision compare-and-set.
    """

    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease | None: ...

    async def get_current_for_bench(
        self,
        bench_id: str,
    ) -> CoordinatedReservationLease | None: ...

    async def get_mutation_result(
        self,
        mutation_key: str,
        *,
        organisation_id: UUID,
        request_fingerprint: str,
    ) -> LeaseWriteResult | None: ...

    async def grant_if_eligible(
        self,
        request: ReservationGrantRequest,
        *,
        mutation_key: str,
        request_fingerprint: str,
        expected_agent_status: AgentStatus,
        expected_bench_status: GlobalBenchStatus,
    ) -> LeaseWriteResult | None: ...

    async def replace_if_current(
        self,
        record: CoordinatedReservationLease,
        *,
        expected_revision: int,
        mutation_key: str,
        request_fingerprint: str,
        expected_agent_status: AgentStatus | None = None,
        expected_bench_status: GlobalBenchStatus | None = None,
    ) -> LeaseWriteResult | None: ...

    async def list(
        self,
        *,
        agent_id: UUID | None = None,
        states: Iterable[ReservationLeaseState] | None = None,
        limit: int = 10_000,
    ) -> list[CoordinatedReservationLease]: ...


class CentralReservationLeaseService:
    """Coordinate global reservations and versioned Agent leases safely."""

    def __init__(
        self,
        repository: CentralReservationLeaseRepository,
        directory: DistributedReservationDirectory,
        synchronizer: ReservationLeaseSynchronizer,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
        default_reservation_duration_seconds: int = 3_600,
        maximum_reservation_duration_seconds: int = 86_400,
        default_lease_ttl_seconds: int = 3_600,
        maximum_lease_ttl_seconds: int = 3_600,
        maximum_clock_skew_seconds: int = 30,
        offline_reservation_grace_seconds: int = 300,
    ) -> None:
        _require_positive_seconds(
            default_reservation_duration_seconds,
            field="default_reservation_duration_seconds",
        )
        _require_positive_seconds(
            maximum_reservation_duration_seconds,
            field="maximum_reservation_duration_seconds",
        )
        _require_positive_seconds(
            default_lease_ttl_seconds,
            field="default_lease_ttl_seconds",
        )
        _require_positive_seconds(
            maximum_lease_ttl_seconds,
            field="maximum_lease_ttl_seconds",
        )
        _require_nonnegative_seconds(
            offline_reservation_grace_seconds,
            field="offline_reservation_grace_seconds",
        )
        if maximum_reservation_duration_seconds < default_reservation_duration_seconds:
            raise ValueError("Maximum reservation duration cannot be shorter than default")
        if maximum_lease_ttl_seconds < default_lease_ttl_seconds:
            raise ValueError("Maximum lease TTL cannot be shorter than default")
        _require_nonnegative_seconds(
            maximum_clock_skew_seconds,
            field="maximum_clock_skew_seconds",
        )
        self._repository = repository
        self._directory = directory
        self._synchronizer = synchronizer
        self._clock = clock or _utc_now
        self._id_factory = id_factory or uuid4
        self._default_reservation_duration = default_reservation_duration_seconds
        self._maximum_reservation_duration = maximum_reservation_duration_seconds
        self._default_lease_ttl = default_lease_ttl_seconds
        self._maximum_lease_ttl = maximum_lease_ttl_seconds
        self._maximum_clock_skew = maximum_clock_skew_seconds
        self._offline_grace = offline_reservation_grace_seconds
        self._authorisation: AuthorisationService | None = None

    def set_authorisation_service(self, authorisation: AuthorisationService) -> None:
        """Enable identity enforcement after runtime dependency construction."""

        self._authorisation = authorisation

    async def grant(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        owner_principal: ReservationOwner | None = None,
        idempotency_key: str,
        reservation_duration_seconds: int | None = None,
        lease_ttl_seconds: int | None = None,
        source: ReservationSource = ReservationSource.API,
        metadata: Mapping[str, str] | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> CoordinatedReservationLease:
        normalized_owner = _require_text(owner, field="owner", maximum_length=200)
        mutation_key = _require_mutation_key(idempotency_key)
        duration = self._reservation_duration(reservation_duration_seconds)
        lease_ttl = self._lease_ttl(lease_ttl_seconds)
        normalized_metadata = dict(metadata or {})
        agent, bench = await self._trusted_route(agent_id, bench_id)
        await self._require_bench_authorisation(
            agent,
            bench,
            "benches:reserve",
            authentication_context=authentication_context,
            owner=normalized_owner,
            owner_principal=owner_principal,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        fingerprint = _request_fingerprint(
            "grant",
            {
                "agent_id": str(agent_id),
                "bench_id": bench_id,
                "owner": normalized_owner,
                "owner_principal": (
                    owner_principal.model_dump(mode="json") if owner_principal is not None else None
                ),
                "reservation_duration_seconds": duration,
                "lease_ttl_seconds": lease_ttl,
                "source": source.value,
                "metadata": normalized_metadata,
            },
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=bench.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            current = await self._repository.get(replay.record.reservation.id)
            effective = current or replay.record
            if effective.state is ReservationLeaseState.ACTIVATING:
                return await self._synchronize_pending(effective, operation_key=mutation_key)
            return effective

        now = self._now()
        ends_at = now + timedelta(seconds=duration)
        reservation = Reservation(
            id=self._id_factory(),
            organisation_id=bench.organisation_id,
            bench_id=bench_id,
            owner=normalized_owner,
            owner_principal_id=(
                owner_principal.principal_id if owner_principal is not None else None
            ),
            owner_principal_type=(
                owner_principal.principal_type.value if owner_principal is not None else None
            ),
            created_at=now,
            requested_at=now,
            starts_at=now,
            ends_at=ends_at,
            status=ReservationStatus.SCHEDULED,
            source=source,
            metadata=normalized_metadata,
            idempotency_key=mutation_key,
        )
        request = ReservationGrantRequest(
            reservation=reservation,
            agent_id=agent_id,
            lease_valid_until=min(ends_at, now + timedelta(seconds=lease_ttl)),
        )
        result = await self._repository.grant_if_eligible(
            request,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        if result is not None:
            return await self._synchronize_pending(
                result.record,
                operation_key=mutation_key,
            )
        await self._raise_grant_failure(agent_id, bench_id)

    async def renew(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        owner_principal: ReservationOwner | None = None,
        expected_lease_version: int,
        idempotency_key: str,
        lease_ttl_seconds: int | None = None,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> CoordinatedReservationLease:
        normalized_owner = _require_text(owner, field="owner", maximum_length=200)
        mutation_key = _require_mutation_key(idempotency_key)
        _require_positive_version(expected_lease_version)
        lease_ttl = self._lease_ttl(lease_ttl_seconds)
        current = await self._require_record(reservation_id)
        agent, bench = await self._trusted_route(
            current.lease.agent_id,
            current.lease.bench_id,
        )
        _validate_record_organisation(current, bench)
        await self._require_bench_authorisation(
            agent,
            bench,
            "benches:reserve",
            authentication_context=authentication_context,
            owner=normalized_owner,
            owner_principal=owner_principal,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        await self._require_reservation_owner(
            current,
            normalized_owner,
            owner_principal=owner_principal,
            authentication_context=authentication_context,
        )
        fingerprint = _request_fingerprint(
            "renew",
            {
                "reservation_id": str(reservation_id),
                "owner": normalized_owner,
                "owner_principal_id": (
                    str(owner_principal.principal_id) if owner_principal is not None else None
                ),
                "expected_lease_version": expected_lease_version,
                "lease_ttl_seconds": lease_ttl,
            },
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=current.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            latest = await self._repository.get(replay.record.reservation.id)
            effective = latest or replay.record
            if effective.state is ReservationLeaseState.RENEWING:
                return await self._synchronize_pending(effective, operation_key=mutation_key)
            return effective
        _require_version(current, expected_lease_version)
        if current.state is not ReservationLeaseState.ACTIVE:
            raise ReservationNotActiveError(
                "Only an active reservation lease can be renewed.",
                reservation_id=str(reservation_id),
                state=current.state.value,
            )
        now = self._now()
        ends_at = current.reservation.ends_at
        if ends_at is None or ends_at <= now:
            raise ReservationLeaseExpiredError(
                "Global reservation ended before its lease could be renewed.",
                reservation_id=str(reservation_id),
            )
        candidate = CoordinatedReservationLease(
            reservation=current.reservation,
            lease=ReservationLease(
                reservation_id=reservation_id,
                agent_id=current.lease.agent_id,
                bench_id=current.lease.bench_id,
                owner=current.lease.owner,
                valid_from=now,
                valid_until=min(ends_at, now + timedelta(seconds=lease_ttl)),
                lease_version=current.lease.lease_version + 1,
            ),
            state=ReservationLeaseState.RENEWING,
            revision=current.revision + 1,
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=current.revision,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        if result is not None:
            return await self._synchronize_pending(
                result.record,
                operation_key=mutation_key,
            )
        await self._raise_transition_failure(current, require_eligible=True)

    async def release(
        self,
        reservation_id: UUID,
        *,
        owner: str,
        owner_principal: ReservationOwner | None = None,
        expected_lease_version: int,
        idempotency_key: str,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> CoordinatedReservationLease:
        normalized_owner = _require_text(owner, field="owner", maximum_length=200)
        mutation_key = _require_mutation_key(idempotency_key)
        _require_positive_version(expected_lease_version)
        current = await self._require_record(reservation_id)
        agent, bench = await self._trusted_route(
            current.lease.agent_id,
            current.lease.bench_id,
        )
        _validate_record_organisation(current, bench)
        await self._require_bench_authorisation(
            agent,
            bench,
            "benches:reserve",
            authentication_context=authentication_context,
            owner=normalized_owner,
            owner_principal=owner_principal,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        await self._require_reservation_owner(
            current,
            normalized_owner,
            owner_principal=owner_principal,
            authentication_context=authentication_context,
        )
        fingerprint = _request_fingerprint(
            "release",
            {
                "reservation_id": str(reservation_id),
                "owner": normalized_owner,
                "owner_principal_id": (
                    str(owner_principal.principal_id) if owner_principal is not None else None
                ),
                "expected_lease_version": expected_lease_version,
            },
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=current.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay.record
        _require_version(current, expected_lease_version)
        if current.state is ReservationLeaseState.RELEASED:
            return current
        if current.state in _TERMINAL_LEASE_STATES:
            raise ReservationNotActiveError(
                "Reservation lease is already terminal.",
                reservation_id=str(reservation_id),
                state=current.state.value,
            )
        now = self._now()
        candidate = _terminal_record(
            current,
            state=ReservationLeaseState.RELEASED,
            reservation_status=ReservationStatus.RELEASED,
            occurred_at=now,
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=current.revision,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
        )
        if result is not None:
            await self._notify_release(result.record.lease)
            return result.record
        await self._raise_transition_failure(current)

    async def revoke(
        self,
        reservation_id: UUID,
        *,
        expected_lease_version: int,
        idempotency_key: str,
        authentication_context: AuthenticationContext | None = None,
        allow_legacy_authorisation: bool = False,
        allow_internal_authorisation: bool = False,
    ) -> CoordinatedReservationLease:
        mutation_key = _require_mutation_key(idempotency_key)
        _require_positive_version(expected_lease_version)
        current = await self._require_record(reservation_id)
        agent, bench = await self._trusted_route(
            current.lease.agent_id,
            current.lease.bench_id,
        )
        _validate_record_organisation(current, bench)
        await self._require_bench_authorisation(
            agent,
            bench,
            "benches:manage",
            authentication_context=authentication_context,
            owner=None,
            owner_principal=None,
            allow_legacy_authorisation=allow_legacy_authorisation,
            allow_internal_authorisation=allow_internal_authorisation,
        )
        fingerprint = _request_fingerprint(
            "revoke",
            {
                "reservation_id": str(reservation_id),
                "expected_lease_version": expected_lease_version,
            },
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=current.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay.record
        _require_version(current, expected_lease_version)
        if current.state is ReservationLeaseState.REVOKED:
            return current
        if current.state in _TERMINAL_LEASE_STATES:
            raise ReservationNotActiveError(
                "Reservation lease is already terminal.",
                reservation_id=str(reservation_id),
                state=current.state.value,
            )
        candidate = _terminal_record(
            current,
            state=ReservationLeaseState.REVOKED,
            reservation_status=ReservationStatus.CANCELLED,
            occurred_at=self._now(),
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=current.revision,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
        )
        if result is not None:
            await self._notify_release(result.record.lease)
            return result.record
        await self._raise_transition_failure(current)

    async def mark_agent_disconnected(
        self,
        agent_id: UUID,
        *,
        disconnect_id: UUID,
    ) -> tuple[CoordinatedReservationLease, ...]:
        now = self._now()
        active = await self._repository.list(
            agent_id=agent_id,
            states={
                ReservationLeaseState.ACTIVATING,
                ReservationLeaseState.ACTIVE,
                ReservationLeaseState.RENEWING,
            },
        )
        changed: list[CoordinatedReservationLease] = []
        for current in active:
            ends_at = current.reservation.ends_at
            deadline = now + timedelta(seconds=self._offline_grace)
            if ends_at is not None:
                deadline = min(deadline, ends_at)
            deadline = max(deadline, now)
            mutation_key = f"disconnect:{disconnect_id}:{current.reservation.id}"
            fingerprint = _request_fingerprint(
                "disconnect",
                {
                    "disconnect_id": str(disconnect_id),
                    "reservation_id": str(current.reservation.id),
                    "lease_version": current.lease.lease_version,
                },
            )
            replay = await self._repository.get_mutation_result(
                mutation_key,
                organisation_id=current.reservation.organisation_id,
                request_fingerprint=fingerprint,
            )
            if replay is not None:
                changed.append(replay.record)
                continue
            candidate = CoordinatedReservationLease(
                reservation=current.reservation,
                lease=current.lease,
                state=ReservationLeaseState.UNKNOWN,
                revision=current.revision + 1,
                unknown_since=now,
                reconciliation_deadline=deadline,
            )
            result = await self._repository.replace_if_current(
                candidate,
                expected_revision=current.revision,
                mutation_key=mutation_key,
                request_fingerprint=fingerprint,
            )
            if result is not None:
                changed.append(result.record)
        return tuple(changed)

    async def reconcile_after_reconnect(
        self,
        presented_lease: ReservationLease,
        *,
        idempotency_key: str,
    ) -> CoordinatedReservationLease:
        mutation_key = _require_mutation_key(idempotency_key)
        current = await self._require_record(presented_lease.reservation_id)
        fingerprint = _request_fingerprint(
            "reconcile",
            {"presented_lease": presented_lease.model_dump(mode="json")},
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=current.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay.record
        _require_version(current, presented_lease.lease_version)
        if current.state is not ReservationLeaseState.UNKNOWN:
            raise ReservationLeaseInvalidError(
                "Only an UNKNOWN lease can be restored by reconciliation.",
                reservation_id=str(presented_lease.reservation_id),
                state=current.state.value,
            )
        if current.lease != presented_lease:
            raise ReservationLeaseInvalidError(
                "Agent lease does not match current control-plane lease content.",
                reservation_id=str(presented_lease.reservation_id),
                lease_version=presented_lease.lease_version,
            )
        now = self._now()
        if presented_lease.released_at is not None or not presented_lease.is_valid_at(
            now,
            maximum_clock_skew_seconds=self._maximum_clock_skew,
        ):
            raise ReservationLeaseExpiredError(
                "Agent lease expired before reconciliation completed.",
                reservation_id=str(presented_lease.reservation_id),
                lease_version=presented_lease.lease_version,
            )
        candidate = CoordinatedReservationLease(
            reservation=_activated_reservation(current.reservation, now),
            lease=current.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=current.revision + 1,
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=current.revision,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        if result is not None:
            return result.record
        await self._raise_transition_failure(current, require_eligible=True)

    async def require_for_new_work(
        self,
        reservation_id: UUID,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        lease_version: int,
        agent_observed_at: datetime | None = None,
    ) -> ReservationLease:
        _require_positive_version(lease_version)
        current = await self._require_record(reservation_id)
        _require_owner(current, owner)
        _require_version(current, lease_version)
        if current.lease.agent_id != agent_id or current.lease.bench_id != bench_id:
            raise BenchAgentMismatchError(
                "Reservation lease route does not match the requested Agent bench.",
                reservation_id=str(reservation_id),
                expected_agent_id=str(current.lease.agent_id),
                received_agent_id=str(agent_id),
                expected_bench_id=current.lease.bench_id,
                received_bench_id=bench_id,
            )
        if current.state is not ReservationLeaseState.ACTIVE:
            raise ReservationLeaseInvalidError(
                "Reservation lease cannot authorize new work in its current state.",
                reservation_id=str(reservation_id),
                state=current.state.value,
            )
        agent = await self._directory.get_agent(agent_id)
        bench = await self._directory.get_bench(bench_id)
        _require_eligible_route(agent_id, bench_id, agent, bench)
        now = self._now()
        observed = (
            now
            if agent_observed_at is None
            else _as_utc(
                agent_observed_at,
                field="Agent lease observation timestamp",
            )
        )
        offset = abs((observed - now).total_seconds())
        if offset > self._maximum_clock_skew:
            raise AgentDegradedError(
                "Agent clock offset exceeds the accepted lease skew.",
                agent_id=str(agent_id),
                observed_clock_offset_seconds=offset,
                maximum_clock_skew_seconds=self._maximum_clock_skew,
            )
        if (
            current.lease.released_at is not None
            or now < current.lease.valid_from
            or now > current.lease.valid_until
            or not current.lease.is_valid_at(
                observed,
                maximum_clock_skew_seconds=self._maximum_clock_skew,
            )
        ):
            raise ReservationLeaseExpiredError(
                "Reservation lease is outside its bounded validity window.",
                reservation_id=str(reservation_id),
                lease_version=lease_version,
            )
        return current.lease

    async def expire_due(self, *, limit: int = 10_000) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = self._now()
        records = await self._repository.list(
            states={
                ReservationLeaseState.ACTIVATING,
                ReservationLeaseState.ACTIVE,
                ReservationLeaseState.RENEWING,
                ReservationLeaseState.UNKNOWN,
            },
            limit=limit,
        )
        expired = 0
        for current in records:
            ends_at = current.reservation.ends_at
            reservation_due = ends_at is not None and ends_at <= now
            lease_due = (
                current.state is not ReservationLeaseState.UNKNOWN
                and current.lease.valid_until + timedelta(seconds=self._maximum_clock_skew) < now
            )
            reconciliation_due = (
                current.state is ReservationLeaseState.UNKNOWN
                and current.reconciliation_deadline is not None
                and current.reconciliation_deadline <= now
            )
            if not reservation_due and not lease_due and not reconciliation_due:
                continue
            mutation_key = f"expire:{current.reservation.id}:{current.lease.lease_version}"
            fingerprint = _request_fingerprint(
                "expire",
                {
                    "reservation_id": str(current.reservation.id),
                    "lease_version": current.lease.lease_version,
                },
            )
            replay = await self._repository.get_mutation_result(
                mutation_key,
                organisation_id=current.reservation.organisation_id,
                request_fingerprint=fingerprint,
            )
            if replay is not None:
                continue
            candidate = _terminal_record(
                current,
                state=ReservationLeaseState.EXPIRED,
                reservation_status=ReservationStatus.EXPIRED,
                occurred_at=now,
            )
            result = await self._repository.replace_if_current(
                candidate,
                expected_revision=current.revision,
                mutation_key=mutation_key,
                request_fingerprint=fingerprint,
            )
            if result is not None and result.disposition is LeaseWriteDisposition.APPLIED:
                await self._notify_release(result.record.lease)
                expired += 1
        return expired

    async def get(self, reservation_id: UUID) -> CoordinatedReservationLease:
        return await self._require_record(reservation_id)

    async def _trusted_route(
        self,
        agent_id: UUID,
        bench_id: str,
    ) -> tuple[AgentRecord, GlobalBenchRecord]:
        agent = await self._directory.get_agent(agent_id)
        if agent is None:
            raise AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))
        bench = await self._directory.get_bench(bench_id)
        if bench is None or bench.id != bench_id or bench.agent_id != agent_id:
            raise BenchAgentMismatchError(
                "Global bench is not owned by the requested Agent.",
                agent_id=str(agent_id),
                bench_id=bench_id,
            )
        _validate_trusted_route_organisation(agent, bench)
        return agent, bench

    async def _require_bench_authorisation(
        self,
        agent: AgentRecord,
        bench: GlobalBenchRecord,
        permission: str,
        *,
        authentication_context: AuthenticationContext | None,
        owner: str | None,
        owner_principal: ReservationOwner | None,
        allow_legacy_authorisation: bool,
        allow_internal_authorisation: bool,
    ) -> None:
        if allow_legacy_authorisation and allow_internal_authorisation:
            raise ValueError(
                "Legacy and internal reservation authorisation escapes are mutually exclusive"
            )
        if allow_legacy_authorisation and (
            authentication_context is not None or owner_principal is not None
        ):
            raise ValueError("Legacy reservation authorisation cannot carry authenticated identity")
        if authentication_context is not None:
            principal = authentication_context.principal
            if principal.organisation_id != bench.organisation_id:
                raise ValueError(
                    "Reservation target organisation does not match the authenticated principal"
                )
            if owner is not None:
                if owner_principal is None:
                    raise ValueError(
                        "Authenticated reservations require durable principal ownership"
                    )
                if (
                    owner_principal.principal_id != principal.id
                    or owner_principal.principal_type is not principal.type
                    or owner_principal.display_name != principal.display_name
                    or owner != principal.display_name
                ):
                    raise ValueError("Reservation owner does not match the authenticated principal")
        elif (
            self._authorisation is not None
            and owner_principal is not None
            and not allow_internal_authorisation
        ):
            raise ValueError(
                "Reservation principal ownership requires authenticated principal context"
            )

        authorisation = self._authorisation
        if authorisation is None or allow_internal_authorisation or allow_legacy_authorisation:
            return
        if authentication_context is None:
            raise AuthenticationRequiredError(
                "An authenticated principal is required to manage reservations."
            )
        await authorisation.require(
            authentication_context.principal,
            permission,
            AuthorisationResource(
                type=ResourceType.BENCH,
                id=bench.id,
                organisation_id=bench.organisation_id,
                parent_agent_id=agent.id,
            ),
            credential_restrictions=authentication_context.permission_restrictions,
        )

    async def _require_reservation_owner(
        self,
        record: CoordinatedReservationLease,
        owner: str,
        *,
        owner_principal: ReservationOwner | None,
        authentication_context: AuthenticationContext | None,
    ) -> None:
        try:
            _require_owner(record, owner, owner_principal=owner_principal)
        except ReservationOwnerMismatchError:
            if self._authorisation is not None and authentication_context is not None:
                await self._authorisation.audit_permission_denied(
                    authentication_context.principal,
                    "benches:reserve",
                    resource_type=ResourceType.BENCH.value,
                    resource_id=record.reservation.bench_id,
                    reason="The reservation belongs to another principal.",
                )
            raise

    async def _synchronize_pending(
        self,
        pending: CoordinatedReservationLease,
        *,
        operation_key: str,
    ) -> CoordinatedReservationLease:
        if pending.state not in {
            ReservationLeaseState.ACTIVATING,
            ReservationLeaseState.RENEWING,
        }:
            return pending
        confirmation_key = f"confirm:{operation_key}"
        fingerprint = _request_fingerprint(
            "confirm",
            {
                "reservation_id": str(pending.reservation.id),
                "agent_id": str(pending.lease.agent_id),
                "bench_id": pending.lease.bench_id,
                "lease_version": pending.lease.lease_version,
            },
        )
        replay = await self._repository.get_mutation_result(
            confirmation_key,
            organisation_id=pending.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return (await self._repository.get(pending.reservation.id)) or replay.record
        try:
            receipt = await self._synchronizer.apply_lease(pending.lease)
            _require_matching_receipt(pending.lease, receipt)
            receipt_time = _as_utc(
                receipt.confirmed_at,
                field="lease application confirmation timestamp",
            )
            if not pending.lease.is_valid_at(
                receipt_time,
                maximum_clock_skew_seconds=self._maximum_clock_skew,
            ):
                raise ReservationLeaseExpiredError(
                    "Agent confirmed a lease outside its bounded validity window.",
                    reservation_id=str(pending.reservation.id),
                    lease_version=pending.lease.lease_version,
                )
        except Exception:
            return await self._mark_unknown(pending, operation_key=operation_key)

        candidate = CoordinatedReservationLease(
            reservation=_activated_reservation(pending.reservation, receipt_time),
            lease=pending.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=pending.revision + 1,
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=pending.revision,
            mutation_key=confirmation_key,
            request_fingerprint=fingerprint,
            expected_agent_status=AgentStatus.ONLINE,
            expected_bench_status=GlobalBenchStatus.ONLINE,
        )
        if result is not None:
            return result.record
        current = await self._require_record(pending.reservation.id)
        if (
            current.state is ReservationLeaseState.ACTIVE
            and current.lease.lease_version == pending.lease.lease_version
        ):
            return current
        if current.revision != pending.revision:
            return current
        return await self._mark_unknown(pending, operation_key=operation_key)

    async def _mark_unknown(
        self,
        pending: CoordinatedReservationLease,
        *,
        operation_key: str,
    ) -> CoordinatedReservationLease:
        now = self._now()
        deadline = now + timedelta(seconds=self._offline_grace)
        if pending.reservation.ends_at is not None:
            deadline = min(deadline, pending.reservation.ends_at)
        deadline = max(deadline, now)
        mutation_key = f"unknown:{operation_key}"
        fingerprint = _request_fingerprint(
            "unknown",
            {
                "reservation_id": str(pending.reservation.id),
                "lease_version": pending.lease.lease_version,
            },
        )
        replay = await self._repository.get_mutation_result(
            mutation_key,
            organisation_id=pending.reservation.organisation_id,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return (await self._repository.get(pending.reservation.id)) or replay.record
        candidate = CoordinatedReservationLease(
            reservation=pending.reservation,
            lease=pending.lease,
            state=ReservationLeaseState.UNKNOWN,
            revision=pending.revision + 1,
            unknown_since=now,
            reconciliation_deadline=deadline,
        )
        result = await self._repository.replace_if_current(
            candidate,
            expected_revision=pending.revision,
            mutation_key=mutation_key,
            request_fingerprint=fingerprint,
        )
        return (
            result.record
            if result is not None
            else await self._require_record(pending.reservation.id)
        )

    async def _notify_release(self, lease: ReservationLease) -> None:
        try:
            await self._synchronizer.release_lease(lease)
        except Exception:
            # Central fencing is already committed. Reconciliation or local TTL
            # will clear an Agent that missed this idempotent release notification.
            return

    async def _require_record(
        self,
        reservation_id: UUID,
    ) -> CoordinatedReservationLease:
        record = await self._repository.get(reservation_id)
        if record is None:
            raise ReservationNotFoundError(
                f"Reservation {reservation_id} was not found.",
                reservation_id=str(reservation_id),
            )
        return record

    async def _raise_grant_failure(
        self,
        agent_id: UUID,
        bench_id: str,
    ) -> NoReturn:
        current = await self._repository.get_current_for_bench(bench_id)
        if current is not None:
            raise BenchAlreadyReservedError(
                "Global bench already has an active or interrupted reservation.",
                bench_id=bench_id,
                reservation_id=str(current.reservation.id),
            )
        agent = await self._directory.get_agent(agent_id)
        bench = await self._directory.get_bench(bench_id)
        _require_eligible_route(agent_id, bench_id, agent, bench)
        raise ReservationLeaseInvalidError(
            "Reservation grant lost an atomic eligibility race.",
            agent_id=str(agent_id),
            bench_id=bench_id,
        )

    async def _raise_transition_failure(
        self,
        expected: CoordinatedReservationLease,
        *,
        require_eligible: bool = False,
    ) -> NoReturn:
        current = await self._require_record(expected.reservation.id)
        if current.lease.lease_version != expected.lease.lease_version:
            raise ReservationLeaseVersionMismatchError(
                "Reservation lease changed concurrently.",
                reservation_id=str(expected.reservation.id),
                expected_lease_version=expected.lease.lease_version,
                current_lease_version=current.lease.lease_version,
            )
        if require_eligible:
            agent = await self._directory.get_agent(expected.lease.agent_id)
            bench = await self._directory.get_bench(expected.lease.bench_id)
            _require_eligible_route(
                expected.lease.agent_id,
                expected.lease.bench_id,
                agent,
                bench,
            )
        raise ReservationLeaseInvalidError(
            "Reservation lease state changed concurrently.",
            reservation_id=str(expected.reservation.id),
            expected_revision=expected.revision,
            current_revision=current.revision,
            current_state=current.state.value,
        )

    def _now(self) -> datetime:
        return _as_utc(self._clock(), field="reservation lease timestamp")

    def _reservation_duration(self, requested: int | None) -> int:
        duration = self._default_reservation_duration if requested is None else requested
        _require_positive_seconds(duration, field="reservation_duration_seconds")
        if duration > self._maximum_reservation_duration:
            raise ValueError("reservation_duration_seconds exceeds configured maximum")
        return duration

    def _lease_ttl(self, requested: int | None) -> int:
        ttl = self._default_lease_ttl if requested is None else requested
        _require_positive_seconds(ttl, field="lease_ttl_seconds")
        if ttl > self._maximum_lease_ttl:
            raise ValueError("lease_ttl_seconds exceeds configured maximum")
        return ttl


def _terminal_record(
    current: CoordinatedReservationLease,
    *,
    state: ReservationLeaseState,
    reservation_status: ReservationStatus,
    occurred_at: datetime,
) -> CoordinatedReservationLease:
    timestamp = max(
        _as_utc(occurred_at, field="reservation terminal timestamp"),
        current.lease.valid_from,
    )
    reservation_updates: dict[str, object] = {
        "status": reservation_status,
        "release_pending": False,
    }
    if reservation_status is ReservationStatus.RELEASED:
        reservation_updates["released_at"] = timestamp
    elif reservation_status is ReservationStatus.EXPIRED:
        reservation_updates["expired_at"] = timestamp
    return CoordinatedReservationLease(
        reservation=current.reservation.model_copy(update=reservation_updates),
        lease=current.lease.model_copy(update={"released_at": timestamp}),
        state=state,
        revision=current.revision + 1,
    )


def _activated_reservation(
    reservation: Reservation,
    confirmed_at: datetime,
) -> Reservation:
    timestamp = _as_utc(confirmed_at, field="reservation activation timestamp")
    starts_at = reservation.starts_at or reservation.created_at
    activated_at = max(timestamp, starts_at)
    return reservation.model_copy(
        update={
            "status": ReservationStatus.ACTIVE,
            "activated_at": activated_at,
        }
    )


def _validate_coordination_state(record: CoordinatedReservationLease) -> None:
    expected_reservation_statuses = {
        ReservationLeaseState.ACTIVATING: {ReservationStatus.SCHEDULED},
        ReservationLeaseState.ACTIVE: ReservationStatus.ACTIVE,
        ReservationLeaseState.RENEWING: {ReservationStatus.ACTIVE},
        ReservationLeaseState.UNKNOWN: {
            ReservationStatus.SCHEDULED,
            ReservationStatus.ACTIVE,
        },
        ReservationLeaseState.RELEASED: ReservationStatus.RELEASED,
        ReservationLeaseState.EXPIRED: ReservationStatus.EXPIRED,
        ReservationLeaseState.REVOKED: ReservationStatus.CANCELLED,
    }[record.state]
    if isinstance(expected_reservation_statuses, ReservationStatus):
        expected_reservation_statuses = {expected_reservation_statuses}
    if record.reservation.status not in expected_reservation_statuses:
        raise ValueError("Reservation status does not match lease coordination state")
    terminal = record.state in _TERMINAL_LEASE_STATES
    if terminal != (record.lease.released_at is not None):
        raise ValueError("Terminal coordination state and lease release must match")
    if record.state is ReservationLeaseState.UNKNOWN:
        if record.unknown_since is None or record.reconciliation_deadline is None:
            raise ValueError("UNKNOWN lease requires reconciliation timestamps")
        unknown_since = _as_utc(record.unknown_since, field="unknown lease timestamp")
        deadline = _as_utc(
            record.reconciliation_deadline,
            field="lease reconciliation deadline",
        )
        if deadline < unknown_since:
            raise ValueError("Lease reconciliation deadline cannot precede unknown_since")
    elif record.unknown_since is not None or record.reconciliation_deadline is not None:
        raise ValueError("Only UNKNOWN leases may carry reconciliation timestamps")


def _require_eligible_route(
    agent_id: UUID,
    bench_id: str,
    agent: AgentRecord | None,
    bench: GlobalBenchRecord | None,
) -> None:
    if agent is None:
        raise AgentNotFoundError("Agent does not exist.", agent_id=str(agent_id))
    if bench is None or bench.agent_id != agent_id or bench.id != bench_id:
        raise BenchAgentMismatchError(
            "Global bench is not owned by the requested Agent.",
            agent_id=str(agent_id),
            bench_id=bench_id,
        )
    if agent.status is AgentStatus.REVOKED:
        raise AgentRevokedError("Agent is revoked.", agent_id=str(agent_id))
    if agent.status in {AgentStatus.DRAINING, AgentStatus.DRAINED}:
        raise AgentDrainingError("Agent is draining.", agent_id=str(agent_id))
    if agent.status is AgentStatus.DEGRADED:
        raise AgentDegradedError("Agent is degraded.", agent_id=str(agent_id))
    if agent.status is AgentStatus.INCOMPATIBLE:
        raise AgentIncompatibleError("Agent is incompatible.", agent_id=str(agent_id))
    if agent.status is not AgentStatus.ONLINE:
        raise AgentOfflineError("Agent is offline.", agent_id=str(agent_id))
    if bench.status is GlobalBenchStatus.DEGRADED:
        raise AgentDegradedError(
            "Global bench is degraded.",
            agent_id=str(agent_id),
            bench_id=bench_id,
        )
    if bench.status is not GlobalBenchStatus.ONLINE:
        raise AgentOfflineError(
            "Global bench is offline.",
            agent_id=str(agent_id),
            bench_id=bench_id,
        )


def _validate_trusted_route_organisation(
    agent: AgentRecord,
    bench: GlobalBenchRecord,
) -> None:
    if agent.organisation_id != bench.organisation_id:
        raise ValueError("Agent and bench organisations do not match")


def _validate_record_organisation(
    record: CoordinatedReservationLease,
    bench: GlobalBenchRecord,
) -> None:
    if record.reservation.organisation_id != bench.organisation_id:
        raise ValueError("Reservation organisation does not match its trusted bench")


def _require_owner(
    record: CoordinatedReservationLease,
    owner: str,
    *,
    owner_principal: ReservationOwner | None = None,
) -> None:
    reservation = record.reservation
    if owner_principal is not None and reservation.owner_principal_id is not None:
        owner_mismatch = (
            reservation.owner_principal_id != owner_principal.principal_id
            or reservation.owner_principal_type != owner_principal.principal_type.value
        )
    else:
        owner_mismatch = reservation.owner != owner
    if owner_mismatch:
        raise ReservationOwnerMismatchError(
            "Reservation owner does not match.",
            reservation_id=str(reservation.id),
        )


def _require_version(record: CoordinatedReservationLease, expected_version: int) -> None:
    if record.lease.lease_version != expected_version:
        raise ReservationLeaseVersionMismatchError(
            "Reservation lease version is stale.",
            reservation_id=str(record.reservation.id),
            expected_lease_version=expected_version,
            current_lease_version=record.lease.lease_version,
        )


def _require_matching_receipt(
    lease: ReservationLease,
    receipt: LeaseApplicationReceipt,
) -> None:
    if (
        receipt.reservation_id != lease.reservation_id
        or receipt.agent_id != lease.agent_id
        or receipt.bench_id != lease.bench_id
        or receipt.lease_version != lease.lease_version
    ):
        raise ReservationLeaseInvalidError(
            "Agent confirmation does not match the offered reservation lease.",
            reservation_id=str(lease.reservation_id),
            lease_version=lease.lease_version,
        )
    _as_utc(receipt.confirmed_at, field="lease application confirmation timestamp")


def _require_positive_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_lease_version must be a positive integer")


def _require_positive_seconds(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _require_nonnegative_seconds(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _require_mutation_key(value: str) -> str:
    return _require_text(value, field="idempotency_key", maximum_length=500)


def _require_text(value: str, *, field: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field} cannot exceed {maximum_length} characters")
    return normalized


def _request_fingerprint(kind: str, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
