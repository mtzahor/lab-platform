from __future__ import annotations

import re
from dataclasses import dataclass

from lab_platform.agent_protocol.errors import ProtocolVersionUnsupportedError

PROTOCOL_VERSION = "1.0"
_PROTOCOL_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


@dataclass(frozen=True, order=True, slots=True)
class ProtocolVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        if type(self.major) is not int or type(self.minor) is not int:
            raise TypeError("Protocol version components must be integers")
        if self.major < 0 or self.minor < 0:
            raise ValueError("Protocol version components cannot be negative")

    @classmethod
    def parse(cls, value: str) -> ProtocolVersion:
        if not isinstance(value, str):
            raise ProtocolVersionUnsupportedError(
                "Protocol version must be a string.",
                received_type=type(value).__name__,
            )
        match = _PROTOCOL_VERSION_PATTERN.fullmatch(value)
        if match is None:
            raise ProtocolVersionUnsupportedError(
                "Protocol version must use '<major>.<minor>' syntax.",
                protocol_version=value,
            )
        return cls(major=int(match.group(1)), minor=int(match.group(2)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    def is_compatible_with(self, other: ProtocolVersion) -> bool:
        return self.major == other.major


def negotiate_protocol_version(
    remote_version: str,
    *,
    local_version: str = PROTOCOL_VERSION,
) -> str:
    """Return the highest common minor version when protocol majors match."""

    local = ProtocolVersion.parse(local_version)
    remote = ProtocolVersion.parse(remote_version)
    if not local.is_compatible_with(remote):
        raise ProtocolVersionUnsupportedError(
            "Agent protocol major versions are incompatible.",
            local_version=str(local),
            remote_version=str(remote),
        )
    return str(ProtocolVersion(local.major, min(local.minor, remote.minor)))
