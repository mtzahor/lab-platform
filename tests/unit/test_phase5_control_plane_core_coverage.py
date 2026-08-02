from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.control_plane_core import artifacts as artifact_module
from lab_platform.control_plane_core import distributed_ci as ci_module
from lab_platform.control_plane_core import reservations as reservation_module
from lab_platform.control_plane_core.artifacts import (
    DistributedArtifactService,
    FilesystemTransferStore,
    InMemoryArtifactTransferRepository,
)
from lab_platform.control_plane_core.errors import (
    AgentNotFoundError,
    ArtifactTransferFailedError,
    ArtifactTransferTokenExpiredError,
    BenchAgentMismatchError,
)
from lab_platform.control_plane_core.reservations import (
    CentralReservationLeaseService,
    CoordinatedReservationLease,
    ReservationLeaseState,
)
from lab_platform.control_plane_core.workflows import (
    DistributedWorkflowReservationLifecycle,
)
from lab_platform.core.errors import (
    ArtifactChecksumMismatchError,
    ArtifactTooLargeError,
    ReservationNotFoundError,
)
from lab_platform.models import (
    AgentRecord,
    AgentStatus,
    ArtifactReference,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandType,
    Reservation,
    ReservationLease,
    ReservationSource,
    ReservationStatus,
    WorkflowDefinition,
)

NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)
AGENT_ID = UUID(int=7001)
BENCH_ID = "coverage-agent/bench-01"


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TokenFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"lpt_{self._value:048d}"


async def _chunks(*values: object) -> AsyncIterator[bytes]:
    for value in values:
        yield cast(bytes, value)


def _remote_artifact(
    *,
    artifact_id: UUID | None = None,
    local_artifact_id: UUID | None = None,
    content: bytes = b"artifact-content",
    uploaded_at: datetime | None = None,
) -> RemoteArtifactMetadata:
    return RemoteArtifactMetadata(
        id=artifact_id or uuid4(),
        agent_id=AGENT_ID,
        local_artifact_id=local_artifact_id or uuid4(),
        command_id=UUID(int=7002),
        operation_id=UUID(int=7003),
        name="result.bin",
        artifact_type="test-output",
        content_type="application/octet-stream",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_at=NOW,
        uploaded_at=uploaded_at,
    )


def _transfer(
    *,
    transfer_id: UUID | None = None,
    token_hash: str = "a" * 64,
) -> ArtifactTransferRecord:
    return ArtifactTransferRecord(
        id=transfer_id or uuid4(),
        agent_id=AGENT_ID,
        artifact_id=UUID(int=7004),
        direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
        token_hash=token_hash,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        expected_sha256="b" * 64,
        expected_size_bytes=10,
    )


def _reservation_record(
    *,
    state: ReservationLeaseState = ReservationLeaseState.ACTIVE,
    metadata: dict[str, str] | None = None,
) -> CoordinatedReservationLease:
    terminal = state in {
        ReservationLeaseState.RELEASED,
        ReservationLeaseState.EXPIRED,
        ReservationLeaseState.REVOKED,
    }
    status = {
        ReservationLeaseState.ACTIVATING: ReservationStatus.SCHEDULED,
        ReservationLeaseState.ACTIVE: ReservationStatus.ACTIVE,
        ReservationLeaseState.RENEWING: ReservationStatus.ACTIVE,
        ReservationLeaseState.UNKNOWN: ReservationStatus.ACTIVE,
        ReservationLeaseState.RELEASED: ReservationStatus.RELEASED,
        ReservationLeaseState.EXPIRED: ReservationStatus.EXPIRED,
        ReservationLeaseState.REVOKED: ReservationStatus.CANCELLED,
    }[state]
    reservation_id = UUID(int=7010)
    released_at = NOW + timedelta(minutes=1) if terminal else None
    reservation = Reservation(
        id=reservation_id,
        bench_id=BENCH_ID,
        owner="ci/coverage",
        created_at=NOW,
        requested_at=NOW,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        activated_at=NOW if status is not ReservationStatus.SCHEDULED else None,
        released_at=released_at if status is ReservationStatus.RELEASED else None,
        expired_at=released_at if status is ReservationStatus.EXPIRED else None,
        status=status,
        source=ReservationSource.CI,
        metadata=metadata or {},
        idempotency_key="coverage-reservation",
    )
    lease = ReservationLease(
        reservation_id=reservation_id,
        agent_id=AGENT_ID,
        bench_id=BENCH_ID,
        owner=reservation.owner,
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        lease_version=3,
        released_at=released_at,
    )
    timestamps = (
        {
            "unknown_since": NOW + timedelta(minutes=1),
            "reconciliation_deadline": NOW + timedelta(minutes=2),
        }
        if state is ReservationLeaseState.UNKNOWN
        else {}
    )
    return CoordinatedReservationLease(
        reservation=reservation,
        lease=lease,
        state=state,
        revision=4,
        **timestamps,
    )


