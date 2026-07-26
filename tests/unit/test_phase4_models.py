from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from lab_platform.models import (
    ApiToken,
    ApiTokenScope,
    ArtifactOwnerType,
    ArtifactRecord,
    BenchRequest,
    CiOutcome,
    CiProvider,
    CiSession,
    CiSessionStatus,
    CleanupResult,
    CleanupStatus,
    ReservationSource,
)
from lab_platform.models import (
    TestResult as HardwareTestResult,
)
from lab_platform.models import (
    TestStatus as HardwareTestStatus,
)
from pydantic import ValidationError

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def test_phase4_ci_models_normalize_values_and_preserve_outcome() -> None:
    offset_now = NOW.astimezone(timezone(timedelta(hours=3)))
    request = BenchRequest(
        required_capabilities={" Firmware ", "SERIAL"},
        required_labels={" board ": " esp32 "},
        preferred_labels={"location": "lab-a"},
    )
    session = CiSession(
        provider=CiProvider.GITHUB_ACTIONS,
        external_run_id="12345",
        requested_by="github-actions",
        status=CiSessionStatus.COMPLETED,
        outcome=CiOutcome.SUCCEEDED,
        created_at=offset_now,
        completed_at=offset_now,
        cleanup_status=CleanupStatus.SUCCEEDED,
        bench_request=request,
    )

    assert request.required_capabilities == {"firmware", "serial"}
    assert request.required_labels == {"board": "esp32"}
    assert session.created_at == NOW
    assert session.status is CiSessionStatus.COMPLETED
    assert session.outcome is CiOutcome.SUCCEEDED
    assert session.bench_request == request
    assert ReservationSource.CI.value == "ci"


def test_phase4_token_artifact_cleanup_and_test_result_models() -> None:
    token = ApiToken(
        name="github-demo",
        token_hash="a" * 64,
        owner="github-actions",
        scopes={ApiTokenScope.CI_SESSIONS, ApiTokenScope.ARTIFACTS_WRITE},
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    artifact = ArtifactRecord(
        owner_type=ArtifactOwnerType.CI_SESSION,
        owner_id=uuid4(),
        name="firmware.bin",
        artifact_type="firmware",
        content_type="application/octet-stream",
        path="objects/ab/cd",
        size_bytes=42,
        sha256="b" * 64,
        created_at=NOW,
        metadata={"board": "esp32"},
    )
    cleanup = CleanupResult(
        reservation_released=True,
        workflow_stopped=True,
        locks_released=True,
        serial_closed=True,
        artifacts_finalized=True,
    )
    result = HardwareTestResult(
        name="ESP32 boot verification",
        status=HardwareTestStatus.PASSED,
        duration_ms=4271,
        details={"firmware_version": "0.5.0"},
    )

    assert token.scopes == {
        ApiTokenScope.CI_SESSIONS,
        ApiTokenScope.ARTIFACTS_WRITE,
    }
    assert artifact.owner_type is ArtifactOwnerType.CI_SESSION
    assert cleanup.errors == []
    assert result.status is HardwareTestStatus.PASSED


def test_phase4_models_reject_unsafe_or_incoherent_values() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        BenchRequest(allow_simulated=False, allow_physical=False)
    with pytest.raises(ValidationError, match="expires_at must be later"):
        ApiToken(
            name="expired",
            token_hash="a" * 64,
            owner="ci",
            scopes={ApiTokenScope.CI_SESSIONS},
            created_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        CiSession(
            provider=CiProvider.LOCAL,
            external_run_id="local-1",
            requested_by="developer",
            created_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValidationError, match="String should match pattern"):
        ArtifactRecord(
            owner_type=ArtifactOwnerType.OPERATION,
            owner_id=uuid4(),
            name="trace.log",
            artifact_type="trace",
            path="objects/trace",
            size_bytes=1,
            sha256="not-a-checksum",
        )
