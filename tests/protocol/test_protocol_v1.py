from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import (
    AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES,
    CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES,
    PROTOCOL_VERSION,
    SUPPORTED_MESSAGE_TYPES,
    ActorAttributedControlPayload,
    AgentHeartbeatEnvelope,
    AgentHelloEnvelope,
    AgentStatus,
    BenchSnapshotEnvelope,
    CommandCancelPayload,
    DrainAgentPayload,
    EventAckEnvelope,
    EventAckPayload,
    IncomingSequenceTracker,
    InventoryRefreshRequestPayload,
    MessageDirection,
    MessageType,
    ProtocolMessageInvalidError,
    ProtocolSequenceError,
    ProtocolVersion,
    ProtocolVersionUnsupportedError,
    ReconciliationRequestPayload,
    SequenceDisposition,
    WelcomeEnvelope,
    negotiate_protocol_version,
    parse_agent_message,
    parse_control_plane_message,
    parse_envelope,
    validate_message_direction,
)
from lab_platform.agent_protocol.envelopes import ArtifactUploadRequestEnvelope
from lab_platform.models import ActorContext, PrincipalType
from pydantic import SecretStr, ValidationError

AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")


BOOT_ID = UUID("1ab59e13-5517-482f-849c-2fe8145db25c")


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


