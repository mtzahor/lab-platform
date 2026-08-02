from __future__ import annotations


class AgentProtocolError(ValueError):
    """Base error with a stable code suitable for transport error envelopes."""

    code = "PROTOCOL_ERROR"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class ProtocolVersionUnsupportedError(AgentProtocolError):
    code = "PROTOCOL_VERSION_UNSUPPORTED"


class ProtocolMessageInvalidError(AgentProtocolError):
    code = "PROTOCOL_MESSAGE_INVALID"


class ProtocolSequenceError(AgentProtocolError):
    code = "PROTOCOL_SEQUENCE_ERROR"