def _agent() -> AgentRecord:
    return AgentRecord(
        id=AGENT_ID,
        slug="coverage-agent",
        name="Coverage Agent",
        status=AgentStatus.ONLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        registered_at=NOW,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )


def _bench() -> GlobalBenchRecord:
    return GlobalBenchRecord(
        id=BENCH_ID,
        agent_id=AGENT_ID,
        agent_slug="coverage-agent",
        local_bench_id="bench-01",
        name="Coverage Bench",
        backend_id="simlab",
        kind=GlobalBenchKind.SIMULATED,
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"reset"}),
        created_at=NOW,
        updated_at=NOW,
        last_seen_at=NOW,
    )


def test_in_memory_artifact_repository_rejects_identity_and_attempt_reuse() -> None:
    async def scenario() -> None:
        repository = InMemoryArtifactTransferRepository()
        transfer = _transfer()
        assert await repository.create_transfer(transfer) == transfer
        assert await repository.create_transfer(transfer) == transfer

        with pytest.raises(ArtifactTransferFailedError, match="ID was reused"):
            await repository.create_transfer(
                transfer.model_copy(update={"expected_size_bytes": 11})
            )
        with pytest.raises(ArtifactTransferFailedError, match="token collision"):
            await repository.create_transfer(
                _transfer(transfer_id=uuid4(), token_hash=transfer.token_hash)
            )
        assert (
            await repository.update_transfer(
                transfer.model_copy(update={"status": ArtifactTransferStatus.FAILED}),
                expected_statuses={ArtifactTransferStatus.IN_PROGRESS},
            )
            is None
        )

        attempt = ArtifactTransferAttempt(
            transfer_id=transfer.id,
            attempt_number=1,
            started_at=NOW,
        )
        assert await repository.add_attempt(attempt) == attempt
        assert await repository.add_attempt(attempt) == attempt
        with pytest.raises(ArtifactTransferFailedError, match="attempt was reused"):
            await repository.add_attempt(attempt.model_copy(update={"bytes_transferred": 1}))

        second = _transfer(transfer_id=uuid4(), token_hash="c" * 64)
        await repository.create_transfer(second)
        with pytest.raises(ArtifactTransferFailedError, match="sequence is invalid"):
            await repository.add_attempt(
                ArtifactTransferAttempt(
                    transfer_id=second.id,
                    attempt_number=2,
                    started_at=NOW,
                )
            )

        local_id = uuid4()
        artifact = _remote_artifact(local_artifact_id=local_id)
        assert await repository.put_remote_artifact(artifact) == artifact
        conflicting = artifact.model_copy(update={"id": uuid4(), "name": "changed.bin"})
        with pytest.raises(ArtifactTransferFailedError, match="identity was reused"):
            await repository.put_remote_artifact(conflicting)

        uploaded = artifact.model_copy(update={"id": uuid4(), "uploaded_at": NOW})
        stored_upload = await repository.put_remote_artifact(uploaded)
        assert stored_upload.id == artifact.id
        changed_timestamp = uploaded.model_copy(update={"uploaded_at": NOW + timedelta(seconds=1)})
        with pytest.raises(ArtifactTransferFailedError, match="timestamp cannot change"):
            await repository.put_remote_artifact(changed_timestamp)

        reused_id = _remote_artifact(artifact_id=uuid4())
        await repository.put_remote_artifact(reused_id)
        with pytest.raises(ArtifactTransferFailedError, match="identity was reused"):
            await repository.put_remote_artifact(
                reused_id.model_copy(update={"agent_id": uuid4(), "local_artifact_id": uuid4()})
            )

    asyncio.run(scenario())


