from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import total_ordering
from uuid import UUID

from lab_platform.agent_protocol import PROTOCOL_VERSION, negotiate_protocol_version
from lab_platform.agent_protocol.errors import ProtocolVersionUnsupportedError
from lab_platform.core.release import API_VERSION, PLUGIN_API_VERSION, ReleaseChannel
from lab_platform.core.version import VERSION

_VERSION_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-?(?P<tag>alpha|a|beta|b|preview|pre|rc|nightly|dev)"
    r"(?:[.-]?(?P<number>[0-9]+))?)?"
    r"(?:\+[0-9A-Za-z.-]+)?$",
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
class ApplicationVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    prerelease_number: int = 0

    @classmethod
    def parse(cls, value: str) -> ApplicationVersion:
        if not isinstance(value, str):
            raise ValueError("application version must be a string")
        match = _VERSION_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError(f"unsupported semantic application version: {value!r}")
        tag = match.group("tag")
        normalized_tag = tag.casefold() if tag is not None else None
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=normalized_tag,
            prerelease_number=int(match.group("number") or 0),
        )

    def _comparison_key(self) -> tuple[int, int, int, int, int]:
        rank = 4 if self.prerelease is None else _PRERELEASE_RANK[self.prerelease]
        return (self.major, self.minor, self.patch, rank, self.prerelease_number)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ApplicationVersion):
            return NotImplemented
        return self._comparison_key() < other._comparison_key()

    def __str__(self) -> str:
        suffix = ""
        if self.prerelease is not None:
            suffix = f"-{self.prerelease}"
            if self.prerelease_number:
                suffix += f".{self.prerelease_number}"
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"


class AgentUpgradeStatus(StrEnum):
    UP_TO_DATE = "up_to_date"
    UPGRADE_AVAILABLE = "upgrade_available"
    UPGRADE_RECOMMENDED = "upgrade_recommended"
    UPGRADE_REQUIRED = "upgrade_required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class AgentUpgradeAssessment:
    status: AgentUpgradeStatus
    agent_version: str
    protocol_version: str
    release_channel: ReleaseChannel
    target_version: str
    minimum_supported_version: str
    work_allowed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "agent_version": self.agent_version,
            "protocol_version": self.protocol_version,
            "release_channel": self.release_channel,
            "target_version": self.target_version,
            "minimum_supported_version": self.minimum_supported_version,
            "work_allowed": self.work_allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class VersionCompatibilityPolicy:
    minimum_supported_agent: ApplicationVersion
    minimum_recommended_agent: ApplicationVersion
    target_agent: ApplicationVersion
    maximum_supported_agent: ApplicationVersion
    control_plane_version: str = VERSION
    api_version: str = API_VERSION
    agent_protocol_version: str = PROTOCOL_VERSION
    plugin_api_version: str = PLUGIN_API_VERSION
    release_channel: ReleaseChannel = "stable"

    def __post_init__(self) -> None:
        if not (
            self.minimum_supported_agent
            <= self.minimum_recommended_agent
            <= self.target_agent
            <= self.maximum_supported_agent
        ):
            raise ValueError(
                "Agent compatibility versions must order minimum supported, minimum "
                "recommended, target, then maximum supported"
            )
        if self.release_channel not in {"stable", "preview", "nightly"}:
            raise ValueError("release channel must be stable, preview, or nightly")

    @classmethod
    def from_strings(
        cls,
        *,
        minimum_supported_agent: str,
        minimum_recommended_agent: str,
        target_agent: str,
        maximum_supported_agent: str,
        control_plane_version: str = VERSION,
        api_version: str = API_VERSION,
        agent_protocol_version: str = PROTOCOL_VERSION,
        plugin_api_version: str = PLUGIN_API_VERSION,
        release_channel: ReleaseChannel = "stable",
    ) -> VersionCompatibilityPolicy:
        return cls(
            minimum_supported_agent=ApplicationVersion.parse(minimum_supported_agent),
            minimum_recommended_agent=ApplicationVersion.parse(minimum_recommended_agent),
            target_agent=ApplicationVersion.parse(target_agent),
            maximum_supported_agent=ApplicationVersion.parse(maximum_supported_agent),
            control_plane_version=control_plane_version,
            api_version=api_version,
            agent_protocol_version=agent_protocol_version,
            plugin_api_version=plugin_api_version,
            release_channel=release_channel,
        )

    def evaluate_agent(
        self,
        version: str,
        protocol_version: str,
        release_channel: ReleaseChannel = "stable",
    ) -> AgentUpgradeAssessment:
        def assessment(
            status: AgentUpgradeStatus,
            *,
            work_allowed: bool,
            reason: str,
        ) -> AgentUpgradeAssessment:
            return AgentUpgradeAssessment(
                status=status,
                agent_version=version,
                protocol_version=protocol_version,
                release_channel=release_channel,
                target_version=str(self.target_agent),
                minimum_supported_version=str(self.minimum_supported_agent),
                work_allowed=work_allowed,
                reason=reason,
            )

        try:
            parsed = ApplicationVersion.parse(version)
        except ValueError:
            return assessment(
                AgentUpgradeStatus.UNSUPPORTED,
                work_allowed=False,
                reason="Agent application version is not a supported semantic version.",
            )
        try:
            negotiate_protocol_version(
                protocol_version,
                local_version=self.agent_protocol_version,
            )
        except (ProtocolVersionUnsupportedError, ValueError):
            return assessment(
                AgentUpgradeStatus.UNSUPPORTED,
                work_allowed=False,
                reason=(
                    "Agent protocol is incompatible with control-plane protocol "
                    f"{self.agent_protocol_version}."
                ),
            )
        if parsed > self.maximum_supported_agent:
            return assessment(
                AgentUpgradeStatus.UNSUPPORTED,
                work_allowed=False,
                reason="Agent is newer than the maximum version supported by this control plane.",
            )
        if parsed < self.minimum_supported_agent:
            return assessment(
                AgentUpgradeStatus.UPGRADE_REQUIRED,
                work_allowed=False,
                reason=(
                    "Agent must be upgraded before accepting work. Minimum supported version: "
                    f"{self.minimum_supported_agent}."
                ),
            )
        if parsed < self.minimum_recommended_agent:
            return assessment(
                AgentUpgradeStatus.UPGRADE_RECOMMENDED,
                work_allowed=True,
                reason="Agent remains supported, but an upgrade is recommended.",
            )
        if parsed < self.target_agent:
            return assessment(
                AgentUpgradeStatus.UPGRADE_AVAILABLE,
                work_allowed=True,
                reason="A newer compatible Agent release is available.",
            )
        return assessment(
            AgentUpgradeStatus.UP_TO_DATE,
            work_allowed=True,
            reason=(
                "Agent is at the target version."
                if parsed == self.target_agent
                else "Agent is newer than the target and remains compatible."
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentVersionInfo:
    id: UUID
    name: str
    version: str
    protocol_version: str
    release_channel: ReleaseChannel = "stable"


@dataclass(frozen=True, slots=True)
class AssessedAgentVersion:
    agent: AgentVersionInfo
    assessment: AgentUpgradeAssessment

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.agent.id),
            "name": self.agent.name,
            **self.assessment.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class UpgradeCheck:
    code: str
    message: str
    blocking: bool

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "blocking": self.blocking}


