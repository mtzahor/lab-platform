from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from lab_platform.agent_protocol import (
    AGENT_TO_CONTROL_PLANE_MESSAGE_TYPES,
    CONTROL_PLANE_TO_AGENT_MESSAGE_TYPES,
    PROTOCOL_VERSION,
    SUPPORTED_MESSAGE_TYPES,
    EventAckEnvelope,
    EventAckPayload,
    MessageType,
    ProtocolMessageInvalidError,
    parse_agent_message,
    parse_control_plane_message,
)
from lab_platform.agent_protocol.envelopes import ArtifactUploadRequestEnvelope
from pydantic import SecretStr

AGENT_ID = UUID("9a975329-5ec8-4a5f-83b4-762684206e34")
OTHER_AGENT_ID = UUID("4c4e5910-bfe4-4cb7-a3e3-f40098f3be65")
BOOT_ID = UUID("1ab59e13-5517-482f-849c-2fe8145db25c")
COMMAND_ID = UUID("10f76299-e654-41a8-a896-71f8fd9bd0b1")
RESERVATION_ID = UUID("a9e16eb6-cc01-48df-a6c7-c99ef117a002")
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
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
        "valid_from": NOW.isoformat(),
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
        "created_at": NOW.isoformat(),
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
        "created_at": NOW.isoformat(),
    }


def _protocol_payloads() -> dict[MessageType, dict[str, object]]:
    operation_base: dict[str, object] = {
        "command_id": str(COMMAND_ID),
        "local_operation_id": str(uuid4()),
        "occurred_at": NOW.isoformat(),
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
            "timestamp": NOW.isoformat(),
        },
        MessageType.AGENT_STATUS: {
            "agent_id": str(AGENT_ID),
            "boot_id": str(BOOT_ID),
            "status": "ONLINE",
            "changed_at": NOW.isoformat(),
            "reason": "connection established",
        },
        MessageType.BENCH_SNAPSHOT: {
            "boot_id": str(BOOT_ID),
            "generated_at": NOW.isoformat(),
            "benches": [_bench()],
        },
        MessageType.BENCH_ADDED: {
            "boot_id": str(BOOT_ID),
            "bench": _bench(),
            "changed_at": NOW.isoformat(),
        },
        MessageType.BENCH_REMOVED: {
            "boot_id": str(BOOT_ID),
            "local_bench_id": "esp32-01",
            "changed_at": NOW.isoformat(),
        },
        MessageType.BENCH_HEALTH_CHANGED: {
            "boot_id": str(BOOT_ID),
            "local_bench_id": "esp32-01",
            "connectivity": "degraded",
            "health": "warning",
            "changed_at": NOW.isoformat(),
        },
        MessageType.COMMAND_ACCEPTED: {
            "command_id": str(COMMAND_ID),
            "accepted_at": NOW.isoformat(),
            "journal_status": "ACCEPTED",
            "local_operation_id": str(uuid4()),
        },
        MessageType.COMMAND_REJECTED: {
            "command_id": str(COMMAND_ID),
            "rejected_at": NOW.isoformat(),
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
            "occurred_at": NOW.isoformat(),
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
                    "created_at": NOW.isoformat(),
                }
            ]
        },
        MessageType.RECONCILIATION_REPORT: {
            "report": {
                "agent_id": str(AGENT_ID),
                "boot_id": str(BOOT_ID),
                "generated_at": NOW.isoformat(),
                "active_commands": [
                    {
                        "command_id": str(COMMAND_ID),
                        "status": "RUNNING",
                        "updated_at": NOW.isoformat(),
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
            "server_time": NOW.isoformat(),
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
            "released_at": NOW.isoformat(),
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
        "sent_at": NOW.isoformat(),
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
                {**naive, "changed_at": NOW.replace(tzinfo=None).isoformat()},
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
        "created_at": NOW.isoformat(),
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
        "sent_at": NOW.isoformat(),
        "correlation_id": str(CORRELATION_ID),
        "sequence_number": 1,
        "payload": payload,
    }