def test_filesystem_transfer_store_verifies_every_failure_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "store"
        store = FilesystemTransferStore(root)
        content = b"verified bytes"
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = uuid4()
        source = tmp_path / "source.bin"
        source.write_bytes(content)

        staged = await store.stage_verified_file(
            artifact_id,
            digest,
            len(content),
            source,
            maximum_size_bytes=100,
        )
        assert staged.read_bytes() == content
        assert (
            await store.stage_verified_file(
                artifact_id,
                digest,
                len(content),
                source,
                maximum_size_bytes=100,
            )
            == staged
        )
        assert store.open_verified(artifact_id, digest) == staged

        with pytest.raises(ArtifactTransferFailedError, match="regular file"):
            await store.stage_verified_file(
                uuid4(), digest, len(content), tmp_path, maximum_size_bytes=100
            )
        with pytest.raises(ArtifactTooLargeError):
            await store.write_verified(
                uuid4(), digest, len(content), _chunks(content), maximum_size_bytes=1
            )
        with pytest.raises(ArtifactTransferFailedError, match="non-byte"):
            await store.write_verified(
                uuid4(), digest, len(content), _chunks("not bytes"), maximum_size_bytes=100
            )
        with pytest.raises(ArtifactTooLargeError, match="declared size"):
            await store.write_verified(
                uuid4(), digest, len(content) - 1, _chunks(content), maximum_size_bytes=100
            )
        with pytest.raises(ArtifactTransferFailedError, match="content length"):
            await store.write_verified(
                uuid4(), digest, len(content), _chunks(content[:-1]), maximum_size_bytes=100
            )
        with pytest.raises(ArtifactChecksumMismatchError):
            await store.write_verified(
                uuid4(), "d" * 64, len(content), _chunks(content), maximum_size_bytes=100
            )
        assert list(root.rglob("*.part")) == []

        with pytest.raises(ArtifactTransferFailedError, match="unavailable"):
            store.open_verified(uuid4(), digest)

        outside = tmp_path / "outside.bin"
        outside.write_bytes(content)
        escaped_id = uuid4()
        escaped_path = store.path_for(escaped_id, digest)
        escaped_path.parent.mkdir(parents=True, exist_ok=True)
        escaped_path.symlink_to(outside)
        with pytest.raises(ArtifactTransferFailedError, match="escaped"):
            store.open_verified(escaped_id, digest)

        staged.write_bytes(b"corrupt")
        with pytest.raises(ArtifactChecksumMismatchError, match="Stored artifact"):
            store.open_verified(artifact_id, digest)

    asyncio.run(scenario())


