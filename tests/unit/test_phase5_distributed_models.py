from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from lab_platform.models import (
    DISTRIBUTED_OPERATION_TRANSITIONS,
    REMOTE_COMMAND_TRANSITIONS,
    TERMINAL_DISTRIBUTED_OPERATION_STATUSES,
    TERMINAL_REMOTE_COMMAND_STATUSES,
    AgentConnectionRecord,
    ArtifactTransferAttempt,
    ArtifactTransferDirection,
    ArtifactTransferRecord,
    ArtifactTransferStatus,
    BufferedAgentEvent,
    BufferedEventPriority,
    CommandJournalEntry,
    DistributedOperation,
    DistributedOperationStatus,
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
)
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
