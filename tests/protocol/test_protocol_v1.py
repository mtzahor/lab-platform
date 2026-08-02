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
    AgentHeartbeatEnvelope,
    AgentHelloEnvelope,
    AgentStatus,
    BenchSnapshotEnvelope,
    IncomingSequenceTracker,
    MessageDirection,
    MessageType,
    ProtocolMessageInvalidError,
    ProtocolSequenceError,
    ProtocolVersion,
    ProtocolVersionUnsupportedError,
    SequenceDisposition,
    WelcomeEnvelope,
    negotiate_protocol_version,
    parse_agent_message,
    parse_control_plane_message,
    parse_envelope,
    validate_message_direction,
)

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