def test_distributed_artifact_service_authorization_download_and_expiry(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        repository = InMemoryArtifactTransferRepository()
        store = FilesystemTransferStore(tmp_path / "store")
        service = DistributedArtifactService(
            repository,
            store,
            maximum_upload_size_bytes=100,
            token_ttl_seconds=10,
            clock=clock,
            token_factory=TokenFactory(),
        )
        content = b"downloadable"
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = uuid4()
        await store.write_verified(
            artifact_id,
            digest,
            len(content),
            _chunks(content),
            maximum_size_bytes=100,
        )
        issued = await service.issue_download(
            agent_id=AGENT_ID,
            artifact_id=artifact_id,
            sha256=digest,
            size_bytes=len(content),
        )
        token = issued.plaintext_token.get_secret_value()

        with pytest.raises(ArtifactTransferFailedError, match="authentication failed"):
            await service.authorize(
                issued.transfer.id,
                "bad",
                direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
            )
        valid_unknown_token = "lpt_" + "z" * 48
        for transfer_id, candidate_token, direction, agent_id in (
            (
                uuid4(),
                token,
                ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
                AGENT_ID,
            ),
            (
                issued.transfer.id,
                token,
                ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
                AGENT_ID,
            ),
            (
                issued.transfer.id,
                token,
                ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
                uuid4(),
            ),
            (
                issued.transfer.id,
                valid_unknown_token,
                ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
                AGENT_ID,
            ),
        ):
            with pytest.raises(ArtifactTransferFailedError, match="authentication failed"):
                await service.authorize(
                    transfer_id,
                    candidate_token,
                    direction=direction,
                    agent_id=agent_id,
                )

        assert await service.download_path(issued.transfer.id, token, agent_id=AGENT_ID) == (
            store.path_for(artifact_id, digest)
        )
        assert await service.download_path(issued.transfer.id, token, agent_id=AGENT_ID) == (
            store.path_for(artifact_id, digest)
        )
        completed = await repository.get_transfer(issued.transfer.id)
        assert completed is not None
        assert completed.status is ArtifactTransferStatus.COMPLETED
        assert completed.attempt_count == 1

        expiring = await service.issue_download(
            agent_id=AGENT_ID,
            artifact_id=artifact_id,
            sha256=digest,
            size_bytes=len(content),
        )
        expiring_token = expiring.plaintext_token.get_secret_value()
        clock.now += timedelta(seconds=11)
        for _ in range(2):
            with pytest.raises(ArtifactTransferTokenExpiredError):
                await service.authorize(
                    expiring.transfer.id,
                    expiring_token,
                    direction=ArtifactTransferDirection.CONTROL_PLANE_TO_AGENT,
                )
        expired = await repository.get_transfer(expiring.transfer.id)
        assert expired is not None and expired.status is ArtifactTransferStatus.EXPIRED

        with pytest.raises(ArtifactTransferFailedError, match="size does not match"):
            await service.issue_download(
                agent_id=AGENT_ID,
                artifact_id=artifact_id,
                sha256=digest,
                size_bytes=len(content) + 1,
            )

    asyncio.run(scenario())

    repository = InMemoryArtifactTransferRepository()
    store = FilesystemTransferStore(tmp_path / "other-store")
    with pytest.raises(ValueError):
        DistributedArtifactService(repository, store, maximum_upload_size_bytes=0)
    with pytest.raises(ValueError):
        DistributedArtifactService(repository, store, token_ttl_seconds=0)


def test_distributed_artifact_service_upload_validation_and_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = InMemoryArtifactTransferRepository()
        service = DistributedArtifactService(
            repository,
            FilesystemTransferStore(tmp_path / "store"),
            maximum_upload_size_bytes=20,
            token_factory=TokenFactory(),
            clock=MutableClock(),
        )
        content = b"upload me"
        with pytest.raises(ArtifactTooLargeError):
            await service.register_remote_artifact(_remote_artifact(content=b"x" * 21))
        with pytest.raises(ArtifactTransferFailedError, match="already marked uploaded"):
            await service.register_remote_artifact(
                _remote_artifact(content=content, uploaded_at=NOW)
            )
        with pytest.raises(ArtifactTransferFailedError, match="does not exist"):
            await service.issue_upload(uuid4())

        artifact = await service.register_remote_artifact(_remote_artifact(content=content))
        issued = await service.issue_upload(artifact.id)
        token = issued.plaintext_token.get_secret_value()
        with pytest.raises(ArtifactTransferFailedError, match="Content-Length"):
            await service.upload(
                issued.transfer.id,
                token,
                _chunks(content),
                content_length=len(content) + 1,
            )
        with pytest.raises(ArtifactChecksumMismatchError):
            await service.upload(
                issued.transfer.id,
                token,
                _chunks(b"bad bytes"),
                content_length=len(content),
            )
        failed = await repository.get_transfer(issued.transfer.id)
        assert failed is not None
        assert failed.status is ArtifactTransferStatus.FAILED
        assert failed.attempt_count == 1

        uploaded = await service.upload(
            issued.transfer.id,
            token,
            _chunks(content),
            content_length=None,
            agent_id=AGENT_ID,
        )
        assert uploaded.uploaded_at == NOW
        completed = await repository.get_transfer(issued.transfer.id)
        assert completed is not None and completed.attempt_count == 2

        orphan = await service.register_remote_artifact(_remote_artifact(content=content))
        orphan_transfer = await service.issue_upload(orphan.id)
        repository._artifacts.pop(orphan.id)
        with pytest.raises(ArtifactTransferFailedError, match="metadata does not exist"):
            await service.upload(
                orphan_transfer.transfer.id,
                orphan_transfer.plaintext_token.get_secret_value(),
                _chunks(content),
                content_length=len(content),
            )

        incomplete = await service.register_remote_artifact(_remote_artifact(content=content))
        incomplete_transfer = await service.issue_upload(incomplete.id)
        repository._transfers[incomplete_transfer.transfer.id] = (
            incomplete_transfer.transfer.model_copy(
                update={"status": ArtifactTransferStatus.COMPLETED, "completed_at": NOW}
            )
        )
        with pytest.raises(ArtifactTransferFailedError, match="incomplete metadata"):
            await service.upload(
                incomplete_transfer.transfer.id,
                incomplete_transfer.plaintext_token.get_secret_value(),
                _chunks(content),
                content_length=len(content),
            )

        with pytest.raises(ArtifactTransferFailedError, match="transfer does not exist"):
            await service._require_transfer(uuid4())

        invalid_tokens = DistributedArtifactService(
            repository,
            FilesystemTransferStore(tmp_path / "invalid-token-store"),
            token_factory=lambda: "lpt_too_short",
        )
        with pytest.raises(ValueError, match="insufficient entropy"):
            await invalid_tokens.issue_upload(artifact.id)

    asyncio.run(scenario())


def test_coordinated_reservation_invariants_and_validation_helpers() -> None:
    active = _reservation_record()
    for lease, revision in (
        (active.lease, 0),
        (active.lease.model_copy(update={"reservation_id": uuid4()}), 1),
        (active.lease.model_copy(update={"bench_id": "coverage-agent/other"}), 1),
        (active.lease.model_copy(update={"owner": "other"}), 1),
    ):
        with pytest.raises(ValueError):
            CoordinatedReservationLease(
                reservation=active.reservation,
                lease=lease,
                state=ReservationLeaseState.ACTIVE,
                revision=revision,
            )

    with pytest.raises(ValueError, match="status does not match"):
        CoordinatedReservationLease(
            reservation=active.reservation.model_copy(
                update={"status": ReservationStatus.SCHEDULED}
            ),
            lease=active.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=1,
        )
    with pytest.raises(ValueError, match="release must match"):
        CoordinatedReservationLease(
            reservation=active.reservation.model_copy(
                update={"status": ReservationStatus.RELEASED, "released_at": NOW}
            ),
            lease=active.lease,
            state=ReservationLeaseState.RELEASED,
            revision=1,
        )
    with pytest.raises(ValueError, match="requires reconciliation"):
        CoordinatedReservationLease(
            reservation=active.reservation,
            lease=active.lease,
            state=ReservationLeaseState.UNKNOWN,
            revision=1,
        )
    with pytest.raises(ValueError, match="cannot precede"):
        CoordinatedReservationLease(
            reservation=active.reservation,
            lease=active.lease,
            state=ReservationLeaseState.UNKNOWN,
            revision=1,
            unknown_since=NOW + timedelta(minutes=2),
            reconciliation_deadline=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="Only UNKNOWN"):
        CoordinatedReservationLease(
            reservation=active.reservation,
            lease=active.lease,
            state=ReservationLeaseState.ACTIVE,
            revision=1,
            unknown_since=NOW,
        )

    with pytest.raises(AgentNotFoundError):
        reservation_module._require_eligible_route(AGENT_ID, BENCH_ID, None, _bench())
    with pytest.raises(BenchAgentMismatchError):
        reservation_module._require_eligible_route(AGENT_ID, BENCH_ID, _agent(), None)
    with pytest.raises(ValueError, match="positive integer"):
        reservation_module._require_positive_version(True)
    with pytest.raises(ValueError, match="must be a string"):
        reservation_module._require_text(cast(Any, 1), field="owner", maximum_length=10)
    with pytest.raises(ValueError, match="cannot exceed"):
        reservation_module._require_text("too long", field="owner", maximum_length=2)
    with pytest.raises(ValueError, match="timezone-aware"):
        reservation_module._as_utc(datetime(2026, 7, 29), field="test")

    dummy = cast(Any, object())
    with pytest.raises(ValueError, match="Maximum lease TTL"):
        CentralReservationLeaseService(
            dummy,
            dummy,
            dummy,
            default_lease_ttl_seconds=10,
            maximum_lease_ttl_seconds=9,
        )


class LifecycleRemoteWork:
    def __init__(self, commands: Iterable[RemoteCommand]) -> None:
        self.commands = list(commands)

    async def list_terminal_workflow_commands(self, *, limit: int) -> list[RemoteCommand]:
        return self.commands[:limit]


class LifecycleReservations:
    def __init__(
        self,
        record: CoordinatedReservationLease | None,
        *,
        fail_get: bool = False,
        fail_release: bool = False,
    ) -> None:
        self.record = record
        self.fail_get = fail_get
        self.fail_release = fail_release

    async def grant(
        self,
        *,
        agent_id: UUID,
        bench_id: str,
        owner: str,
        idempotency_key: str,
        reservation_duration_seconds: int | None = None,
        lease_ttl_seconds: int | None = None,
        source: ReservationSource = ReservationSource.API,
        metadata: Mapping[str, str] | None = None,
    ) -> CoordinatedReservationLease:
        del (
            agent_id,
            bench_id,
            owner,
            idempotency_key,
            reservation_duration_seconds,
            lease_ttl_seconds,
            source,
            metadata,
        )
        raise AssertionError("grant is not used by reservation cleanup")

    async def get(self, _reservation_id: UUID) -> CoordinatedReservationLease:
        if self.fail_get or self.record is None:
            raise ReservationNotFoundError("missing")
        return self.record

    async def release(
        self,
        _reservation_id: UUID,
        *,
        owner: str,
        expected_lease_version: int,
        idempotency_key: str,
    ) -> CoordinatedReservationLease:
        del owner, expected_lease_version, idempotency_key
        if self.fail_release or self.record is None:
            raise ReservationNotFoundError("raced")
        return _reservation_record(
            state=ReservationLeaseState.RELEASED,
            metadata={"reservation_lifecycle": "workflow"},
        )


def _workflow_command(
    *, command_type: RemoteCommandType = RemoteCommandType.RUN_WORKFLOW
) -> RemoteCommand:
    return RemoteCommand(
        agent_id=AGENT_ID,
        bench_id=BENCH_ID,
        command_type=command_type,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="coverage-command",
        reservation_id=UUID(int=7010) if command_type is RemoteCommandType.RUN_WORKFLOW else None,
        lease_version=3 if command_type is RemoteCommandType.RUN_WORKFLOW else None,
    )


def test_workflow_reservation_lifecycle_ignores_invalid_and_raced_cleanup() -> None:
    async def scenario() -> None:
        non_workflow = _workflow_command(command_type=RemoteCommandType.PROBE)
        lifecycle = DistributedWorkflowReservationLifecycle(
            LifecycleRemoteWork([non_workflow]),
            LifecycleReservations(None),
        )
        assert await lifecycle.release_terminal() == 0

        command = _workflow_command()
        missing = DistributedWorkflowReservationLifecycle(
            LifecycleRemoteWork([command]),
            LifecycleReservations(None, fail_get=True),
        )
        assert await missing.release_terminal() == 0

        active = _reservation_record(metadata={"reservation_lifecycle": "workflow"})
        raced = DistributedWorkflowReservationLifecycle(
            LifecycleRemoteWork([command]),
            LifecycleReservations(active, fail_release=True),
        )
        assert await raced.release_terminal() == 0

    asyncio.run(scenario())


def _ci_session(*, outcome: CiOutcome = CiOutcome.PENDING) -> CiSession:
    return CiSession(
        provider=CiProvider.LOCAL,
        external_run_id="coverage-run",
        requested_by="ci/coverage",
        status=CiSessionStatus.RUNNING,
        outcome=outcome,
        created_at=NOW,
    )


def _operation(status: DistributedOperationStatus) -> DistributedOperation:
    terminal = status in {
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
        DistributedOperationStatus.CANCELLED,
    }
    return DistributedOperation(
        remote_command_id=UUID(int=7020),
        agent_id=AGENT_ID,
        bench_id=BENCH_ID,
        operation_type="RUN_WORKFLOW",
        status=status,
        created_at=NOW,
        completed_at=NOW if terminal else None,
    )


def test_distributed_ci_helpers_cover_timeout_and_input_boundaries() -> None:
    timed_out = _ci_session(outcome=CiOutcome.TIMED_OUT)
    assert ci_module._operation_session_result(
        timed_out,
        _operation(DistributedOperationStatus.SUCCEEDED),
        None,
    ) == (CiSessionStatus.TIMED_OUT, CiOutcome.TIMED_OUT)
    assert ci_module._operation_session_result(
        timed_out,
        _operation(DistributedOperationStatus.FAILED),
        None,
    ) == (CiSessionStatus.TIMED_OUT, CiOutcome.TIMED_OUT)

    assert (
        ci_module._requested_kind(BenchRequest(allow_simulated=True, allow_physical=True)) is None
    )
    assert ci_module._requested_kind(BenchRequest()) is GlobalBenchKind.SIMULATED
    assert (
        ci_module._requested_kind(BenchRequest(allow_simulated=False, allow_physical=True))
        is GlobalBenchKind.PHYSICAL
    )
    with pytest.raises(ValueError, match="must not be empty"):
        ci_module._require_text(" ", field="value", maximum_length=2)
    with pytest.raises(ValueError, match="maximum length"):
        ci_module._require_text("long", field="value", maximum_length=2)
    with pytest.raises(ValueError, match="timezone-aware"):
        ci_module._as_utc(datetime(2026, 7, 29), field="value")

    dummy = cast(Any, object())
    with pytest.raises(ValueError, match="Default CI timeout"):
        ci_module.DistributedCiSessionService(
            dummy,
            dummy,
            dummy,
            dummy,
            dummy,
            dummy,
            dummy,
            dummy,
            session_timeout_seconds=11,
            maximum_session_timeout_seconds=10,
        )

    definition = WorkflowDefinition.model_validate(
        {
            "name": "coverage-workflow",
            "version": 2,
            "requirements": {"capabilities": ["reset"]},
            "inputs": {"firmware": {"type": "artifact", "required": True}},
            "steps": [{"action": "reset"}],
        }
    )
    with_artifact = ci_module._with_ci_capabilities(
        definition,
        BenchRequest(required_capabilities={"diagnostic"}),
    )
    assert set(with_artifact.requirements.capabilities) == {"diagnostic", "reset"}
    assert ArtifactReference(artifact_id=uuid4()).artifact_id is not None


def test_artifact_private_timestamp_guard_and_hash_file(tmp_path: Path) -> None:
    content = b"more than one byte"
    path = tmp_path / "hash.bin"
    path.write_bytes(content)
    assert artifact_module._hash_file(path) == (
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        artifact_module._utc(datetime(2026, 7, 29))