def _base_envelope(
    message_type: MessageType,
    payload: dict[str, Any],
    *,
    sequence_number: int = 1,
    message_id: UUID | None = None,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    return {
        "protocol_version": protocol_version,
        "message_id": str(message_id or uuid4()),
        "message_type": message_type.value,
        "agent_id": str(AGENT_ID),
        "sent_at": NOW.isoformat(),
        "correlation_id": None,
        "sequence_number": sequence_number,
        "payload": payload,
    }


def _hello_envelope(
    *,
    sequence_number: int = 1,
    message_id: UUID | None = None,
    protocol_version: str = PROTOCOL_VERSION,
) -> AgentHelloEnvelope:
    parsed = parse_agent_message(
        _base_envelope(
            MessageType.AGENT_HELLO,
            {
                "agent_version": "0.6.0-alpha",
                "protocol_version": protocol_version,
                "agent_name": "home-lab",
                "boot_id": str(BOOT_ID),
                "capabilities": ["remote_operations"],
                "last_acknowledged_command_sequence": 0,
            },
            sequence_number=sequence_number,
            message_id=message_id,
            protocol_version=protocol_version,
        )
    )
    assert isinstance(parsed, AgentHelloEnvelope)
    return parsed


def test_protocol_version_negotiates_minor_and_rejects_other_majors() -> None:
    assert ProtocolVersion.parse("1.12") == ProtocolVersion(major=1, minor=12)
    assert str(ProtocolVersion.parse("1.12")) == "1.12"
    assert negotiate_protocol_version("1.4") == "1.0"
    assert negotiate_protocol_version("1.4", local_version="1.7") == "1.4"

    with pytest.raises(ProtocolVersionUnsupportedError) as incompatible:
        negotiate_protocol_version("2.0")
    assert incompatible.value.code == "PROTOCOL_VERSION_UNSUPPORTED"
    assert incompatible.value.details == {"local_version": "1.0", "remote_version": "2.0"}

    for malformed in ("1", "v1.0", "01.0", "1.-1"):
        with pytest.raises(ProtocolVersionUnsupportedError):
            ProtocolVersion.parse(malformed)
    with pytest.raises(ProtocolVersionUnsupportedError, match="must be a string"):
        ProtocolVersion.parse(cast(Any, 1))
    with pytest.raises(ValueError, match="cannot be negative"):
        ProtocolVersion(major=-1, minor=0)
    with pytest.raises(TypeError, match="must be integers"):
        ProtocolVersion(major=cast(Any, True), minor=0)


def test_hello_round_trip_validates_payload_and_ignores_new_minor_fields() -> None:
    offset_time = NOW.astimezone(timezone(timedelta(hours=3)))
    raw = _base_envelope(
        MessageType.AGENT_HELLO,
        {
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.2",
            "agent_name": " home-lab ",
            "boot_id": str(BOOT_ID),
            "capabilities": [" Remote_Operations ", "ARTIFACT_UPLOAD", "artifact_upload"],
            "last_acknowledged_command_sequence": 148,
            "future_optional_payload_field": True,
        },
        protocol_version="1.2",
    )
    raw["sent_at"] = offset_time.isoformat()
    raw["future_optional_envelope_field"] = {"enabled": True}

    envelope = parse_agent_message(raw)

    assert isinstance(envelope, AgentHelloEnvelope)
    assert envelope.sent_at == NOW
    assert envelope.payload.agent_name == "home-lab"
    assert envelope.payload.capabilities == frozenset({"remote_operations", "artifact_upload"})
    assert envelope.payload.last_acknowledged_command_sequence == 148
    dumped = envelope.model_dump(mode="json")
    assert "future_optional_envelope_field" not in dumped
    assert "future_optional_payload_field" not in cast(dict[str, Any], dumped["payload"])
    decoded = json.loads(envelope.model_dump_json())
    assert parse_agent_message(decoded) == envelope


def test_hello_rejects_mismatched_versions_and_invalid_payload() -> None:
    mismatched = _base_envelope(
        MessageType.AGENT_HELLO,
        {
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.1",
            "agent_name": "home-lab",
            "boot_id": str(BOOT_ID),
            "capabilities": [],
        },
    )
    with pytest.raises(ProtocolMessageInvalidError, match="versions differ"):
        parse_agent_message(mismatched)

    invalid_capability = _base_envelope(
        MessageType.AGENT_HELLO,
        {
            "agent_version": "0.6.0-alpha",
            "protocol_version": "1.0",
            "agent_name": "home-lab",
            "boot_id": str(BOOT_ID),
            "capabilities": [""],
        },
    )
    with pytest.raises(ProtocolMessageInvalidError) as invalid:
        parse_agent_message(invalid_capability)
    assert invalid.value.code == "PROTOCOL_MESSAGE_INVALID"
    assert "validation_errors" in invalid.value.details


def test_heartbeat_requires_matching_identity_and_coherent_counts() -> None:
    payload = {
        "agent_id": str(AGENT_ID),
        "boot_id": str(BOOT_ID),
        "uptime_seconds": 60,
        "active_operations": 1,
        "connected_benches": 3,
        "degraded_benches": 1,
        "event_buffer_size": 2,
        "timestamp": NOW.isoformat(),
    }
    envelope = parse_agent_message(_base_envelope(MessageType.AGENT_HEARTBEAT, payload))
    assert isinstance(envelope, AgentHeartbeatEnvelope)
    assert envelope.payload.timestamp == NOW

    wrong_agent = {**payload, "agent_id": str(uuid4())}
    with pytest.raises(ProtocolMessageInvalidError, match="Agent IDs differ"):
        parse_agent_message(_base_envelope(MessageType.AGENT_HEARTBEAT, wrong_agent))

    impossible_counts = {**payload, "connected_benches": 1, "degraded_benches": 2}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(_base_envelope(MessageType.AGENT_HEARTBEAT, impossible_counts))

    naive_timestamp = {**payload, "timestamp": NOW.replace(tzinfo=None).isoformat()}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(_base_envelope(MessageType.AGENT_HEARTBEAT, naive_timestamp))

    coerced_counter = {**payload, "active_operations": "sensitive-counter"}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed") as invalid:
        parse_agent_message(_base_envelope(MessageType.AGENT_HEARTBEAT, coerced_counter))
    assert "sensitive-counter" not in repr(invalid.value.details)


def test_inventory_snapshot_has_agent_authoritative_fields_and_unique_local_ids() -> None:
    bench = {
        "local_bench_id": "esp32-devkit-01",
        "name": "Desk ESP32",
        "backend_id": "local-hardware",
        "kind": "physical",
        "target_type": "esp32-devkit-v1",
        "connectivity": "online",
        "health": "healthy",
        "capabilities": [" Firmware ", "SERIAL", "serial"],
        "labels": {" board ": " esp32 ", "location": "home"},
        "firmware_version": "0.6.0",
    }
    payload = {
        "boot_id": str(BOOT_ID),
        "generated_at": NOW.isoformat(),
        "benches": [bench],
    }
    envelope = parse_agent_message(_base_envelope(MessageType.BENCH_SNAPSHOT, payload))

    assert isinstance(envelope, BenchSnapshotEnvelope)
    assert envelope.payload.benches[0].kind.value == "physical"
    assert envelope.payload.benches[0].target_type == "esp32-devkit-v1"
    assert envelope.payload.benches[0].capabilities == frozenset({"firmware", "serial"})
    assert envelope.payload.benches[0].labels == {"board": "esp32", "location": "home"}

    duplicate_payload = {**payload, "benches": [bench, bench]}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(_base_envelope(MessageType.BENCH_SNAPSHOT, duplicate_payload))

    bad_labels = {**bench, "labels": {"": "esp32"}}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _base_envelope(MessageType.BENCH_SNAPSHOT, {**payload, "benches": [bad_labels]})
        )

    colliding_labels = {**bench, "labels": {"board": "esp32", " board ": "stm32"}}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _base_envelope(
                MessageType.BENCH_SNAPSHOT,
                {**payload, "benches": [colliding_labels]},
            )
        )

    too_many_capabilities = {**bench, "capabilities": [f"capability-{i}" for i in range(257)]}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _base_envelope(
                MessageType.BENCH_SNAPSHOT,
                {**payload, "benches": [too_many_capabilities]},
            )
        )

    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _base_envelope(
                MessageType.BENCH_SNAPSHOT,
                {**payload, "benches": [{}] * 10_001},
            )
        )


