from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from lab_platform.logging import StructuredFormatter, get_logger
from lab_platform.models import (
    DISTRIBUTED_OPERATION_TRANSITIONS,
    REMOTE_COMMAND_TRANSITIONS,
    TERMINAL_DISTRIBUTED_OPERATION_STATUSES,
    TERMINAL_REMOTE_COMMAND_STATUSES,
    AgentConnectionRecord,
    AgentCredential,
    AgentCredentialKind,
    AgentEnrollmentToken,
    AgentRecord,
    AgentStatus,
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    Bench,
    BenchRequest,
    BufferedAgentEvent,
    BufferedEventPriority,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    CommandJournalEntry,
    DistributedOperation,
    DistributedOperationStatus,
    EnrollmentStatus,
    Event,
    GlobalBenchKind,
    GlobalBenchRecord,
    GlobalBenchStatus,
    HealthStatus,
    ReconciliationBenchSnapshot,
    ReconciliationCommandState,
    ReconciliationReport,
    RemoteArtifactMetadata,
    RemoteCommand,
    RemoteCommandStatus,
    RemoteCommandType,
    ReservationLease,
    ReservationSource,
)
from lab_platform.models import (
    TestResult as HardwareTestResult,
)
from lab_platform.models import (
    TestStatus as HardwareTestStatus,
)
from lab_platform.simlab import SimLab
from pydantic import ValidationError

AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")


BOOT_ID = UUID("1ab59e13-5517-482f-849c-2fe8145db25c")


RESERVATION_ID = UUID("a9e16eb6-cc01-48df-a6c7-c99ef117a002")


COMMAND_ID = UUID("10f76299-e654-41a8-a896-71f8fd9bd0b1")


NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


LATER = NOW + timedelta(minutes=5)


def _lease(**updates: object) -> ReservationLease:
    values: dict[str, object] = {
        "reservation_id": RESERVATION_ID,
        "agent_id": AGENT_ID,
        "bench_id": "home-lab/esp32-01",
        "owner": "github-actions",
        "valid_from": NOW,
        "valid_until": LATER,
        "lease_version": 3,
    }
    values.update(updates)
    return ReservationLease.model_validate(values)


def _command(**updates: object) -> RemoteCommand:
    values: dict[str, object] = {
        "id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "bench_id": "home-lab/esp32-01",
        "command_type": RemoteCommandType.RUN_WORKFLOW,
        "payload": {"workflow_name": "esp32-ci-test"},
        "status": RemoteCommandStatus.CREATED,
        "created_at": NOW,
        "expires_at": LATER,
        "idempotency_key": "ci-session:42:workflow",
    }
    values.update(updates)
    return RemoteCommand.model_validate(values)


def _reconciliation_bench(**updates: object) -> ReconciliationBenchSnapshot:
    values: dict[str, object] = {
        "local_bench_id": "esp32-01",
        "name": "ESP32",
        "backend_id": "hardware",
        "kind": GlobalBenchKind.PHYSICAL,
        "target_type": "esp32-devkit-v1",
        "status": GlobalBenchStatus.ONLINE,
        "health": HealthStatus.HEALTHY,
        "capabilities": frozenset({"firmware", "serial"}),
        "labels": {"board": "esp32"},
    }
    values.update(updates)
    return ReconciliationBenchSnapshot.model_validate(values)


def test_global_bench_identity_is_immutable_and_timestamps_are_coherent() -> None:
    offset_now = NOW.astimezone(timezone(timedelta(hours=3)))
    bench = GlobalBenchRecord(
        id="home-lab/esp32-01",
        agent_id=AGENT_ID,
        agent_slug="home-lab",
        local_bench_id="esp32-01",
        name="ESP32",
        backend_id="hardware",
        kind=GlobalBenchKind.PHYSICAL,
        target_type="esp32-devkit-v1",
        status=GlobalBenchStatus.ONLINE,
        health=HealthStatus.HEALTHY,
        capabilities=frozenset({"firmware", "serial"}),
        labels={"board": "esp32"},
        last_seen_at=offset_now,
        created_at=offset_now,
        updated_at=offset_now,
    )

    assert bench.id == f"{bench.agent_slug}/{bench.local_bench_id}"
    assert bench.last_seen_at == NOW
    assert bench.online
    assert not bench.model_copy(update={"status": GlobalBenchStatus.OFFLINE}).online

    with pytest.raises(ValidationError, match="does not match"):
        GlobalBenchRecord.model_validate({**bench.model_dump(), "id": "other-lab/esp32-01"})
    with pytest.raises(ValidationError, match="syntax"):
        GlobalBenchRecord.model_validate({**bench.model_dump(), "id": "home-lab/nested/esp32-01"})
    with pytest.raises(ValidationError, match="last_seen_at"):
        GlobalBenchRecord.model_validate({**bench.model_dump(), "last_seen_at": LATER})