@dataclass(frozen=True, slots=True)
class UpgradeCheckReport:
    current_version: str
    target_version: str
    release_channel: ReleaseChannel
    agents: tuple[AssessedAgentVersion, ...]
    checks: tuple[UpgradeCheck, ...]

    @property
    def ready(self) -> bool:
        return not any(check.blocking for check in self.checks) and all(
            assessed.assessment.work_allowed for assessed in self.agents
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "target_version": self.target_version,
            "release_channel": self.release_channel,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
            "agents": [agent.as_dict() for agent in self.agents],
        }


def build_upgrade_report(
    policy: VersionCompatibilityPolicy,
    agents: Iterable[AgentVersionInfo],
    *,
    target_version: str,
    checks: Sequence[UpgradeCheck] = (),
) -> UpgradeCheckReport:
    ApplicationVersion.parse(target_version)
    assessed = tuple(
        AssessedAgentVersion(
            agent=agent,
            assessment=policy.evaluate_agent(
                agent.version,
                agent.protocol_version,
                agent.release_channel,
            ),
        )
        for agent in agents
    )
    compatibility_checks = list(checks)
    required = sum(
        item.assessment.status is AgentUpgradeStatus.UPGRADE_REQUIRED for item in assessed
    )
    unsupported = sum(item.assessment.status is AgentUpgradeStatus.UNSUPPORTED for item in assessed)
    if required:
        compatibility_checks.append(
            UpgradeCheck(
                code="AGENT_UPGRADE_REQUIRED",
                message=f"{required} Agent(s) require an upgrade before control-plane upgrade.",
                blocking=True,
            )
        )
    if unsupported:
        compatibility_checks.append(
            UpgradeCheck(
                code="AGENT_VERSION_UNSUPPORTED",
                message=f"{unsupported} Agent(s) are incompatible with the target policy.",
                blocking=True,
            )
        )
    return UpgradeCheckReport(
        current_version=policy.control_plane_version,
        target_version=target_version,
        release_channel=policy.release_channel,
        agents=assessed,
        checks=tuple(compatibility_checks),
    )