def test_welcome_carries_negotiated_version_and_liveness_policy() -> None:
    payload = {
        "connection_id": str(uuid4()),
        "accepted_protocol_version": "1.0",
        "server_time": NOW.isoformat(),
        "heartbeat_interval_seconds": 15,
        "heartbeat_timeout_seconds": 45,
        "offline_timeout_seconds": 90,
    }
    envelope = parse_control_plane_message(_base_envelope(MessageType.WELCOME, payload))
    assert isinstance(envelope, WelcomeEnvelope)
    assert envelope.payload.accepted_protocol_version == "1.0"

    mismatched = {**payload, "accepted_protocol_version": "1.1"}
    with pytest.raises(ProtocolMessageInvalidError, match="negotiated protocol version"):
        parse_control_plane_message(_base_envelope(MessageType.WELCOME, mismatched))

    with pytest.raises(ProtocolMessageInvalidError, match="negotiated protocol version"):
        parse_control_plane_message(
            _base_envelope(MessageType.WELCOME, payload, protocol_version="1.3")
        )

    incoherent = {**payload, "heartbeat_timeout_seconds": 10}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_control_plane_message(_base_envelope(MessageType.WELCOME, incoherent))

    equal_timeouts = {
        **payload,
        "heartbeat_interval_seconds": 15,
        "heartbeat_timeout_seconds": 15,
    }
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_control_plane_message(_base_envelope(MessageType.WELCOME, equal_timeouts))

    equal_offline_timeout = {**payload, "offline_timeout_seconds": 45}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_control_plane_message(_base_envelope(MessageType.WELCOME, equal_offline_timeout))


def test_message_vocabulary_and_direction_are_explicit() -> None:
    expected_agent_messages = {
        "AGENT_HELLO",
        "AGENT_HEARTBEAT",
        "AGENT_STATUS",
        "BENCH_SNAPSHOT",
        "BENCH_ADDED",
        "BENCH_REMOVED",
        "BENCH_HEALTH_CHANGED",
        "COMMAND_ACCEPTED",
        "COMMAND_REJECTED",
        "OPERATION_STARTED",
        "OPERATION_PROGRESS",
        "OPERATION_SUCCEEDED",
        "OPERATION_FAILED",
        "OPERATION_CANCELLED",
        "WORKFLOW_PROGRESS",
        "ARTIFACT_CREATED",
        "EVENT_BATCH",
        "RECONCILIATION_REPORT",
    }
    expected_control_plane_messages = {
        "WELCOME",
        "EVENT_ACK",
        "COMMAND_REQUEST",
        "COMMAND_CANCEL",
        "INVENTORY_REFRESH_REQUEST",
        "RECONCILIATION_REQUEST",
        "ARTIFACT_UPLOAD_REQUEST",
        "CONFIG_REFRESH_REQUEST",
        "DRAIN_AGENT",
        "RESERVATION_ACTIVATED",
        "RESERVATION_RELEASED",
    }
    assert {item.value for item in AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES} == (
        expected_agent_messages
    )
    assert {item.value for item in CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES} == (
        expected_control_plane_messages
    )
    assert AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES.isdisjoint(CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES)
    assert frozenset(MessageType) == (
        AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES | CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES
    )
    assert {
        AgentStatus.DRAINING,
        AgentStatus.DRAINED,
        AgentStatus.INCOMPATIBLE,
    }.issubset(frozenset(AgentStatus))
    assert frozenset(MessageType) == SUPPORTED_MESSAGE_TYPES

    validate_message_direction(
        MessageType.AGENT_HELLO,
        MessageDirection.AGENT_TO_CONTROL_PLANE,
    )
    validate_message_direction(
        MessageType.WELCOME,
        MessageDirection.CONTROL_PLANE_TO_AGENT,
    )
    with pytest.raises(ProtocolMessageInvalidError, match="wrong direction"):
        validate_message_direction(
            MessageType.WELCOME,
            MessageDirection.AGENT_TO_CONTROL_PLANE,
        )
    with pytest.raises(ProtocolMessageInvalidError, match="wrong direction"):
        parse_envelope(
            _hello_envelope().model_dump(mode="json"),
            expected_direction=MessageDirection.CONTROL_PLANE_TO_AGENT,
        )


