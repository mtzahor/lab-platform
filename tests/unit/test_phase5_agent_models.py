from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from lab_platform.models import (
    AgentCredential,
    AgentCredentialKind,
    AgentEnrollmentToken,
    AgentRecord,
    AgentStatus,
    EnrollmentStatus,
)
from pydantic import ValidationError

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


def test_agent_identity_normalizes_metadata_and_preserves_canonical_id() -> None:
    agent_id = uuid4()
    offset_now = NOW.astimezone(timezone(timedelta(hours=3)))
    agent = AgentRecord(
        id=agent_id,
        slug=" Home-Lab-1234 ",
        name="Home Lab",
        status=AgentStatus.OFFLINE,
        version="0.6.0-alpha",
        protocol_version="1.0",
        location="jerusalem-home",
        labels={" environment ": " development ", "owner": "michael"},
        registered_at=offset_now,
        enrollment_status=EnrollmentStatus.ENROLLED,
    )

    assert agent.id == agent_id
    assert agent.slug == "home-lab-1234"
    assert agent.registered_at == NOW
    assert agent.labels == {"environment": "development", "owner": "michael"}
    assert AgentStatus.DRAINING.value == "DRAINING"
    assert AgentStatus.DRAINED.value == "DRAINED"


def test_agent_identity_rejects_invalid_timeline_and_partial_revocation() -> None:
    common = {
        "slug": "home-lab-1234",
        "name": "Home Lab",
        "version": "0.6.0-alpha",
        "protocol_version": "1.0",
        "registered_at": NOW,
    }
    with pytest.raises(ValidationError, match="requires last_connected_at"):
        AgentRecord.model_validate({**common, "last_seen_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="must be set together"):
        AgentRecord.model_validate(
            {
                **common,
                "status": AgentStatus.REVOKED,
                "enrollment_status": EnrollmentStatus.ENROLLED,
                "revoked_at": NOW,
            }
        )
    with pytest.raises(ValidationError, match="internal hyphens"):
        AgentRecord.model_validate({**common, "slug": "not_valid"})


def test_enrollment_token_requires_bounded_single_use_metadata() -> None:
    agent_id = uuid4()
    request_id = uuid4()
    token = AgentEnrollmentToken(
        name="home-lab",
        token_hash="A" * 64,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        used_at=NOW + timedelta(minutes=1),
        used_by_agent_id=agent_id,
        enrollment_request_id=request_id,
        allowed_labels={" environment ": " development "},
    )

    assert token.token_hash == "a" * 64
    assert token.allowed_labels == {"environment": "development"}
    assert token.used_by_agent_id == agent_id

    with pytest.raises(ValidationError, match="must be set together"):
        AgentEnrollmentToken.model_validate(
            {
                **token.model_dump(),
                "used_by_agent_id": None,
            }
        )
    with pytest.raises(ValidationError, match="later than created_at"):
        AgentEnrollmentToken(
            name="expired",
            token_hash="b" * 64,
            created_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValidationError, match="SHA-256"):
        AgentEnrollmentToken(
            name="bad-hash",
            token_hash="not-a-hash",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )


def test_agent_credential_validates_version_hash_and_lifecycle() -> None:
    credential = AgentCredential(
        agent_id=uuid4(),
        kind=AgentCredentialKind.OPAQUE_TOKEN,
        credential_hash="C" * 64,
        version=2,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        last_used_at=NOW + timedelta(minutes=1),
    )
    assert credential.credential_hash == "c" * 64
    assert credential.version == 2

    with pytest.raises(ValidationError, match="later than created_at"):
        AgentCredential(
            agent_id=credential.agent_id,
            credential_hash="d" * 64,
            created_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValidationError):
        AgentCredential(
            agent_id=credential.agent_id,
            credential_hash="d" * 64,
            version=True,
            created_at=NOW,
        )
