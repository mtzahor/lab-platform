from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import total_ordering

from lab_platform.plugin_sdk.constants import PLUGIN_API_VERSION
from lab_platform.plugin_sdk.models import (
    CompatibilityStatus,
    PluginCompatibility,
    PluginMetadata,
)

_API_PATTERN = re.compile(r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)$")
_VERSION_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:(?:-|\.)?(?P<tag>a|alpha|b|beta|pre|preview|rc|dev|nightly)"
    r"(?:[.-]?(?P<number>[0-9]+))?)?(?:\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)
_PRERELEASE_RANK = {
    "dev": 0,
    "nightly": 0,
    "a": 1,
    "alpha": 1,
    "b": 2,
    "beta": 2,
    "pre": 2,
    "preview": 2,
    "rc": 3,
}


@total_ordering
@dataclass(frozen=True, slots=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease_rank: int = 4
    prerelease_number: int = 0

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        match = _VERSION_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError(f"unsupported semantic version: {value!r}")
        tag = match.group("tag")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease_rank=4 if tag is None else _PRERELEASE_RANK[tag.casefold()],
            prerelease_number=int(match.group("number") or 0),
        )

    def _key(self) -> tuple[int, int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease_rank,
            self.prerelease_number,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self._key() < other._key()


@dataclass(frozen=True, slots=True)
class PluginApiVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> PluginApiVersion:
        match = _API_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError(f"unsupported Plugin API version: {value!r}")
        return cls(major=int(match.group("major")), minor=int(match.group("minor")))

    def is_compatible_with(self, other: PluginApiVersion) -> bool:
        # Plugin API 1.x promises compatibility for the complete major line.
        return self.major == other.major


def normalize_platform(value: str | None = None) -> str:
    platform = (value or sys.platform).casefold()
    if platform.startswith("linux"):
        return "linux"
    if platform in {"darwin", "mac", "macos"}:
        return "macos"
    if platform.startswith(("win", "cygwin", "msys")):
        return "windows"
    return platform


def assess_plugin_compatibility(
    metadata: PluginMetadata,
    *,
    agent_version: str,
    supported_plugin_api_version: str = PLUGIN_API_VERSION,
    platform: str | None = None,
) -> PluginCompatibility:
    try:
        requested_api = PluginApiVersion.parse(metadata.plugin_api_version)
        supported_api = PluginApiVersion.parse(supported_plugin_api_version)
        running_agent = SemanticVersion.parse(agent_version)
        minimum_agent = SemanticVersion.parse(metadata.minimum_agent_version)
        maximum_agent = (
            SemanticVersion.parse(metadata.maximum_agent_version)
            if metadata.maximum_agent_version is not None
            else None
        )
    except ValueError as exc:
        return PluginCompatibility(
            compatible=False,
            status=CompatibilityStatus.INVALID_METADATA,
            message=str(exc),
            agent_version=agent_version,
            supported_plugin_api_version=supported_plugin_api_version,
        )
    if not requested_api.is_compatible_with(supported_api):
        return PluginCompatibility(
            compatible=False,
            status=CompatibilityStatus.PLUGIN_API_INCOMPATIBLE,
            message=(
                f"Plugin {metadata.name} {metadata.version} requires Plugin API "
                f"{metadata.plugin_api_version}. This Agent supports Plugin API "
                f"{supported_plugin_api_version}."
            ),
            agent_version=agent_version,
            supported_plugin_api_version=supported_plugin_api_version,
        )
    if running_agent < minimum_agent or (
        maximum_agent is not None and running_agent > maximum_agent
    ):
        maximum = metadata.maximum_agent_version or "newer"
        return PluginCompatibility(
            compatible=False,
            status=CompatibilityStatus.AGENT_VERSION_INCOMPATIBLE,
            message=(
                f"Plugin {metadata.name} supports Agent versions "
                f"{metadata.minimum_agent_version} through {maximum}; running {agent_version}."
            ),
            agent_version=agent_version,
            supported_plugin_api_version=supported_plugin_api_version,
        )
    running_platform = normalize_platform(platform)
    supported_platforms = {item.casefold() for item in metadata.supported_platforms}
    if "any" not in supported_platforms and running_platform not in supported_platforms:
        return PluginCompatibility(
            compatible=False,
            status=CompatibilityStatus.PLATFORM_UNSUPPORTED,
            message=(
                f"Plugin {metadata.name} does not support platform {running_platform}; "
                f"supported: {', '.join(sorted(supported_platforms))}."
            ),
            agent_version=agent_version,
            supported_plugin_api_version=supported_plugin_api_version,
        )
    return PluginCompatibility(
        compatible=True,
        status=CompatibilityStatus.COMPATIBLE,
        message=(
            f"Plugin {metadata.name} is compatible with Plugin API "
            f"{supported_plugin_api_version} and Agent {agent_version}."
        ),
        agent_version=agent_version,
        supported_plugin_api_version=supported_plugin_api_version,
    )


__all__ = [
    "PluginApiVersion",
    "SemanticVersion",
    "assess_plugin_compatibility",
    "normalize_platform",
]