def test_unknown_and_malformed_messages_fail_clearly() -> None:
    with pytest.raises(ProtocolMessageInvalidError, match="must be a string"):
        parse_agent_message({})

    unknown = _base_envelope(MessageType.AGENT_HELLO, {})
    unknown["message_type"] = "FUTURE_REQUIRED_MESSAGE"
    with pytest.raises(ProtocolMessageInvalidError, match="Unknown") as error:
        parse_agent_message(unknown)
    assert error.value.details["message_type"] == "FUTURE_REQUIRED_MESSAGE"

    invalid_sequence = _hello_envelope().model_dump(mode="json")
    invalid_sequence["sequence_number"] = 0
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(invalid_sequence)

    invalid_sequence["sequence_number"] = True
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(invalid_sequence)

    incompatible = _hello_envelope().model_dump(mode="json")
    incompatible["protocol_version"] = "2.0"
    with pytest.raises(ProtocolVersionUnsupportedError):
        parse_agent_message(incompatible)


@pytest.mark.parametrize("value", [None, [], "message", 7])
def test_non_object_protocol_messages_use_the_stable_error(value: object) -> None:
    with pytest.raises(ProtocolMessageInvalidError, match="JSON object") as error:
        parse_agent_message(value)
    assert error.value.code == "PROTOCOL_MESSAGE_INVALID"
    assert error.value.details["received_type"] == type(value).__name__


def test_sequence_tracker_accepts_duplicates_and_rejects_gaps_and_replays() -> None:
    first_id = uuid4()
    first = _hello_envelope(sequence_number=1, message_id=first_id)
    second = _hello_envelope(sequence_number=2)
    tracker = IncomingSequenceTracker(max_recent_message_ids=1)

    assert tracker.observe(first) is SequenceDisposition.ACCEPTED
    conflicting = first.model_copy(update={"sequence_number": 2})
    with pytest.raises(ProtocolSequenceError, match="reused with different content") as reuse:
        tracker.observe(conflicting)
    assert reuse.value.details["original_sequence"] == 1
    assert reuse.value.details["received_sequence"] == 2
    assert tracker.observe(first) is SequenceDisposition.DUPLICATE
    assert tracker.observe(second) is SequenceDisposition.ACCEPTED
    assert tracker.last_accepted_sequence == 2

    replay = _hello_envelope(sequence_number=1, message_id=first_id)
    with pytest.raises(ProtocolSequenceError, match="replay") as error:
        tracker.observe(replay)
    assert error.value.code == "PROTOCOL_SEQUENCE_ERROR"
    assert error.value.details["expected_sequence"] == 3

    gap = _hello_envelope(sequence_number=4)
    with pytest.raises(ProtocolSequenceError, match="gap"):
        tracker.observe(gap)

    with pytest.raises(ValueError, match="expected_sequence"):
        IncomingSequenceTracker(expected_sequence=0)
    with pytest.raises(ValueError, match="max_recent_message_ids"):
        IncomingSequenceTracker(max_recent_message_ids=0)


OTHER_AGENT_ID = UUID("4c4e5910-bfe4-4cb7-a3e3-f40098f3be65")


COMMAND_ID = UUID("10f76299-e654-41a8-a896-71f8fd9bd0b1")


RESERVATION_ID = UUID("a9e16eb6-cc01-48df-a6c7-c99ef117a002")


NOW_PROTOCOL_V1_REMAINING = datetime(2026, 7, 28, 12, tzinfo=UTC)


LATER = NOW_PROTOCOL_V1_REMAINING + timedelta(minutes=5)


CORRELATION_ID = UUID("a6f74b29-63cc-4874-96bd-7c42643bad63")


TRANSFER_SECRET = "lat_7GfQhBZ6m2cGjV89xKp4TnWaSdE1LrY"


def _bench() -> dict[str, object]:
    return {
        "local_bench_id": "esp32-01",
        "name": "Desk ESP32",
        "backend_id": "hardware",
        "kind": "physical",
        "target_type": "esp32-devkit-v1",
        "connectivity": "online",
        "health": "healthy",
        "capabilities": ["firmware", "serial"],
        "labels": {"board": "esp32"},
        "firmware_version": "0.6.0-alpha",
    }


def _lease() -> dict[str, object]:
    return {
        "reservation_id": str(RESERVATION_ID),
        "agent_id": str(AGENT_ID),
        "bench_id": "home-lab/esp32-01",
        "owner": "github-actions",
        "valid_from": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        "valid_until": LATER.isoformat(),
        "lease_version": 3,
    }


