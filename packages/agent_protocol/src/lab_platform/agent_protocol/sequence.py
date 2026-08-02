from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from lab_platform.agent_protocol.envelopes import EnvelopeBase
from lab_platform.agent_protocol.errors import ProtocolSequenceError


class SequenceDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(slots=True)
class IncomingSequenceTracker:
    """Validate one connection's ordered stream with bounded duplicate memory.

    A new tracker is created for every connection. Persisted command and event IDs,
    rather than this transport sequence, provide duplicate protection across reconnects.
    """

    expected_sequence: int = 1
    max_recent_message_ids: int = 4096
    _recent_message_ids: deque[UUID] = field(default_factory=deque, init=False, repr=False)
    _seen_messages: dict[UUID, tuple[int, bytes]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.expected_sequence < 1:
            raise ValueError("expected_sequence must be at least 1")
        if self.max_recent_message_ids < 1:
            raise ValueError("max_recent_message_ids must be at least 1")

    @property
    def last_accepted_sequence(self) -> int:
        return self.expected_sequence - 1

    def observe(self, envelope: EnvelopeBase) -> SequenceDisposition:
        previous = self._seen_messages.get(envelope.message_id)
        fingerprint = _message_fingerprint(envelope)
        if previous is not None:
            if previous == (envelope.sequence_number, fingerprint):
                return SequenceDisposition.DUPLICATE
            raise ProtocolSequenceError(
                "Protocol message ID was reused with different content.",
                message_id=str(envelope.message_id),
                original_sequence=previous[0],
                received_sequence=envelope.sequence_number,
            )
        if envelope.sequence_number != self.expected_sequence:
            kind = "gap" if envelope.sequence_number > self.expected_sequence else "replay"
            raise ProtocolSequenceError(
                f"Protocol sequence {kind} detected.",
                expected_sequence=self.expected_sequence,
                received_sequence=envelope.sequence_number,
                message_id=str(envelope.message_id),
            )

        self._recent_message_ids.append(envelope.message_id)
        self._seen_messages[envelope.message_id] = (envelope.sequence_number, fingerprint)
        self.expected_sequence += 1
        if len(self._recent_message_ids) > self.max_recent_message_ids:
            expired = self._recent_message_ids.popleft()
            del self._seen_messages[expired]
        return SequenceDisposition.ACCEPTED


def _message_fingerprint(envelope: EnvelopeBase) -> bytes:
    canonical = json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()
