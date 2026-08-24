from __future__ import annotations

from uuid import UUID

import pytest
from lab_platform.control_plane.compatibility import (
    AgentUpgradeStatus,
    AgentVersionInfo,
    ApplicationVersion,
    UpgradeCheck,
    VersionCompatibilityPolicy,
    build_upgrade_report,
)


def _policy() -> VersionCompatibilityPolicy:
    return VersionCompatibilityPolicy.from_strings(
        minimum_supported_agent="0.8.0",
        minimum_recommended_agent="0.8.5",
        target_agent="0.9.0-beta",
        maximum_supported_agent="0.9.99",
        control_plane_version="0.9.0-beta",
        release_channel="preview",
    )


def test_application_version_parses_release_spellings_and_orders_prereleases() -> None:
    assert ApplicationVersion.parse("v0.9.0-beta.2") == ApplicationVersion(
        0,
        9,
        0,
        "beta",
        2,
    )
    assert ApplicationVersion.parse("0.9.0b3+build.7") == ApplicationVersion(
        0,
        9,
        0,
        "b",
        3,
    )
    assert (
        ApplicationVersion.parse("0.9.0-dev")
        < ApplicationVersion.parse("0.9.0-alpha")
        < ApplicationVersion.parse("0.9.0-beta")
        < ApplicationVersion.parse("0.9.0-rc.1")
        < ApplicationVersion.parse("0.9.0")
    )

    for invalid in ("", "0.9", "0.09.0", "0.9.0-final", "release-0.9.0"):
        with pytest.raises(ValueError, match="semantic application version"):
            ApplicationVersion.parse(invalid)


@pytest.mark.parametrize(
    ("version", "protocol", "expected_status", "work_allowed"),
    [
        ("not-a-version", "1.0", AgentUpgradeStatus.UNSUPPORTED, False),
        ("0.9.0-beta", "2.0", AgentUpgradeStatus.UNSUPPORTED, False),
        ("1.0.0", "1.0", AgentUpgradeStatus.UNSUPPORTED, False),
        ("0.7.99", "1.0", AgentUpgradeStatus.UPGRADE_REQUIRED, False),
        ("0.8.4", "1.0", AgentUpgradeStatus.UPGRADE_RECOMMENDED, True),
        ("0.8.5", "1.0", AgentUpgradeStatus.UPGRADE_AVAILABLE, True),
        ("0.9.0-beta", "1.0", AgentUpgradeStatus.UP_TO_DATE, True),
        ("0.9.1", "1.0", AgentUpgradeStatus.UP_TO_DATE, True),
    ],
)
def test_agent_policy_reports_every_upgrade_state(
    version: str,
    protocol: str,
    expected_status: AgentUpgradeStatus,
    work_allowed: bool,
) -> None:
    assessment = _policy().evaluate_agent(version, protocol, "nightly")

    assert assessment.status is expected_status
    assert assessment.work_allowed is work_allowed
    assert assessment.agent_version == version
    assert assessment.protocol_version == protocol
    assert assessment.minimum_supported_version == "0.8.0"
    assert assessment.target_version == "0.9.0-beta"
    assert assessment.release_channel == "nightly"
    assert assessment.reason
    assert assessment.as_dict()["status"] == expected_status.value


def test_compatibility_policy_rejects_an_inverted_support_window() -> None:
    with pytest.raises(ValueError, match="must order"):
        VersionCompatibilityPolicy.from_strings(
            minimum_supported_agent="0.9.0",
            minimum_recommended_agent="0.8.5",
            target_agent="0.9.0",
            maximum_supported_agent="1.0.0",
        )


def test_upgrade_report_combines_operator_checks_and_agent_blockers() -> None:
    report = build_upgrade_report(
        _policy(),
        (
            AgentVersionInfo(
                id=UUID(int=1),
                name="supported",
                version="0.8.5",
                protocol_version="1.0",
            ),
            AgentVersionInfo(
                id=UUID(int=2),
                name="too-old",
                version="0.7.0",
                protocol_version="1.0",
            ),
            AgentVersionInfo(
                id=UUID(int=3),
                name="bad-protocol",
                version="0.9.0-beta",
                protocol_version="99.0",
            ),
        ),
        target_version="0.9.1",
        checks=(
            UpgradeCheck(
                code="BACKUP_VERIFIED",
                message="Backup was created and verified.",
                blocking=False,
            ),
        ),
    )

    assert report.ready is False
    assert [item.assessment.status for item in report.agents] == [
        AgentUpgradeStatus.UPGRADE_AVAILABLE,
        AgentUpgradeStatus.UPGRADE_REQUIRED,
        AgentUpgradeStatus.UNSUPPORTED,
    ]
    assert [check.code for check in report.checks] == [
        "BACKUP_VERIFIED",
        "AGENT_UPGRADE_REQUIRED",
        "AGENT_VERSION_UNSUPPORTED",
    ]
    document = report.as_dict()
    assert document["release_channel"] == "preview"
    assert document["ready"] is False
    agents = document["agents"]
    assert isinstance(agents, list)
    assert len(agents) == 3