def _command(*, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "id": str(COMMAND_ID),
        "agent_id": str(AGENT_ID),
        "bench_id": "home-lab/esp32-01",
        "command_type": "RUN_WORKFLOW",
        "payload": payload or {"workflow_name": "esp32-ci-test"},
        "status": "CREATED",
        "created_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        "expires_at": LATER.isoformat(),
        "idempotency_key": "ci-session:42:workflow",
        "attempt_count": 0,
        "reservation_id": str(RESERVATION_ID),
        "lease_version": 3,
    }


def _artifact() -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "agent_id": str(AGENT_ID),
        "local_artifact_id": str(uuid4()),
        "command_id": str(COMMAND_ID),
        "operation_id": str(uuid4()),
        "name": "hardware-results.xml",
        "artifact_type": "junit",
        "content_type": "application/xml",
        "size_bytes": 4096,
        "sha256": "a" * 64,
        "created_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
    }


def _protocol_payloads() -> dict[MessageType, dict[str, object]]:
    operation_base: dict[str, object] = {
        "command_id": str(COMMAND_ID),
        "local_operation_id": str(uuid4()),
        "occurred_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
    }
    return {
        MessageType.AGENT_HELLO: {
            "agent_version": "0.6.0-alpha",
            "protocol_version": PROTOCOL_VERSION,
            "agent_name": "home-lab",
            "boot_id": str(BOOT_ID),
            "capabilities": ["remote_operations", "artifact_upload"],
            "last_acknowledged_command_sequence": 7,
        },
        MessageType.AGENT_HEARTBEAT: {
            "agent_id": str(AGENT_ID),
            "boot_id": str(BOOT_ID),
            "uptime_seconds": 60,
            "active_operations": 1,
            "connected_benches": 2,
            "degraded_benches": 0,
            "event_buffer_size": 3,
            "timestamp": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        },
        MessageType.AGENT_STATUS: {
            "agent_id": str(AGENT_ID),
            "boot_id": str(BOOT_ID),
            "status": "ONLINE",
            "changed_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "reason": "connection established",
        },
        MessageType.BENCH_SNAPSHOT: {
            "boot_id": str(BOOT_ID),
            "generated_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "benches": [_bench()],
        },
        MessageType.BENCH_ADDED: {
            "boot_id": str(BOOT_ID),
            "bench": _bench(),
            "changed_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        },
        MessageType.BENCH_REMOVED: {
            "boot_id": str(BOOT_ID),
            "local_bench_id": "esp32-01",
            "changed_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        },
        MessageType.BENCH_HEALTH_CHANGED: {
            "boot_id": str(BOOT_ID),
            "local_bench_id": "esp32-01",
            "connectivity": "degraded",
            "health": "warning",
            "changed_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        },
        MessageType.COMMAND_ACCEPTED: {
            "command_id": str(COMMAND_ID),
            "accepted_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "journal_status": "ACCEPTED",
            "local_operation_id": str(uuid4()),
        },
        MessageType.COMMAND_REJECTED: {
            "command_id": str(COMMAND_ID),
            "rejected_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "error_code": "RESERVATION_LEASE_EXPIRED",
            "error_message": "The reservation lease has expired.",
        },
        MessageType.OPERATION_STARTED: operation_base,
        MessageType.OPERATION_PROGRESS: {**operation_base, "progress": 50, "message": "flashing"},
        MessageType.OPERATION_SUCCEEDED: {
            **operation_base,
            "progress": 100,
            "result": {"status": "passed"},
        },
        MessageType.OPERATION_FAILED: {
            **operation_base,
            "error_code": "FLASH_FAILED",
            "error_message": "Target did not respond.",
        },
        MessageType.OPERATION_CANCELLED: {**operation_base, "message": "cancelled by user"},
        MessageType.WORKFLOW_PROGRESS: {
            "command_id": str(COMMAND_ID),
            "local_workflow_run_id": str(uuid4()),
            "occurred_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "step_index": 1,
            "step_count": 5,
            "step_name": "Flash firmware",
            "progress": 40,
            "message": "writing image",
        },
        MessageType.ARTIFACT_CREATED: {"artifact": _artifact()},
        MessageType.EVENT_BATCH: {
            "events": [
                {
                    "id": str(uuid4()),
                    "agent_id": str(AGENT_ID),
                    "sequence_number": 1,
                    "event_type": "OPERATION_PROGRESS",
                    "payload": {"command_id": str(COMMAND_ID), "progress": 75},
                    "priority": 50,
                    "created_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
                }
            ]
        },
        MessageType.RECONCILIATION_REPORT: {
            "report": {
                "agent_id": str(AGENT_ID),
                "boot_id": str(BOOT_ID),
                "generated_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
                "active_commands": [
                    {
                        "command_id": str(COMMAND_ID),
                        "status": "RUNNING",
                        "updated_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
                        "result": {"progress": 75},
                    }
                ],
                "recent_commands": [],
                "local_reservation_leases": [_lease()],
                "bench_snapshots": [
                    {
                        "local_bench_id": "esp32-01",
                        "name": "Desk ESP32",
                        "backend_id": "hardware",
                        "kind": "PHYSICAL",
                        "target_type": "esp32-devkit-v1",
                        "status": "ONLINE",
                        "health": "healthy",
                        "capabilities": ["firmware", "serial"],
                        "labels": {"board": "esp32"},
                        "firmware_version": "0.6.0-alpha",
                    }
                ],
                "buffered_event_count": 1,
            }
        },
        MessageType.WELCOME: {
            "connection_id": str(uuid4()),
            "accepted_protocol_version": PROTOCOL_VERSION,
            "server_time": NOW_PROTOCOL_V1_REMAINING.isoformat(),
            "heartbeat_interval_seconds": 15,
            "heartbeat_timeout_seconds": 45,
            "offline_timeout_seconds": 90,
        },
        MessageType.EVENT_ACK: {
            "batch_message_id": str(CORRELATION_ID),
            "acknowledged_event_sequence": 17,
        },
        MessageType.COMMAND_REQUEST: {
            "command": _command(),
            "reservation_lease": _lease(),
        },
        MessageType.COMMAND_CANCEL: {
            "command_id": str(COMMAND_ID),
            "reason": "CI session cancelled",
        },
        MessageType.INVENTORY_REFRESH_REQUEST: {"request_id": str(uuid4())},
        MessageType.RECONCILIATION_REQUEST: {
            "request_id": str(uuid4()),
            "expected_boot_id": str(BOOT_ID),
            "last_control_plane_sequence": 42,
        },
        MessageType.ARTIFACT_UPLOAD_REQUEST: {
            "transfer_id": str(uuid4()),
            "artifact_id": str(uuid4()),
            "upload_url": "https://control.example.test/api/v1/artifact-transfers/upload",
            "transfer_token": TRANSFER_SECRET,
            "expires_at": LATER.isoformat(),
            "maximum_size_bytes": 500_000_000,
            "expected_sha256": "b" * 64,
        },
        MessageType.CONFIG_REFRESH_REQUEST: {"config_version": 3},
        MessageType.DRAIN_AGENT: {"drain": True, "deadline": LATER.isoformat()},
        MessageType.RESERVATION_ACTIVATED: {"lease": _lease()},
        MessageType.RESERVATION_RELEASED: {
            "reservation_id": str(RESERVATION_ID),
            "bench_id": "home-lab/esp32-01",
            "lease_version": 3,
            "released_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        },
    }


@pytest.mark.parametrize("message_type", list(MessageType))
def test_every_protocol_message_round_trips_and_rejects_the_other_direction(
    message_type: MessageType,
) -> None:
    payloads = _protocol_payloads()
    assert frozenset(payloads) == frozenset(MessageType) == SUPPORTED_MESSAGE_TYPES
    raw: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(uuid4()),
        "message_type": message_type.value,
        "agent_id": str(AGENT_ID),
        "sent_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        "correlation_id": str(CORRELATION_ID),
        "sequence_number": 1,
        "payload": payloads[message_type],
    }

    if message_type in AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES:
        parsed = parse_agent_message(raw)
        reparsed = parse_agent_message(json.loads(parsed.model_dump_json()))
        wrong_parser = parse_control_plane_message
    else:
        assert message_type in CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES
        parsed = parse_control_plane_message(raw)
        reparsed = parse_control_plane_message(json.loads(parsed.model_dump_json()))
        wrong_parser = parse_agent_message

    assert reparsed == parsed
    assert parsed.message_type is message_type
    with pytest.raises(ProtocolMessageInvalidError, match="wrong direction"):
        wrong_parser(raw)


def test_command_and_lease_identity_must_match_the_envelope_and_each_other() -> None:
    payload = _protocol_payloads()[MessageType.COMMAND_REQUEST]
    raw = _raw(MessageType.COMMAND_REQUEST, payload)
    parse_control_plane_message(raw)

    wrong_envelope = {**raw, "agent_id": str(OTHER_AGENT_ID)}
    with pytest.raises(ProtocolMessageInvalidError, match="Agent IDs differ"):
        parse_control_plane_message(wrong_envelope)

    wrong_lease = {
        **payload,
        "reservation_lease": {**_lease(), "lease_version": 4},
    }
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_control_plane_message(_raw(MessageType.COMMAND_REQUEST, wrong_lease))

    missing_lease: dict[str, object] = {"command": _command()}
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_control_plane_message(_raw(MessageType.COMMAND_REQUEST, missing_lease))


def test_event_ack_is_typed_and_requires_a_positive_strict_watermark() -> None:
    payload = _protocol_payloads()[MessageType.EVENT_ACK]
    parsed = parse_control_plane_message(_raw(MessageType.EVENT_ACK, payload))

    assert isinstance(parsed, EventAckEnvelope)
    assert isinstance(parsed.payload, EventAckPayload)
    assert parsed.payload.batch_message_id == CORRELATION_ID
    assert parsed.payload.acknowledged_event_sequence == 17

    for invalid_watermark in (0, -1, True, 1.0):
        with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
            parse_control_plane_message(
                _raw(
                    MessageType.EVENT_ACK,
                    {**payload, "acknowledged_event_sequence": invalid_watermark},
                )
            )


def test_agent_bound_batches_and_reconciliation_cannot_cross_agent_identity() -> None:
    event_payload = _protocol_payloads()[MessageType.EVENT_BATCH]
    events = cast(list[dict[str, object]], event_payload["events"])
    wrong_event = {**events[0], "agent_id": str(OTHER_AGENT_ID)}
    with pytest.raises(ProtocolMessageInvalidError, match="different Agent"):
        parse_agent_message(_raw(MessageType.EVENT_BATCH, {"events": [wrong_event]}))

    reconciliation = _protocol_payloads()[MessageType.RECONCILIATION_REPORT]
    report = cast(dict[str, object], reconciliation["report"]).copy()
    report["agent_id"] = str(OTHER_AGENT_ID)
    with pytest.raises(ProtocolMessageInvalidError, match="Agent IDs differ"):
        parse_agent_message(_raw(MessageType.RECONCILIATION_REPORT, {"report": report}))


def test_protocol_payload_coherence_rejects_invalid_status_and_progress() -> None:
    accepted = _protocol_payloads()[MessageType.COMMAND_ACCEPTED]
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _raw(
                MessageType.COMMAND_ACCEPTED,
                {**accepted, "journal_status": "DISPATCHED"},
            )
        )

    progress = _protocol_payloads()[MessageType.WORKFLOW_PROGRESS]
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(_raw(MessageType.WORKFLOW_PROGRESS, {**progress, "step_index": 5}))

    naive = _protocol_payloads()[MessageType.AGENT_STATUS]
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(
            _raw(
                MessageType.AGENT_STATUS,
                {**naive, "changed_at": NOW_PROTOCOL_V1_REMAINING.replace(tzinfo=None).isoformat()},
            )
        )


def test_artifact_transfer_secret_is_masked_in_memory_and_explicit_on_the_wire() -> None:
    raw = _raw(
        MessageType.ARTIFACT_UPLOAD_REQUEST,
        _protocol_payloads()[MessageType.ARTIFACT_UPLOAD_REQUEST],
    )
    envelope = parse_control_plane_message(raw)
    assert isinstance(envelope, ArtifactUploadRequestEnvelope)
    assert isinstance(envelope.payload.transfer_token, SecretStr)
    assert envelope.payload.transfer_token.get_secret_value() == TRANSFER_SECRET
    assert TRANSFER_SECRET not in repr(envelope)
    assert TRANSFER_SECRET not in str(envelope.payload.transfer_token)

    python_dump = envelope.model_dump()
    payload_dump = python_dump["payload"]
    assert isinstance(payload_dump, dict)
    dumped_token = payload_dump["transfer_token"]
    assert isinstance(dumped_token, SecretStr)
    assert TRANSFER_SECRET not in repr(python_dump)

    wire = envelope.model_dump_json()
    assert TRANSFER_SECRET in wire
    assert parse_control_plane_message(json.loads(wire)) == envelope


def test_protocol_collection_and_untrusted_json_payloads_are_bounded() -> None:
    event = {
        "id": str(uuid4()),
        "agent_id": str(AGENT_ID),
        "sequence_number": 1,
        "event_type": "OPERATION_PROGRESS",
        "payload": {},
        "priority": 10,
        "created_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
    }
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed"):
        parse_agent_message(_raw(MessageType.EVENT_BATCH, {"events": [event] * 10_001}))

    oversized = _protocol_payloads()[MessageType.COMMAND_REQUEST]
    oversized_command = _command(payload={"blob": "x" * 2_100_000})
    with pytest.raises(ProtocolMessageInvalidError, match="validation failed") as error:
        parse_control_plane_message(
            _raw(
                MessageType.COMMAND_REQUEST,
                {**oversized, "command": oversized_command},
            )
        )
    assert "x" * 100 not in repr(error.value.details)


def _raw(message_type: MessageType, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(uuid4()),
        "message_type": message_type.value,
        "agent_id": str(AGENT_ID),
        "sent_at": NOW_PROTOCOL_V1_REMAINING.isoformat(),
        "correlation_id": str(CORRELATION_ID),
        "sequence_number": 1,
        "payload": payload,
    }


NOW_CONTROL_ACTOR_CONTEXT = datetime(2026, 8, 13, 9, tzinfo=UTC)


AGENT_ID_CONTROL_ACTOR_CONTEXT = UUID(int=70_001)


PRINCIPAL_ID = UUID(int=70_002)


SNAPSHOT_ID = UUID(int=70_003)


def _actor(snapshot_id: UUID | None = SNAPSHOT_ID) -> ActorContext:
    return ActorContext(
        principal_id=PRINCIPAL_ID,
        principal_type=PrincipalType.USER,
        display_name="Control Operator",
        organisation_id=UUID(int=70_004),
        authorisation_snapshot_id=snapshot_id,
    )


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        (
            MessageType.COMMAND_CANCEL,
            CommandCancelPayload(
                command_id=UUID(int=70_010),
                reason="operator request",
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.INVENTORY_REFRESH_REQUEST,
            InventoryRefreshRequestPayload(
                request_id=UUID(int=70_011),
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.RECONCILIATION_REQUEST,
            ReconciliationRequestPayload(
                request_id=UUID(int=70_012),
                expected_boot_id=UUID(int=70_013),
                last_control_plane_sequence=9,
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
        (
            MessageType.DRAIN_AGENT,
            DrainAgentPayload(
                drain=True,
                actor_context=_actor(),
                authorisation_snapshot_id=SNAPSHOT_ID,
            ),
        ),
    ],
)
def test_principal_initiated_control_payload_round_trips_actor_and_snapshot(
    message_type: MessageType,
    payload: ActorAttributedControlPayload,
) -> None:
    serialized = payload.model_dump(mode="json")
    assert serialized["actor_context"] == _actor().model_dump(mode="json")
    assert serialized["authorisation_snapshot_id"] == str(SNAPSHOT_ID)
    parsed = parse_control_plane_message(
        {
            "protocol_version": "1.0",
            "message_id": str(UUID(int=70_020)),
            "message_type": message_type.value,
            "agent_id": str(AGENT_ID_CONTROL_ACTOR_CONTEXT),
            "sent_at": NOW_CONTROL_ACTOR_CONTEXT.isoformat(),
            "correlation_id": str(UUID(int=70_021)),
            "sequence_number": 1,
            "payload": serialized,
        }
    )

    assert isinstance(parsed.payload, ActorAttributedControlPayload)
    assert parsed.payload.actor_context == _actor()
    assert parsed.payload.authorisation_snapshot_id == SNAPSHOT_ID


@pytest.mark.parametrize(
    "payload",
    [
        CommandCancelPayload(command_id=UUID(int=70_030)),
        InventoryRefreshRequestPayload(request_id=UUID(int=70_031)),
        ReconciliationRequestPayload(request_id=UUID(int=70_032)),
        DrainAgentPayload(drain=False),
    ],
)
def test_phase5_and_automatic_control_payloads_remain_valid_without_actor(
    payload: ActorAttributedControlPayload,
) -> None:
    assert payload.actor_context is None
    assert payload.authorisation_snapshot_id is None
    serialized = payload.model_dump(mode="json")
    assert "actor_context" not in serialized
    assert "authorisation_snapshot_id" not in serialized


@pytest.mark.parametrize(
    ("payload_type", "values"),
    [
        (CommandCancelPayload, {"command_id": UUID(int=70_040)}),
        (InventoryRefreshRequestPayload, {"request_id": UUID(int=70_041)}),
        (ReconciliationRequestPayload, {"request_id": UUID(int=70_042)}),
        (DrainAgentPayload, {"drain": True}),
    ],
)
def test_control_payload_rejects_mismatched_or_unattributed_snapshot(
    payload_type: type[ActorAttributedControlPayload],
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="snapshot IDs differ"):
        payload_type.model_validate(
            {
                **values,
                "actor_context": _actor(),
                "authorisation_snapshot_id": UUID(int=70_099),
            }
        )
    with pytest.raises(ValidationError, match="snapshot requires actor context"):
        payload_type.model_validate(
            {
                **values,
                "authorisation_snapshot_id": SNAPSHOT_ID,
            }
        )
    with pytest.raises(ValidationError, match="actor context requires authorisation snapshot"):
        payload_type.model_validate(
            {
                **values,
                "actor_context": _actor(snapshot_id=None),
            }
        )