def test_agent_connection_rejects_impossible_timeline_and_naive_times() -> None:
    connection = AgentConnectionRecord(
        agent_id=AGENT_ID,
        boot_id=BOOT_ID,
        protocol_version="1.0",
        connected_at=NOW,
        last_heartbeat_at=NOW,
        last_sequence_number=12,
        observed_clock_offset_seconds=-0.25,
    )
    assert connection.disconnected_at is None

    with pytest.raises(ValidationError, match="last_heartbeat_at"):
        AgentConnectionRecord.model_validate(
            {
                **connection.model_dump(),
                "last_heartbeat_at": NOW - timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="disconnected_at"):
        AgentConnectionRecord.model_validate(
            {
                **connection.model_dump(),
                "last_heartbeat_at": LATER,
                "disconnected_at": NOW,
            }
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        AgentConnectionRecord.model_validate(
            {**connection.model_dump(), "connected_at": NOW.replace(tzinfo=None)}
        )


def test_reservation_lease_uses_versions_timestamps_release_and_bounded_skew() -> None:
    lease = _lease()

    assert lease.is_valid_at(NOW)
    assert lease.is_valid_at(LATER)
    assert not lease.is_valid_at(NOW - timedelta(seconds=1))
    assert not lease.is_valid_at(LATER + timedelta(seconds=1))
    assert lease.is_valid_at(NOW - timedelta(seconds=30), maximum_clock_skew_seconds=30)
    assert lease.is_valid_at(LATER + timedelta(seconds=30), maximum_clock_skew_seconds=30)
    assert not _lease(released_at=NOW + timedelta(seconds=1)).is_valid_at(
        NOW + timedelta(seconds=2)
    )

    with pytest.raises(ValidationError, match="valid_until"):
        _lease(valid_until=NOW)
    with pytest.raises(ValidationError, match="released_at"):
        _lease(released_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _lease(lease_version=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        lease.is_valid_at(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="cannot be negative"):
        lease.is_valid_at(NOW, maximum_clock_skew_seconds=-1)


def test_remote_command_state_graph_prevents_terminal_regression() -> None:
    assert REMOTE_COMMAND_TRANSITIONS[RemoteCommandStatus.CREATED] >= {
        RemoteCommandStatus.QUEUED,
        RemoteCommandStatus.DISPATCHED,
        RemoteCommandStatus.EXPIRED,
    }
    assert (
        RemoteCommandStatus.ACCEPTED in REMOTE_COMMAND_TRANSITIONS[RemoteCommandStatus.DISPATCHED]
    )
    assert RemoteCommandStatus.RUNNING in REMOTE_COMMAND_TRANSITIONS[RemoteCommandStatus.ACCEPTED]
    assert REMOTE_COMMAND_TRANSITIONS[RemoteCommandStatus.RUNNING] >= {
        RemoteCommandStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED,
        RemoteCommandStatus.CANCELLED,
        RemoteCommandStatus.UNKNOWN,
    }
    assert REMOTE_COMMAND_TRANSITIONS[RemoteCommandStatus.UNKNOWN] >= {
        RemoteCommandStatus.RUNNING,
        RemoteCommandStatus.SUCCEEDED,
        RemoteCommandStatus.FAILED,
    }
    assert TERMINAL_REMOTE_COMMAND_STATUSES.isdisjoint(REMOTE_COMMAND_TRANSITIONS)


def test_remote_command_enforces_timeline_terminal_and_lease_coherence() -> None:
    command = _command()
    assert RemoteCommand.model_validate_json(command.model_dump_json()) == command

    with pytest.raises(ValidationError, match="later than created_at"):
        _command(expires_at=NOW)
    with pytest.raises(ValidationError, match="chronological order"):
        _command(dispatched_at=LATER, acknowledged_at=NOW + timedelta(minutes=1))
    with pytest.raises(ValidationError, match="terminal command status"):
        _command(status=RemoteCommandStatus.SUCCEEDED)
    with pytest.raises(ValidationError, match="terminal command status"):
        _command(completed_at=NOW + timedelta(minutes=1))
    with pytest.raises(ValidationError, match="set together"):
        _command(reservation_id=RESERVATION_ID)
    with pytest.raises(ValidationError, match="set together"):
        _command(lease_version=1)

    terminal = _command(
        status=RemoteCommandStatus.SUCCEEDED,
        reservation_id=RESERVATION_ID,
        lease_version=3,
        dispatched_at=NOW + timedelta(seconds=1),
        acknowledged_at=NOW + timedelta(seconds=2),
        started_at=NOW + timedelta(seconds=3),
        completed_at=NOW + timedelta(seconds=4),
    )
    assert terminal.status in TERMINAL_REMOTE_COMMAND_STATUSES


def test_distributed_operation_and_journal_terminal_timelines_are_coherent() -> None:
    assert DISTRIBUTED_OPERATION_TRANSITIONS[DistributedOperationStatus.UNKNOWN] >= {
        DistributedOperationStatus.RECONCILING,
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
    }
    assert DISTRIBUTED_OPERATION_TRANSITIONS[DistributedOperationStatus.RECONCILING] >= {
        DistributedOperationStatus.RUNNING,
        DistributedOperationStatus.SUCCEEDED,
        DistributedOperationStatus.FAILED,
    }
    assert TERMINAL_DISTRIBUTED_OPERATION_STATUSES.isdisjoint(DISTRIBUTED_OPERATION_TRANSITIONS)

    operation = DistributedOperation(
        remote_command_id=COMMAND_ID,
        agent_id=AGENT_ID,
        bench_id="home-lab/esp32-01",
        operation_type="RUN_WORKFLOW",
        status=DistributedOperationStatus.UNKNOWN,
        result={"steps_completed": 2},
        created_at=NOW,
        last_agent_update_at=NOW + timedelta(seconds=2),
        reconciliation_deadline=LATER,
    )
    assert operation.status is DistributedOperationStatus.UNKNOWN
    assert operation.model_dump(mode="json")["result"] == {"steps_completed": 2}
    assert DistributedOperation.model_validate_json(operation.model_dump_json()) == operation
    assert DistributedOperationStatus.UNKNOWN not in TERMINAL_DISTRIBUTED_OPERATION_STATUSES

    with pytest.raises(ValidationError, match="terminal operation status"):
        DistributedOperation.model_validate(
            {**operation.model_dump(), "status": DistributedOperationStatus.SUCCEEDED}
        )
    with pytest.raises(ValidationError, match="cannot be earlier"):
        DistributedOperation.model_validate(
            {
                **operation.model_dump(),
                "last_agent_update_at": NOW - timedelta(seconds=1),
            }
        )

    completed_at = NOW + timedelta(seconds=3)
    journal = CommandJournalEntry(
        command_id=COMMAND_ID,
        idempotency_key="ci-session:42:workflow",
        command_type=RemoteCommandType.RUN_WORKFLOW,
        bench_id="home-lab/esp32-01",
        status=RemoteCommandStatus.SUCCEEDED,
        received_at=NOW,
        started_at=NOW + timedelta(seconds=1),
        completed_at=completed_at,
        result={"workflow_status": "passed"},
    )
    assert CommandJournalEntry.model_validate_json(journal.model_dump_json()) == journal
    with pytest.raises(ValidationError, match="terminal journal status"):
        CommandJournalEntry.model_validate(
            {**journal.model_dump(), "status": RemoteCommandStatus.RUNNING}
        )
    with pytest.raises(ValidationError, match="chronological order"):
        CommandJournalEntry.model_validate(
            {**journal.model_dump(), "started_at": completed_at + timedelta(seconds=1)}
        )


def test_reconciliation_report_round_trips_and_bounds_every_collection() -> None:
    state = ReconciliationCommandState(
        command_id=COMMAND_ID,
        status=RemoteCommandStatus.RUNNING,
        updated_at=NOW,
        result={"progress": 30},
    )
    report = ReconciliationReport(
        agent_id=AGENT_ID,
        boot_id=BOOT_ID,
        generated_at=NOW,
        active_commands=(state,),
        recent_commands=(),
        local_reservation_leases=(_lease(),),
        bench_snapshots=(_reconciliation_bench(),),
        buffered_event_count=2,
    )

    assert ReconciliationReport.model_validate_json(report.model_dump_json()) == report
    assert report.active_commands[0].command_id == COMMAND_ID
    assert report.local_reservation_leases[0].lease_version == 3
    assert report.bench_snapshots[0].local_bench_id == "esp32-01"

    for field in (
        "active_commands",
        "recent_commands",
        "local_reservation_leases",
        "bench_snapshots",
    ):
        if field == "local_reservation_leases":
            item: object = _lease()
        elif field == "bench_snapshots":
            item = _reconciliation_bench()
        else:
            item = state
        with pytest.raises(ValidationError):
            ReconciliationReport.model_validate({**report.model_dump(), field: [item] * 10_001})
    with pytest.raises(ValidationError):
        ReconciliationReport.model_validate({**report.model_dump(), "buffered_event_count": -1})


def test_buffered_events_have_stable_identity_order_priority_and_bounded_fields() -> None:
    event = BufferedAgentEvent(
        id=uuid4(),
        agent_id=AGENT_ID,
        sequence_number=1,
        event_type="OPERATION_SUCCEEDED",
        payload={"command_id": str(COMMAND_ID)},
        priority=BufferedEventPriority.TERMINAL,
        created_at=NOW,
    )
    assert BufferedAgentEvent.model_validate_json(event.model_dump_json()) == event
    assert BufferedEventPriority.TERMINAL > BufferedEventPriority.FAILURE
    assert BufferedEventPriority.FAILURE > BufferedEventPriority.PROGRESS

    with pytest.raises(ValidationError):
        BufferedAgentEvent.model_validate({**event.model_dump(), "sequence_number": 0})
    with pytest.raises(ValidationError):
        BufferedAgentEvent.model_validate({**event.model_dump(), "event_type": "x" * 201})


def test_artifact_metadata_and_transfer_validate_digests_and_completion() -> None:
    artifact = RemoteArtifactMetadata(
        agent_id=AGENT_ID,
        local_artifact_id=uuid4(),
        command_id=COMMAND_ID,
        name="hardware-results.xml",
        artifact_type="junit",
        content_type="application/xml",
        size_bytes=4096,
        sha256="A" * 64,
        created_at=NOW,
    )
    assert artifact.sha256 == "a" * 64

    transfer = ArtifactTransferRecord(
        agent_id=AGENT_ID,
        artifact_id=artifact.id,
        direction=ArtifactTransferDirection.AGENT_TO_CONTROL_PLANE,
        status=ArtifactTransferStatus.COMPLETED,
        token_hash="B" * 64,
        created_at=NOW,
        expires_at=LATER,
        completed_at=NOW + timedelta(seconds=2),
        expected_sha256=artifact.sha256,
        expected_size_bytes=artifact.size_bytes,
        attempt_count=1,
    )
    assert transfer.token_hash == "b" * 64
    assert "plaintext-token" not in transfer.model_dump_json()

    with pytest.raises(ValidationError, match="64-character"):
        RemoteArtifactMetadata.model_validate({**artifact.model_dump(), "sha256": "z" * 64})
    with pytest.raises(ValidationError, match="later than created_at"):
        ArtifactTransferRecord.model_validate({**transfer.model_dump(), "expires_at": NOW})
    with pytest.raises(ValidationError, match="requires completed_at"):
        ArtifactTransferRecord.model_validate({**transfer.model_dump(), "completed_at": None})

    attempt = ArtifactTransferAttempt(
        transfer_id=transfer.id,
        attempt_number=1,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        bytes_transferred=artifact.size_bytes,
        sha256=artifact.sha256,
    )
    assert ArtifactTransferAttempt.model_validate_json(attempt.model_dump_json()) == attempt
    with pytest.raises(ValidationError, match="cannot be earlier"):
        ArtifactTransferAttempt.model_validate(
            {**attempt.model_dump(), "completed_at": NOW - timedelta(seconds=1)}
        )


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (RemoteCommand, "payload", {"blob": "x" * 2_100_000}),
        (DistributedOperation, "result", {"blob": "x" * 2_100_000}),
        (CommandJournalEntry, "result", {"blob": "x" * 2_100_000}),
        (BufferedAgentEvent, "payload", {"blob": "x" * 2_100_000}),
    ],
)
def test_untrusted_distributed_payloads_are_bounded(
    model: (
        type[RemoteCommand]
        | type[DistributedOperation]
        | type[CommandJournalEntry]
        | type[BufferedAgentEvent]
    ),
    field: str,
    value: dict[str, Any],
) -> None:
    if model is RemoteCommand:
        raw = _command().model_dump()
    elif model is DistributedOperation:
        raw = DistributedOperation(
            remote_command_id=COMMAND_ID,
            agent_id=AGENT_ID,
            bench_id="home-lab/esp32-01",
            operation_type="RUN_WORKFLOW",
            created_at=NOW,
        ).model_dump()
    elif model is CommandJournalEntry:
        raw = CommandJournalEntry(
            command_id=COMMAND_ID,
            idempotency_key="workflow:42",
            command_type=RemoteCommandType.RUN_WORKFLOW,
            bench_id="home-lab/esp32-01",
            status=RemoteCommandStatus.RUNNING,
            received_at=NOW,
        ).model_dump()
    else:
        raw = BufferedAgentEvent(
            agent_id=AGENT_ID,
            sequence_number=1,
            event_type="OPERATION_PROGRESS",
            created_at=NOW,
        ).model_dump()
    with pytest.raises(ValidationError, match="payload|result|size|large"):
        model.model_validate({**raw, field: value})


NOW_MODELS = datetime(2026, 7, 23, 12, tzinfo=UTC)


def test_phase4_ci_models_normalize_values_and_preserve_outcome() -> None:
    offset_now = NOW_MODELS.astimezone(timezone(timedelta(hours=3)))
    request = BenchRequest(
        required_capabilities={" Firmware ", "SERIAL"},
        required_labels={" board ": " esp32 "},
        preferred_labels={"location": "lab-a"},
    )
    session = CiSession(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="12345",
        requested_by="github-actions",
        status=CiSessionStatus.COMPLETED,
        outcome=CiOutcome.SUCCEEDED,
        created_at=offset_now,
        completed_at=offset_now,
        cleanup_status=CleanupStatus.SUCCEEDED,
        bench_request=request,
    )

    assert request.required_capabilities == {"firmware", "serial"}
    assert request.required_labels == {"board": "esp32"}
    assert session.created_at == NOW_MODELS
    assert session.status is CiSessionStatus.COMPLETED
    assert session.outcome is CiOutcome.SUCCEEDED
    assert session.bench_request == request
    assert ReservationSource.CI.value == "ci"


def test_phase4_token_artifact_cleanup_and_test_result_models() -> None:
    token = ApiToken(
        name="github-demo",
        token_hash="a" * 64,
        owner="github-actions",
        scopes={ApiTokenScope.CI_SESSIONS, ApiTokenScope.ARTIFACTS_WRITE},
        created_at=NOW_MODELS,
        expires_at=NOW_MODELS + timedelta(days=1),
    )
    artifact = ArtifactRecord(
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=uuid4(),
        name="firmware.bin",
        artifact_type="firmware",
        content_type="application/octet-stream",
        path="objects/ab/cd",
        size_bytes=42,
        sha256="b" * 64,
        created_at=NOW_MODELS,
        metadata={"board": "esp32"},
    )
    cleanup = CleanupResult(
        reservation_released=True,
        workflow_stopped=True,
        locks_released=True,
        serial_closed=True,
        artifacts_finalized=True,
    )
    result = HardwareTestResult(
        name="ESP32 boot verification",
        status=HardwareTestStatus.PASSED,
        duration_ms=4271,
        details={"firmware_version": "0.5.0"},
    )

    assert token.scopes == {
        ApiTokenScope.CI_SESSIONS,
        ApiTokenScope.ARTIFACTS_WRITE,
    }
    assert artifact.owner_type is ArtifactOwnerType.CI_SESSION
    assert cleanup.errors == []
    assert result.status is HardwareTestStatus.PASSED


def test_phase4_models_reject_unsafe_or_incoherent_values() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        BenchRequest(allow_simulated=False, allow_physical=False)
    with pytest.raises(ValidationError, match="expires_at must be later"):
        ApiToken(
            name="expired",
            token_hash="a" * 64,
            owner="ci",
            scopes={ApiTokenScope.CI_SESSIONS},
            created_at=NOW_MODELS,
            expires_at=NOW_MODELS,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        CiSession(
            provider=CiProvider.LOCAL,
            external_run_id="local-1",
            requested_by="developer",
            created_at=NOW_MODELS.replace(tzinfo=None),
        )
    with pytest.raises(ValidationError, match="String should match pattern"):
        ArtifactRecord(
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="trace.log",
            artifact_type="trace",
            path="objects/trace",
            size_bytes=1,
            sha256="not-a-checksum",
        )


NOW_AGENT_MODELS = datetime(2026, 7, 26, 12, tzinfo=UTC)


def test_agent_identity_normalizes_metadata_and_preserves_canonical_id() -> None:
    agent_id = uuid4()
    offset_now = NOW_AGENT_MODELS.astimezone(timezone(timedelta(hours=3)))
    agent = AgentRecord(
        id=agent_id,
        slug=" Home-Lab-1234 ",
        name="Home Lab",
        status=AgentStatus.OFFLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location="jerusalem-home",
        labels={" environment ": " development ", "owner": "michael"},
        registered_at=offset_now,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )

    assert agent.id == agent_id
    assert agent.slug == "home-lab-1234"
    assert agent.registered_at == NOW_AGENT_MODELS
    assert agent.labels == {"environment": "development", "owner": "michael"}
    assert AgentStatus.DRAINING.value == "DRAINING"
    assert AgentStatus.DRAINED.value == "DRAINED"


def test_agent_identity_rejects_invalid_timeline_and_partial_revocation() -> None:
    common = {
        "slug": "home-lab-1234",
        "name": "Home Lab",
        "version": "0.6.0-alpha",
        "protocol_version": "1.0",
        "registered_at": NOW_AGENT_MODELS,
    }
    with pytest.raises(ValidationError, match="requires last_connected_at"):
        AgentRecord.model_validate(
            {**common, "last_seen_at": NOW_AGENT_MODELS + timedelta(seconds=1)}
        )
    with pytest.raises(ValidationError, match="must be set together"):
        AgentRecord.model_validate(
            {
                **common,
                "status": AgentStatus.REVOKED,
                "enrollment_status": EnrollmentStatus.ENROLLED,
                "revoked_at": NOW_AGENT_MODELS,
            }
        )
    with pytest.raises(ValidationError, match="internal hyphens"):
        AgentRecord.model_validate({**common, "slug": "not_valid"})


def test_enrollment_token_requires_bounded_single_use_metadata() -> None:
    agent_id = uuid4()
    request_id = uuid4()
    token = AgentEnrollmentToken(
        name="home-lab",
        token_hash="A" * 64,
        created_at=NOW_AGENT_MODELS,
        expires_at=NOW_AGENT_MODELS + timedelta(minutes=30),
        used_at=NOW_AGENT_MODELS + timedelta(minutes=1),
        used_by_agent_id=agent_id,
        enrollment_request_id=request_id,
        allowed_labels={" environment ": " development "},
    )

    assert token.token_hash == "a" * 64
    assert token.allowed_labels == {"environment": "development"}
    assert token.used_by_agent_id == agent_id

    with pytest.raises(ValidationError, match="must be set together"):
        AgentEnrollmentToken.model_validate(
            {
                **token.model_dump(),
                "used_by_agent_id": None,
            }
        )
    with pytest.raises(ValidationError, match="later than created_at"):
        AgentEnrollmentToken(
            name="expired",
            token_hash="b" * 64,
            created_at=NOW_AGENT_MODELS,
            expires_at=NOW_AGENT_MODELS,
        )
    with pytest.raises(ValidationError, match="SHA-256"):
        AgentEnrollmentToken(
            name="bad-hash",
            token_hash="not-a-hash",
            created_at=NOW_AGENT_MODELS,
            expires_at=NOW_AGENT_MODELS + timedelta(minutes=1),
        )


def test_agent_credential_validates_version_hash_and_lifecycle() -> None:
    credential = AgentCredential(
        agent_id=uuid4(),
        kind=AgentCredentialKind.OPAQUE_TOKEN,
        credential_hash="C" * 64,
        version=2,
        created_at=NOW_AGENT_MODELS,
        expires_at=NOW_AGENT_MODELS + timedelta(days=1),
        last_used_at=NOW_AGENT_MODELS + timedelta(minutes=1),
    )
    assert credential.credential_hash == "c" * 64
    assert credential.version == 2

    with pytest.raises(ValidationError, match="later than created_at"):
        AgentCredential(
            agent_id=credential.agent_id,
            credential_hash="d" * 64,
            created_at=NOW_AGENT_MODELS,
            expires_at=NOW_AGENT_MODELS,
        )
    with pytest.raises(ValidationError):
        AgentCredential(
            agent_id=credential.agent_id,
            credential_hash="d" * 64,
            version=True,
            created_at=NOW_AGENT_MODELS,
        )


def test_models_are_strict_frozen_and_timestamped() -> None:
    bench = Bench(name=" bench-01 ")
    assert bench.name == "bench-01"
    with pytest.raises(ValidationError):
        Bench.model_validate({"name": "bench", "unknown": True})
    with pytest.raises(ValidationError):
        bench.name = "changed"
    assert Event(type="Ready").timestamp.tzinfo is not None


def test_structured_logging_has_required_fields_and_exception() -> None:
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        "agent",
        logging.INFO,
        __file__,
        1,
        "ready %s",
        ("now",),
        None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["component"] == "agent"
    assert payload["level"] == "INFO"
    assert payload["message"] == "ready now"
    assert payload["timestamp"].endswith("+00:00")

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exception_record = logging.LogRecord(
            "agent",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            __import__("sys").exc_info(),
        )
    assert "RuntimeError: boom" in formatter.format(exception_record)


def test_get_logger_is_idempotent_and_updates_level() -> None:
    logger = get_logger("tests.structured", "INFO")
    initial_handlers = list(logger.handlers)
    same_logger = get_logger("tests.structured", "DEBUG")

    assert same_logger is logger
    assert logger.level == logging.DEBUG
    assert logger.propagate is False
    assert logger.handlers == initial_handlers
    assert initial_handlers[0].level == logging.DEBUG
    logger.handlers.clear()


def test_simlab_lifecycle_and_deterministic_patterns() -> None:
    async def scenario() -> None:
        simlab = SimLab(bench_count=6)
        assert simlab.benches() == []

        await simlab.start()
        benches = simlab.benches()
        assert [bench.name for bench in benches] == [
            "bench-01",
            "bench-02",
            "bench-03",
            "bench-04",
            "bench-05",
            "bench-06",
        ]
        assert benches[0].capabilities == ["Power", "Serial", "Firmware"]
        assert benches[2].capabilities[-1] == "Debugger"
        assert benches[5].capabilities == benches[0].capabilities
        assert benches[0].devices[0].name == "bench-01-controller"

        await simlab.shutdown()
        assert bool(simlab.started) is False
        assert simlab.benches() == []

        disabled = SimLab(enabled=False)
        await disabled.start()
        assert bool(disabled.started) is True
        assert disabled.benches() == []

    import asyncio

    asyncio.run(scenario())
